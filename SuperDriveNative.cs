// Native calls shared by AppleSuperDriveGui.ps1 and Enable-AppleSuperDrive.ps1: the SuperDrive
// wake and eject commands, plus the GUI's title-bar theming.
// Windows PowerShell 5.1 compiles this with the .NET Framework C# 5 compiler, so it avoids
// newer language features (string interpolation, expression-bodied members, nameof, ?.).
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace AppleSuperDrive {
    // Outcome of one wake request: the SCSI status of the last attempt and its decoded sense data.
    public sealed class WakeResult {
        private static readonly string[] SenseKeyNames = {
            "NO SENSE", "RECOVERED ERROR", "NOT READY", "MEDIUM ERROR",
            "HARDWARE ERROR", "ILLEGAL REQUEST", "UNIT ATTENTION", "DATA PROTECT",
            "BLANK CHECK", "VENDOR SPECIFIC", "COPY ABORTED", "ABORTED COMMAND",
            "RESERVED", "VOLUME OVERFLOW", "MISCOMPARE", "COMPLETED"
        };

        public string DevicePath { get; set; }
        public byte ScsiStatus { get; set; }
        public uint BytesReturned { get; set; }
        public int Attempts { get; set; }
        public bool HasSense { get; set; }
        public byte SenseKey { get; set; }
        public byte AdditionalSenseCode { get; set; }
        public byte AdditionalSenseCodeQualifier { get; set; }
        public string SenseDataHex { get; set; }

        public bool Succeeded {
            get { return ScsiStatus == Native.ScsiStatusGood; }
        }

        public string Describe() {
            string text = string.Format("SCSI status 0x{0:X2} ({1})", ScsiStatus, StatusName(ScsiStatus));

            if (!Succeeded && HasSense) {
                text += string.Format(
                    ", sense {0} (key 0x{1:X}, ASC 0x{2:X2}, ASCQ 0x{3:X2})",
                    SenseKeyNames[SenseKey & 0x0F],
                    SenseKey,
                    AdditionalSenseCode,
                    AdditionalSenseCodeQualifier);
            }

            if (Attempts > 1) {
                text += string.Format(", {0} attempts", Attempts);
            }

            return text;
        }

        private static string StatusName(byte status) {
            switch (status) {
                case 0x00: return "GOOD";
                case 0x02: return "CHECK CONDITION";
                case 0x08: return "BUSY";
                case 0x18: return "RESERVATION CONFLICT";
                case 0x28: return "TASK SET FULL";
                default: return "UNEXPECTED";
            }
        }
    }

    public static class Native {
        internal const byte ScsiStatusGood = 0x00;
        private const byte SenseKeyUnitAttention = 0x06;

        // The first command after the drive powers on usually reports UNIT ATTENTION instead of
        // running. Retrying clears it; any other failure is returned to the caller unchanged.
        private const int MaxWakeAttempts = 3;

        private const uint IOCTL_SCSI_PASS_THROUGH = 0x0004D004;
        private const uint IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808;
        private const byte SCSI_IOCTL_DATA_UNSPECIFIED = 2;
        private const uint GENERIC_READ = 0x80000000;
        private const uint GENERIC_WRITE = 0x40000000;
        private const uint FILE_SHARE_READ = 0x00000001;
        private const uint FILE_SHARE_WRITE = 0x00000002;
        private const uint OPEN_EXISTING = 3;
        private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential)]
        private struct SCSI_PASS_THROUGH {
            public ushort Length;
            public byte ScsiStatus;
            public byte PathId;
            public byte TargetId;
            public byte Lun;
            public byte CdbLength;
            public byte SenseInfoLength;
            public byte DataIn;
            public uint DataTransferLength;
            public uint TimeOutValue;
            public UIntPtr DataBufferOffset;
            public uint SenseInfoOffset;

            [MarshalAs(UnmanagedType.ByValArray, SizeConst = 16)]
            public byte[] Cdb;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct SptWithSense {
            public SCSI_PASS_THROUGH Spt;

            [MarshalAs(UnmanagedType.ByValArray, SizeConst = 32)]
            public byte[] Sense;
        }

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateFile(
            string lpFileName,
            uint dwDesiredAccess,
            uint dwShareMode,
            IntPtr lpSecurityAttributes,
            uint dwCreationDisposition,
            uint dwFlagsAndAttributes,
            IntPtr hTemplateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool DeviceIoControl(
            IntPtr hDevice,
            uint dwIoControlCode,
            IntPtr lpInBuffer,
            int nInBufferSize,
            IntPtr lpOutBuffer,
            int nOutBufferSize,
            out uint lpBytesReturned,
            IntPtr lpOverlapped);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        // Sends the vendor-specific SuperDrive initialization CDB (EA 00 00 00 00 00 01).
        // Requires administrator rights because it uses SCSI pass-through.
        public static WakeResult Wake(string driveLetter) {
            string path = GetDevicePath(driveLetter);
            IntPtr handle = OpenDevice(path, GENERIC_READ | GENERIC_WRITE);

            try {
                WakeResult result = null;
                for (int attempt = 1; attempt <= MaxWakeAttempts; attempt++) {
                    result = SendWakeCommand(handle, path);
                    result.Attempts = attempt;

                    bool unitAttention = result.HasSense && result.SenseKey == SenseKeyUnitAttention;
                    if (result.Succeeded || !unitAttention) {
                        break;
                    }
                }

                return result;
            }
            finally {
                CloseHandle(handle);
            }
        }

        // Opens the slot through the storage class driver; works for any optical drive.
        public static void Eject(string driveLetter) {
            string path = GetDevicePath(driveLetter);
            IntPtr handle = OpenDevice(path, GENERIC_READ);

            try {
                uint bytesReturned;
                bool ok = DeviceIoControl(
                    handle,
                    IOCTL_STORAGE_EJECT_MEDIA,
                    IntPtr.Zero,
                    0,
                    IntPtr.Zero,
                    0,
                    out bytesReturned,
                    IntPtr.Zero);

                if (!ok) {
                    throw LastWin32Error("Eject on " + path);
                }
            }
            finally {
                CloseHandle(handle);
            }
        }

        private static WakeResult SendWakeCommand(IntPtr handle, string path) {
            var packet = new SptWithSense {
                Spt = new SCSI_PASS_THROUGH {
                    Length = (ushort)Marshal.SizeOf(typeof(SCSI_PASS_THROUGH)),
                    CdbLength = 7,
                    SenseInfoLength = 32,
                    DataIn = SCSI_IOCTL_DATA_UNSPECIFIED,
                    DataTransferLength = 0,
                    TimeOutValue = 10,
                    DataBufferOffset = UIntPtr.Zero,
                    SenseInfoOffset = (uint)Marshal.OffsetOf(typeof(SptWithSense), "Sense").ToInt32(),
                    Cdb = new byte[16]
                },
                Sense = new byte[32]
            };

            packet.Spt.Cdb[0] = 0xEA;
            packet.Spt.Cdb[6] = 0x01;

            int size = Marshal.SizeOf(typeof(SptWithSense));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(packet, buffer, false);
                uint bytesReturned;
                bool ok = DeviceIoControl(
                    handle,
                    IOCTL_SCSI_PASS_THROUGH,
                    buffer,
                    size,
                    buffer,
                    size,
                    out bytesReturned,
                    IntPtr.Zero);

                if (!ok) {
                    throw LastWin32Error("Wake command on " + path);
                }

                var reply = Marshal.PtrToStructure<SptWithSense>(buffer);
                var result = new WakeResult {
                    DevicePath = path,
                    ScsiStatus = reply.Spt.ScsiStatus,
                    BytesReturned = bytesReturned,
                    SenseDataHex = BitConverter.ToString(reply.Sense)
                };

                if (!result.Succeeded) {
                    DecodeSense(reply.Sense, result);
                }

                return result;
            }
            finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        // Reads the sense key, ASC and ASCQ from fixed-format (0x70/0x71) or
        // descriptor-format (0x72/0x73) sense data. Other formats leave HasSense false.
        private static void DecodeSense(byte[] sense, WakeResult result) {
            int responseCode = sense[0] & 0x7F;

            if (responseCode == 0x70 || responseCode == 0x71) {
                result.HasSense = true;
                result.SenseKey = (byte)(sense[2] & 0x0F);
                result.AdditionalSenseCode = sense[12];
                result.AdditionalSenseCodeQualifier = sense[13];
            }
            else if (responseCode == 0x72 || responseCode == 0x73) {
                result.HasSense = true;
                result.SenseKey = (byte)(sense[1] & 0x0F);
                result.AdditionalSenseCode = sense[2];
                result.AdditionalSenseCodeQualifier = sense[3];
            }
        }

        private static string GetDevicePath(string driveLetter) {
            if (string.IsNullOrWhiteSpace(driveLetter)) {
                throw new ArgumentException("A drive letter is required.", "driveLetter");
            }

            return @"\\.\" + driveLetter.Trim().TrimEnd(':') + ":";
        }

        private static IntPtr OpenDevice(string path, uint access) {
            IntPtr handle = CreateFile(
                path,
                access,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                IntPtr.Zero,
                OPEN_EXISTING,
                0,
                IntPtr.Zero);

            if (handle == INVALID_HANDLE_VALUE) {
                throw LastWin32Error("Opening " + path);
            }

            return handle;
        }

        // Builds an exception whose message keeps the Windows error text (for example
        // "Access is denied."), which Win32Exception(int, string) would otherwise replace.
        private static Win32Exception LastWin32Error(string action) {
            int code = Marshal.GetLastWin32Error();
            string reason = new Win32Exception(code).Message;
            return new Win32Exception(code, action + " failed: " + reason);
        }
    }

    // Matches a window's title bar to the app theme through DWM attributes.
    public static class TitleBar {
        private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
        private const int DWMWA_CAPTION_COLOR = 35;

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

        // Returns false when Windows ignores an attribute (caption colors need Windows 11).
        // The window works either way; only the title bar color differs.
        public static bool Apply(IntPtr hwnd, bool dark, int red, int green, int blue) {
            int darkValue = dark ? 1 : 0;
            int colorRef = red | (green << 8) | (blue << 16);
            bool darkApplied = DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ref darkValue, sizeof(int)) == 0;
            bool colorApplied = DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ref colorRef, sizeof(int)) == 0;
            return darkApplied && colorApplied;
        }
    }
}
