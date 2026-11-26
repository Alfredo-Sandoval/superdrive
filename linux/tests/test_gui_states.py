"""Tests for how the window maps a status snapshot to what it shows."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import superdrive_gui
except ImportError as error:  # PySide6 is only needed for the window.
    raise unittest.SkipTest(f"PySide6 is not installed: {error}")


def snapshot(media=None, wake=None, auto_wake=True, connected=True):
    drive = {
        "name": "sr0", "device": "/dev/sr0", "vendor": "Apple", "model": "SuperDrive", "revision": "2.03",
        "plug_id": "1-14",
        "media": media or {"present": False, "mounted_at": None, "label": "", "fs_type": "", "size_bytes": None},
        "wake": wake,
    }
    return {"drives": [drive] if connected else [], "auto_wake_installed": auto_wake}


GOOD_WAKE = {"ok": True, "detail": "SCSI status 0x00 (GOOD)", "source": "udev", "time": "2026-11-26T02:04:40+00:00"}
FAILED_WAKE = {"ok": False, "detail": "SCSI status 0x02 (CHECK CONDITION)", "source": "gui",
               "time": "2026-11-26T02:05:00+00:00"}


class DescribeStateTests(unittest.TestCase):
    def describe(self, *args, busy=None, **kwargs):
        return superdrive_gui.describe_state(snapshot(*args, **kwargs), busy)

    def test_busy_wins(self):
        view = self.describe(wake=GOOD_WAKE, busy=("Ejecting the disc", "Unmounting."))
        self.assertEqual((view.badge, view.headline, view.spinning), ("Working", "Ejecting the disc", True))

    def test_before_the_first_status(self):
        self.assertEqual(superdrive_gui.describe_state(None, None).headline, "Looking for your SuperDrive")

    def test_not_connected_mentions_install_only_without_auto_wake(self):
        self.assertNotIn("install.sh", self.describe(connected=False).detail)
        self.assertIn("install.sh", self.describe(connected=False, auto_wake=False).detail)

    def test_woken_on_plug_in(self):
        view = self.describe(wake=GOOD_WAKE)
        self.assertEqual((view.tone, view.headline, view.action), ("Success", "Ready for a disc", None))
        self.assertIn("automatically", view.detail)

    def test_failed_wake_offers_retry(self):
        view = self.describe(wake=FAILED_WAKE)
        self.assertEqual((view.tone, view.action, view.action_text), ("Danger", "wake", "Try again"))
        self.assertIn("CHECK CONDITION", view.detail)

    def test_no_wake_record_asks_to_wake(self):
        view = self.describe()
        self.assertEqual((view.tone, view.action, view.action_text), ("Warning", "wake", "Wake drive"))

    def test_mounted_disc_can_be_opened(self):
        media = {"present": True, "mounted_at": "/media/alex/DISC", "label": "DISC", "fs_type": "udf",
                 "size_bytes": 4_700_000_000}
        view = self.describe(media=media, wake=GOOD_WAKE)
        self.assertEqual((view.headline, view.action), ("Disc ready", "open"))
        self.assertIn("UDF", view.detail)
        self.assertIn("4.4 GB", view.detail)

    def test_unidentified_disc_spins(self):
        media = {"present": True, "mounted_at": None, "label": "", "fs_type": "", "size_bytes": None}
        view = self.describe(media=media, wake=GOOD_WAKE)
        self.assertEqual((view.headline, view.spinning, view.action), ("Reading the disc", True, None))


if __name__ == "__main__":
    unittest.main()
