[CmdletBinding()]
param(
    [string]$DriveLetter,
    [switch]$NoAutoElevate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Restart-Elevated {
    param([string[]]$ForwardArgs)

    $argumentList = @(
        "-NoProfile"
        "-ExecutionPolicy"
        "Bypass"
        "-File"
        ('"{0}"' -f $PSCommandPath)
    ) + $ForwardArgs

    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argumentList | Out-Null
}

function Get-AppleSuperDrive {
    $drives = Get-CimInstance Win32_CDROMDrive | Where-Object {
        $_.Caption -like "*Apple SuperDrive*" -or
        $_.Name -like "*Apple SuperDrive*" -or
        $_.PNPDeviceID -like "USBSTOR\\CDROM&VEN_APPLE&PROD_SUPERDRIVE*"
    }

    if ($DriveLetter) {
        $normalized = "{0}:" -f $DriveLetter.Trim().TrimEnd(":")
        $drives = $drives | Where-Object { $_.Drive -eq $normalized }
    }

    return @($drives)
}

if (-not (Test-IsAdministrator) -and -not $NoAutoElevate) {
    $forwardArgs = @()
    if ($DriveLetter) {
        $forwardArgs += @("-DriveLetter", $DriveLetter)
    }
    if ($NoAutoElevate) {
        $forwardArgs += "-NoAutoElevate"
    }
    Restart-Elevated -ForwardArgs $forwardArgs
    return
}

if (-not ("AppleSuperDrive.Native" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace AppleSuperDrive {
    public sealed class WakeResult {
        public string DevicePath { get; set; }
        public byte ScsiStatus { get; set; }
        public uint BytesReturned { get; set; }
        public string SenseDataHex { get; set; }
    }

    public static class Native {
        private const uint IOCTL_SCSI_PASS_THROUGH = 0x0004D004;
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

        public static WakeResult Wake(string driveLetter) {
            if (string.IsNullOrWhiteSpace(driveLetter)) {
                throw new ArgumentException("driveLetter");
            }

            string normalized = driveLetter.Trim().TrimEnd(':') + ":";
            string path = @"\\.\" + normalized;

            IntPtr handle = CreateFile(
                path,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                IntPtr.Zero,
                OPEN_EXISTING,
                0,
                IntPtr.Zero);

            if (handle == INVALID_HANDLE_VALUE) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateFile failed for " + path);
            }

            try {
                var packet = new SptWithSense {
                    Spt = new SCSI_PASS_THROUGH {
                        Length = (ushort)Marshal.SizeOf(typeof(SCSI_PASS_THROUGH)),
                        CdbLength = 7,
                        SenseInfoLength = 32,
                        DataIn = 2,
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

                    var result = Marshal.PtrToStructure<SptWithSense>(buffer);
                    if (!ok) {
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "DeviceIoControl failed");
                    }

                    return new WakeResult {
                        DevicePath = path,
                        ScsiStatus = result.Spt.ScsiStatus,
                        BytesReturned = bytesReturned,
                        SenseDataHex = BitConverter.ToString(result.Sense)
                    };
                }
                finally {
                    Marshal.FreeHGlobal(buffer);
                }
            }
            finally {
                CloseHandle(handle);
            }
        }
    }
}
'@
}

$targets = @(Get-AppleSuperDrive)
if ($targets.Count -eq 0) {
    throw "No Apple USB SuperDrive was found."
}

$results = foreach ($target in $targets) {
    $wake = [AppleSuperDrive.Native]::Wake($target.Drive)
    [pscustomobject]@{
        Drive        = $target.Drive
        Caption      = $target.Caption
        Status       = $target.Status
        DevicePath   = $wake.DevicePath
        ScsiStatus   = $wake.ScsiStatus
        BytesReturned = $wake.BytesReturned
        SenseDataHex = $wake.SenseDataHex
    }
}

$results | Format-Table -AutoSize
