"""Tests for the parts of superdrive.py that do not need a real drive or Linux."""

import ctypes
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import superdrive  # noqa: E402


def make_scsi_drive(sysfs: Path, name: str, vendor: str, model: str) -> None:
    """Builds /sys/class/block/<name>/device with padded inquiry strings, as the kernel does."""
    scsi_device = sysfs / "class" / "block" / name / "device"
    scsi_device.mkdir(parents=True)
    (scsi_device / "vendor").write_text(f"{vendor:<8}\n")
    (scsi_device / "model").write_text(f"{model:<16}\n")
    (scsi_device / "rev").write_text("2.03\n")
    (sysfs / "class" / "block" / name / "size").write_text("0\n")


class StructLayoutTests(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "layout checked for 64-bit Linux")
    def test_sg_io_header_matches_the_kernel_size(self):
        self.assertEqual(ctypes.sizeof(superdrive.SgIoHeader), 88)
        self.assertEqual(superdrive.SgIoHeader.status.offset, 64)
        self.assertEqual(superdrive.SgIoHeader.host_status.offset, 68)


class SenseTests(unittest.TestCase):
    def test_fixed_format(self):
        sense = bytes([0x70, 0, 0x06, 0, 0, 0, 0, 10, 0, 0, 0, 0, 0x29, 0x00])
        self.assertEqual(superdrive.decode_sense(sense), (0x06, 0x29, 0x00))

    def test_descriptor_format(self):
        self.assertEqual(superdrive.decode_sense(bytes([0x72, 0x05, 0x20, 0x00])), (0x05, 0x20, 0x00))

    def test_too_short_or_unknown(self):
        self.assertIsNone(superdrive.decode_sense(b""))
        self.assertIsNone(superdrive.decode_sense(bytes([0x70, 0, 0x06, 0])))
        self.assertIsNone(superdrive.decode_sense(bytes([0x00, 0, 0, 0])))

    def test_describe_rejection(self):
        result = superdrive.WakeResult(status=0x02, host_status=0, driver_status=0x08,
                                       sense=(0x05, 0x20, 0x00), attempts=1)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.describe(),
                         "SCSI status 0x02 (CHECK CONDITION), sense ILLEGAL REQUEST (key 0x5, ASC 0x20, ASCQ 0x00)")

    def test_describe_success_after_unit_attention(self):
        result = superdrive.WakeResult(status=0, host_status=0, driver_status=0, sense=None, attempts=2)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.describe(), "SCSI status 0x00 (GOOD), 2 attempts")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sysfs = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_finds_only_superdrives_in_kernel_order(self):
        make_scsi_drive(self.sysfs, "sr10", "Apple", "SuperDrive")
        make_scsi_drive(self.sysfs, "sr1", "HL-DT-ST", "DVDRAM GP65NB60")
        make_scsi_drive(self.sysfs, "sr2", "Apple", "SuperDrive")
        drives = superdrive.find_superdrives(self.sysfs)
        self.assertEqual([drive.device for drive in drives], ["/dev/sr2", "/dev/sr10"])
        self.assertEqual(drives[0].vendor, "Apple")
        self.assertEqual(drives[0].model, "SuperDrive")
        self.assertEqual(drives[0].revision, "2.03")

    def test_plug_id_comes_from_the_usb_device_above(self):
        # The kernel names these 1-2:1.0, target3:0:0 and 3:0:0:0; colons are left out so the
        # test also runs on Windows, and the lookup does not depend on the names.
        usb_device = self.sysfs / "devices" / "usb1" / "1-2"
        scsi_device = usb_device / "1-2.1.0" / "host3" / "target3.0.0" / "3.0.0.0"
        scsi_device.mkdir(parents=True)
        (usb_device / "busnum").write_text("1\n")
        (usb_device / "devnum").write_text("14\n")
        self.assertEqual(superdrive.usb_plug_id(scsi_device), "1-14")

    def test_select_refuses_devices_that_are_not_superdrives(self):
        make_scsi_drive(self.sysfs, "sr0", "Apple", "SuperDrive")
        make_scsi_drive(self.sysfs, "sr1", "HL-DT-ST", "DVDRAM GP65NB60")
        self.assertEqual(superdrive.select_drives("/dev/sr0", self.sysfs)[0].name, "sr0")
        for device in ("/dev/sr1", "/dev/sda", "/dev/sr0/../sda", "sr0"):
            with self.subTest(device=device), self.assertRaises(superdrive.HelperError):
                superdrive.select_drives(device, self.sysfs)

    def test_select_all_needs_a_drive(self):
        with self.assertRaises(superdrive.HelperError):
            superdrive.select_drives(None, self.sysfs)


class ParsingTests(unittest.TestCase):
    def test_udev_properties(self):
        text = "DEVNAME=/dev/sr0\nID_CDROM_MEDIA=1\nID_FS_LABEL_ENC=My\\x20Disc\nID_FS_TYPE=udf\n"
        properties = superdrive.parse_udev_properties(text)
        self.assertEqual(properties["ID_FS_TYPE"], "udf")
        self.assertEqual(superdrive.decode_udev_value(properties["ID_FS_LABEL_ENC"]), "My Disc")

    def test_mount_point_is_unescaped(self):
        mounts = (
            "/dev/nvme0n1p2 / ext4 rw 0 0\n"
            "/dev/sr0 /media/alex/My\\040Disc udf ro,nosuid 0 0\n"
        )
        self.assertEqual(superdrive.find_mount_point({"/dev/sr0"}, mounts), "/media/alex/My Disc")
        self.assertIsNone(superdrive.find_mount_point({"/dev/sr1"}, mounts))


class WakeRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.drive = superdrive.Drive("sr0", "/dev/sr0", "Apple", "SuperDrive", "2.03", plug_id="1-14")

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_for_the_same_connection(self):
        superdrive.write_wake_record(self.drive, True, "SCSI status 0x00 (GOOD)", "udev", self.state_dir)
        record = superdrive.read_wake_record(self.drive, self.state_dir)
        self.assertTrue(record["ok"])
        self.assertEqual(record["source"], "udev")

    def test_record_from_an_earlier_connection_is_ignored(self):
        superdrive.write_wake_record(self.drive, True, "SCSI status 0x00 (GOOD)", "udev", self.state_dir)
        replugged = superdrive.Drive("sr0", "/dev/sr0", "Apple", "SuperDrive", "2.03", plug_id="1-15")
        self.assertIsNone(superdrive.read_wake_record(replugged, self.state_dir))

    def test_no_record_yet(self):
        self.assertIsNone(superdrive.read_wake_record(self.drive, self.state_dir))


class CommandLineTests(unittest.TestCase):
    def test_pkexec_only_allows_wake(self):
        with mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(superdrive.main(["status"]), 2)
        self.assertIn("only runs 'wake'", errors.getvalue())

    def test_status_json_with_no_drives(self):
        with tempfile.TemporaryDirectory() as temp:
            status = superdrive.collect_status(Path(temp), Path(temp) / "state")
        self.assertEqual(status["drives"], [])
        json.dumps(status)  # the window parses this output


if __name__ == "__main__":
    unittest.main()
