[CmdletBinding()]
param(
    [switch]$NoAutoElevate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Restart-Elevated {
    param([string[]]$ForwardArgs)

    $argumentList = @(
        '-NoProfile'
        '-ExecutionPolicy'
        'Bypass'
        '-STA'
        '-File'
        ('"{0}"' -f $PSCommandPath)
    ) + $ForwardArgs

    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argumentList | Out-Null
}

function Get-AppleSuperDrive {
    $drives = Get-CimInstance Win32_CDROMDrive | Where-Object {
        $_.Caption -like '*Apple SuperDrive*' -or
        $_.Name -like '*Apple SuperDrive*' -or
        $_.PNPDeviceID -like 'USBSTOR\CDROM&VEN_APPLE&PROD_SUPERDRIVE*'
    }

    return @($drives)
}

function Get-DriveSnapshot {
    $all = @(Get-CimInstance Win32_CDROMDrive)
    $apple = @(Get-AppleSuperDrive)
    $primary = if ($apple.Count -gt 0) { $apple[0] } else { $null }

    [pscustomobject]@{
        AllDrives    = $all
        AppleDrives  = $apple
        PrimaryDrive = $primary
    }
}

function Format-BoolYesNo {
    param([object]$Value)

    if ($null -eq $Value) {
        return 'Unknown'
    }

    if ($Value -is [bool]) {
        return $(if ($Value) { 'Yes' } else { 'No' })
    }

    return [string]$Value
}

function Format-Bytes {
    param([object]$Value)

    if ($null -eq $Value) {
        return 'Unknown'
    }

    [double]$size = [double]$Value
    $units = @('B', 'KB', 'MB', 'GB', 'TB')
    $index = 0
    while ($size -ge 1024 -and $index -lt ($units.Length - 1)) {
        $size /= 1024
        $index++
    }

    if ($index -eq 0) {
        return ('{0:N0} {1}' -f $size, $units[$index])
    }

    return ('{0:N1} {1}' -f $size, $units[$index])
}

function Get-DriveMediaState {
    param($Drive)

    if ($null -eq $Drive -or [string]::IsNullOrWhiteSpace($Drive.Drive)) {
        return $null
    }

    $root = '{0}:\' -f $Drive.Drive.Trim().TrimEnd(':')
    $driveInfo = [System.IO.DriveInfo]::GetDrives() | Where-Object {
        $_.Name -eq $root
    } | Select-Object -First 1

    if ($null -eq $driveInfo) {
        return [pscustomobject]@{
            Root               = $root
            DriveType          = 'CDRom'
            IsReady            = $false
            VolumeLabel        = ''
            DriveFormat        = ''
            TotalSize          = $null
            AvailableFreeSpace = $null
        }
    }

    $isReady = $driveInfo.IsReady
    $volumeLabel = ''
    $driveFormat = ''
    $totalSize = $null
    $availableFreeSpace = $null

    if ($isReady) {
        $volumeLabel = $driveInfo.VolumeLabel
        $driveFormat = $driveInfo.DriveFormat
        $totalSize = $driveInfo.TotalSize
        $availableFreeSpace = $driveInfo.AvailableFreeSpace
    }

    return [pscustomobject]@{
        Root               = $driveInfo.Name
        DriveType          = [string]$driveInfo.DriveType
        IsReady            = $isReady
        VolumeLabel        = $volumeLabel
        DriveFormat        = $driveFormat
        TotalSize          = $totalSize
        AvailableFreeSpace = $availableFreeSpace
    }
}

function Get-StateSummary {
    param($State)

    if ($null -eq $State -or $null -eq $State.Drive) {
        return 'No Apple SuperDrive detected.'
    }

    if ($State.Media -and $State.Media.IsReady) {
        $label = if ([string]::IsNullOrWhiteSpace($State.Media.VolumeLabel)) {
            'unlabeled media'
        }
        else {
            $State.Media.VolumeLabel
        }

        return ('Media ready on {0} ({1}).' -f $State.Media.Root, $label)
    }

    if ([string]::IsNullOrWhiteSpace($State.Drive.Drive)) {
        return 'Drive detected, but Windows has not assigned a drive letter yet.'
    }

    return ('Drive {0} is connected, but no readable disc is mounted.' -f $State.Drive.Drive)
}

if (-not $NoAutoElevate -and -not (Test-IsAdministrator)) {
    Restart-Elevated -ForwardArgs @('-NoAutoElevate')
    return
}

if (-not ('AppleSuperDrive.Native' -as [type])) {
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

function Invoke-SuperDriveWake {
    param([Parameter(Mandatory)][string]$DriveLetter)

    [AppleSuperDrive.Native]::Wake($DriveLetter)
}

function Invoke-SuperDriveEject {
    param([Parameter(Mandatory)]$Drive)

    $driveLetter = $Drive.Drive
    if ([string]::IsNullOrWhiteSpace($driveLetter)) {
        throw "Drive does not expose a drive letter."
    }

    try {
        $instance = Get-CimInstance Win32_CDROMDrive | Where-Object {
            $_.DeviceID -eq $Drive.DeviceID -or $_.Drive -eq $Drive.Drive
        } | Select-Object -First 1

        if ($instance) {
            [void](Invoke-CimMethod -InputObject $instance -MethodName Eject -ErrorAction Stop)
            return
        }
    }
    catch {
        # Fall through to the shell-based verb path below.
    }

    $shell = New-Object -ComObject Shell.Application
    $namespace = $shell.Namespace(17)
    if ($namespace) {
        $item = $namespace.ParseName($driveLetter)
        if ($item) {
            $verbs = @($item.Verbs())
            $ejectVerb = $verbs | Where-Object {
                ($_.Name -replace '&', '') -match 'Eject'
            } | Select-Object -First 1

            if ($ejectVerb) {
                $ejectVerb.DoIt()
                return
            }
        }
    }

    throw "No eject method was available for $driveLetter."
}

function Open-DriveLocation {
    param([Parameter(Mandatory)][string]$Root)

    Start-Process -FilePath 'explorer.exe' -ArgumentList $Root | Out-Null
}

if (-not ('System.Windows.Forms.Form' -as [type])) {
    Add-Type -AssemblyName System.Windows.Forms
}
if (-not ('System.Drawing.Point' -as [type])) {
    Add-Type -AssemblyName System.Drawing
}

[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

$script:UiColors = @{
    Background = [System.Drawing.Color]::FromArgb(243, 246, 250)
    Surface    = [System.Drawing.Color]::FromArgb(255, 255, 255)
    Header     = [System.Drawing.Color]::FromArgb(23, 37, 84)
    Accent     = [System.Drawing.Color]::FromArgb(37, 99, 235)
    AccentSoft = [System.Drawing.Color]::FromArgb(219, 234, 254)
    Success    = [System.Drawing.Color]::FromArgb(22, 163, 74)
    Warning    = [System.Drawing.Color]::FromArgb(217, 119, 6)
    Danger     = [System.Drawing.Color]::FromArgb(220, 38, 38)
    Slate      = [System.Drawing.Color]::FromArgb(71, 85, 105)
    Muted      = [System.Drawing.Color]::FromArgb(100, 116, 139)
    Border     = [System.Drawing.Color]::FromArgb(203, 213, 225)
    LightText  = [System.Drawing.Color]::FromArgb(226, 232, 240)
    SurfaceAlt = [System.Drawing.Color]::FromArgb(248, 250, 252)
}

$script:Form = $null
$script:LogBox = $null
$script:StatusLine = $null
$script:HeaderTitle = $null
$script:HeaderSubtitle = $null
$script:BadgeLabel = $null
$script:DriveValue = $null
$script:CaptionValue = $null
$script:StatusValue = $null
$script:AdminValue = $null
$script:MediaValue = $null
$script:VolumeValue = $null
$script:FormatValue = $null
$script:CapacityValue = $null
$script:RootValue = $null
$script:RefreshButton = $null
$script:WakeButton = $null
$script:EjectButton = $null
$script:OpenButton = $null
$script:CopyButton = $null
$script:Busy = $false

function Set-StatusLine {
    param([string]$Text)

    if ($script:StatusLine) {
        $script:StatusLine.Text = $Text
    }
}

function Write-Log {
    param([Parameter(Mandatory)][string]$Message)

    $line = '{0}  {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message
    Set-StatusLine -Text $Message

    if ($script:LogBox) {
        if ($script:LogBox.TextLength -gt 0) {
            $script:LogBox.AppendText([Environment]::NewLine)
        }

        $script:LogBox.AppendText($line)
        $script:LogBox.SelectionStart = $script:LogBox.TextLength
        $script:LogBox.ScrollToCaret()
    }
}

function Set-StatusText {
    param(
        [System.Windows.Forms.Label]$Label,
        [string]$Value
    )

    if ($Label) {
        $Label.Text = $Value
    }
}

function Set-BadgeState {
    param(
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][System.Drawing.Color]$BackColor
    )

    if ($script:BadgeLabel) {
        $script:BadgeLabel.Text = $Text
        $script:BadgeLabel.BackColor = $BackColor
    }
}

function Update-ActionButtons {
    param($State)

    $canActOnDrive = $false
    $canOpenMedia = $false

    if ($State -and $State.Drive -and -not [string]::IsNullOrWhiteSpace($State.Drive.Drive)) {
        $canActOnDrive = $true
    }

    if ($State -and $State.Media -and $State.Media.IsReady) {
        $canOpenMedia = $true
    }

    if ($script:RefreshButton) {
        $script:RefreshButton.Enabled = -not $script:Busy
    }
    if ($script:WakeButton) {
        $script:WakeButton.Enabled = (-not $script:Busy) -and $canActOnDrive
    }
    if ($script:EjectButton) {
        $script:EjectButton.Enabled = (-not $script:Busy) -and $canActOnDrive
    }
    if ($script:OpenButton) {
        $script:OpenButton.Enabled = (-not $script:Busy) -and $canOpenMedia
    }
    if ($script:CopyButton) {
        $script:CopyButton.Enabled = -not $script:Busy
    }
}

function Get-DiagnosticsText {
    $snapshot = Get-DriveSnapshot
    $drive = $snapshot.PrimaryDrive
    $media = Get-DriveMediaState -Drive $drive

    $lines = @(
        'Apple SuperDrive Helper diagnostics'
        ('Timestamp: {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'))
        ('Computer: {0}' -f $env:COMPUTERNAME)
        ('RunningElevated: {0}' -f (Format-BoolYesNo (Test-IsAdministrator)))
        ('AppleDriveCount: {0}' -f $snapshot.AppleDrives.Count)
        ('OpticalDriveCount: {0}' -f $snapshot.AllDrives.Count)
        ''
    )

    if ($drive) {
        $lines += @(
            ('PrimaryDrive: {0}' -f $drive.Caption)
            ('DriveLetter: {0}' -f $(if ($drive.Drive) { $drive.Drive } else { 'None' }))
            ('WindowsStatus: {0}' -f $(if ($drive.Status) { $drive.Status } else { 'Unknown' }))
            ('MediaLoaded: {0}' -f (Format-BoolYesNo $drive.MediaLoaded))
        )

        if ($media) {
            $lines += @(
                ('MediaReady: {0}' -f (Format-BoolYesNo $media.IsReady))
                ('VolumeLabel: {0}' -f $(if ($media.VolumeLabel) { $media.VolumeLabel } else { '' }))
                ('FileSystem: {0}' -f $(if ($media.DriveFormat) { $media.DriveFormat } else { '' }))
                ('Capacity: {0}' -f (Format-Bytes $media.TotalSize))
                ('MediaRoot: {0}' -f $media.Root)
            )
        }
    }
    else {
        $lines += 'PrimaryDrive: none'
    }

    $lines += ''
    $lines += 'Detected optical drives:'
    if ($snapshot.AllDrives.Count -eq 0) {
        $lines += '  none'
    }
    else {
        foreach ($optical in $snapshot.AllDrives) {
            $lines += ('  {0} | Drive={1} | Status={2} | MediaLoaded={3}' -f
                $optical.Caption,
                $(if ($optical.Drive) { $optical.Drive } else { 'None' }),
                $(if ($optical.Status) { $optical.Status } else { 'Unknown' }),
                (Format-BoolYesNo $optical.MediaLoaded))
        }
    }

    if ($script:LogBox -and $script:LogBox.TextLength -gt 0) {
        $lines += ''
        $lines += 'Recent log:'
        $lines += @($script:LogBox.Lines | Select-Object -Last 20)
    }

    return ($lines -join [Environment]::NewLine)
}

function Set-BusyState {
    param([bool]$IsBusy)

    $script:Busy = $IsBusy
    if ($script:Form) {
        $script:Form.UseWaitCursor = $IsBusy
        $script:Form.Cursor = $(if ($IsBusy) {
                [System.Windows.Forms.Cursors]::WaitCursor
            }
            else {
                [System.Windows.Forms.Cursors]::Default
            })
    }

    if ($IsBusy) {
        Set-StatusLine -Text 'Working...'
        Update-ActionButtons -State $null
    }
}

function Refresh-DriveStatus {
    $snapshot = Get-DriveSnapshot
    $drive = $snapshot.PrimaryDrive
    $media = Get-DriveMediaState -Drive $drive

    Set-StatusText $script:AdminValue (Format-BoolYesNo (Test-IsAdministrator))

    if ($null -eq $drive) {
        Set-StatusText $script:DriveValue 'None detected'
        Set-StatusText $script:CaptionValue 'No Apple SuperDrive detected'
        Set-StatusText $script:StatusValue 'Unknown'
        Set-StatusText $script:MediaValue 'No'
        Set-StatusText $script:VolumeValue 'No disc mounted'
        Set-StatusText $script:FormatValue 'Unknown'
        Set-StatusText $script:CapacityValue 'Unknown'
        Set-StatusText $script:RootValue 'Unavailable'

        if ($script:HeaderTitle) {
            $script:HeaderTitle.Text = 'No Apple SuperDrive detected'
        }
        if ($script:HeaderSubtitle) {
            $script:HeaderSubtitle.Text = 'Connect the drive directly over USB, then press Refresh.'
        }

        Set-BadgeState -Text 'Not detected' -BackColor $script:UiColors.Danger

        $state = [pscustomobject]@{
            Snapshot = $snapshot
            Drive    = $null
            Media    = $null
        }

        Update-ActionButtons -State $state
        return $state
    }

    $driveLetter = if ($drive.Drive) { $drive.Drive } else { 'No drive letter' }
    $volumeLabel = 'No disc mounted'
    $fileSystem = 'Unknown'
    $capacity = 'Unknown'
    $root = 'Unavailable'

    if ($media) {
        $root = $media.Root
        if ($media.IsReady) {
            $volumeLabel = if ([string]::IsNullOrWhiteSpace($media.VolumeLabel)) {
                '(No label)'
            }
            else {
                $media.VolumeLabel
            }

            $fileSystem = if ([string]::IsNullOrWhiteSpace($media.DriveFormat)) {
                'Unknown'
            }
            else {
                $media.DriveFormat
            }

            $capacity = Format-Bytes $media.TotalSize
        }
    }

    Set-StatusText $script:DriveValue $driveLetter
    Set-StatusText $script:CaptionValue $(if ($drive.Caption) { $drive.Caption } else { $drive.Name })
    Set-StatusText $script:StatusValue $(if ($drive.Status) { $drive.Status } else { 'Unknown' })
    Set-StatusText $script:MediaValue (Format-BoolYesNo $drive.MediaLoaded)
    Set-StatusText $script:VolumeValue $volumeLabel
    Set-StatusText $script:FormatValue $fileSystem
    Set-StatusText $script:CapacityValue $capacity
    Set-StatusText $script:RootValue $root

    if ($media -and $media.IsReady) {
        if ($script:HeaderTitle) {
            $script:HeaderTitle.Text = 'Media ready in Apple SuperDrive'
        }
        if ($script:HeaderSubtitle) {
            $script:HeaderSubtitle.Text = ('{0} is mounted at {1}.' -f $volumeLabel, $root)
        }

        Set-BadgeState -Text 'Media ready' -BackColor $script:UiColors.Success
    }
    elseif ($drive.Drive) {
        if ($script:HeaderTitle) {
            $script:HeaderTitle.Text = 'Apple SuperDrive connected'
        }
        if ($script:HeaderSubtitle) {
            $script:HeaderSubtitle.Text = ('Drive {0} is present, but no readable disc is mounted.' -f $drive.Drive)
        }

        Set-BadgeState -Text 'Connected' -BackColor $script:UiColors.Warning
    }
    else {
        if ($script:HeaderTitle) {
            $script:HeaderTitle.Text = 'Apple SuperDrive detected'
        }
        if ($script:HeaderSubtitle) {
            $script:HeaderSubtitle.Text = 'Windows sees the hardware, but no drive letter is assigned yet.'
        }

        Set-BadgeState -Text 'No mount' -BackColor $script:UiColors.Warning
    }

    $state = [pscustomobject]@{
        Snapshot = $snapshot
        Drive    = $drive
        Media    = $media
    }

    Update-ActionButtons -State $state
    return $state
}

function Invoke-UiAction {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action
    )

    if ($script:Busy) {
        return
    }

    Set-BusyState -IsBusy $true
    try {
        Write-Log $Name
        & $Action
    }
    catch {
        Write-Log ('ERROR: {0}' -f $_.Exception.Message)
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            'Apple SuperDrive',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
    finally {
        Set-BusyState -IsBusy $false
        [void](Refresh-DriveStatus)
    }
}

function New-CardPanel {
    param([Parameter(Mandatory)][string]$Title)

    $panel = New-Object System.Windows.Forms.Panel
    $panel.Dock = 'Fill'
    $panel.BackColor = $script:UiColors.Surface
    $panel.BorderStyle = 'FixedSingle'
    $panel.Padding = New-Object System.Windows.Forms.Padding(16)

    $layout = New-Object System.Windows.Forms.TableLayoutPanel
    $layout.Dock = 'Fill'
    $layout.BackColor = $script:UiColors.Surface
    $layout.ColumnCount = 1
    $layout.RowCount = 2
    $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
    $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
    $panel.Controls.Add($layout)

    $titleLabel = New-Object System.Windows.Forms.Label
    $titleLabel.Text = $Title
    $titleLabel.AutoSize = $true
    $titleLabel.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
    $titleLabel.ForeColor = $script:UiColors.Header
    $titleLabel.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 12)
    $layout.Controls.Add($titleLabel, 0, 0)

    $body = New-Object System.Windows.Forms.TableLayoutPanel
    $body.Dock = 'Fill'
    $body.BackColor = $script:UiColors.Surface
    $body.ColumnCount = 1
    $body.RowCount = 1
    $body.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
    $layout.Controls.Add($body, 0, 1)

    return [pscustomobject]@{
        Panel = $panel
        Body  = $body
    }
}

function Add-InfoRow {
    param(
        [Parameter(Mandatory)][System.Windows.Forms.TableLayoutPanel]$Table,
        [Parameter(Mandatory)][string]$LabelText,
        [Parameter(Mandatory)][int]$RowIndex,
        [Parameter(Mandatory)][ref]$ValueRef
    )

    $nameLabel = New-Object System.Windows.Forms.Label
    $nameLabel.Text = $LabelText
    $nameLabel.AutoSize = $true
    $nameLabel.Font = New-Object System.Drawing.Font('Segoe UI', 9)
    $nameLabel.ForeColor = $script:UiColors.Muted
    $nameLabel.Margin = New-Object System.Windows.Forms.Padding(0, 4, 10, 8)

    $valueLabel = New-Object System.Windows.Forms.Label
    $valueLabel.Text = ''
    $valueLabel.AutoSize = $true
    $valueLabel.Font = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)
    $valueLabel.ForeColor = $script:UiColors.Slate
    $valueLabel.MaximumSize = New-Object System.Drawing.Size(350, 0)
    $valueLabel.Margin = New-Object System.Windows.Forms.Padding(0, 4, 0, 8)

    $Table.Controls.Add($nameLabel, 0, $RowIndex)
    $Table.Controls.Add($valueLabel, 1, $RowIndex)
    $ValueRef.Value = $valueLabel
}

function New-ActionButton {
    param(
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][System.Drawing.Color]$BackColor,
        [System.Drawing.Color]$ForeColor = [System.Drawing.Color]::White
    )

    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Width = 260
    $button.Height = 38
    $button.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 8)
    $button.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = $BackColor
    $button.ForeColor = $ForeColor
    $button.Font = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)
    $button.Cursor = [System.Windows.Forms.Cursors]::Hand
    $button.UseVisualStyleBackColor = $false

    return $button
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Apple SuperDrive Helper'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(980, 640)
$form.MinimumSize = New-Object System.Drawing.Size(920, 580)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$form.BackColor = $script:UiColors.Background
$form.KeyPreview = $true
$script:Form = $form

$main = New-Object System.Windows.Forms.TableLayoutPanel
$main.Dock = 'Fill'
$main.BackColor = $script:UiColors.Background
$main.ColumnCount = 1
$main.RowCount = 3
$main.Padding = New-Object System.Windows.Forms.Padding(18)
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
$form.Controls.Add($main)

$headerCard = New-Object System.Windows.Forms.Panel
$headerCard.Dock = 'Top'
$headerCard.Height = 120
$headerCard.BackColor = $script:UiColors.Header
$headerCard.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 14)
$headerCard.Padding = New-Object System.Windows.Forms.Padding(22, 18, 22, 18)
$main.Controls.Add($headerCard, 0, 0)

$headerLayout = New-Object System.Windows.Forms.TableLayoutPanel
$headerLayout.Dock = 'Fill'
$headerLayout.BackColor = $script:UiColors.Header
$headerLayout.ColumnCount = 2
$headerLayout.RowCount = 1
$headerLayout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$headerLayout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::AutoSize)))
$headerCard.Controls.Add($headerLayout)

$headerLeft = New-Object System.Windows.Forms.TableLayoutPanel
$headerLeft.Dock = 'Fill'
$headerLeft.BackColor = $script:UiColors.Header
$headerLeft.ColumnCount = 1
$headerLeft.RowCount = 2
$headerLeft.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
$headerLeft.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
$headerLayout.Controls.Add($headerLeft, 0, 0)

$script:HeaderTitle = New-Object System.Windows.Forms.Label
$script:HeaderTitle.Text = 'Apple SuperDrive Helper'
$script:HeaderTitle.AutoSize = $true
$script:HeaderTitle.Font = New-Object System.Drawing.Font('Segoe UI', 22, [System.Drawing.FontStyle]::Bold)
$script:HeaderTitle.ForeColor = [System.Drawing.Color]::White
$script:HeaderTitle.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 6)
$headerLeft.Controls.Add($script:HeaderTitle, 0, 0)

$script:HeaderSubtitle = New-Object System.Windows.Forms.Label
$script:HeaderSubtitle.Text = 'Detect, wake, eject, and diagnose the Apple USB SuperDrive from one window.'
$script:HeaderSubtitle.AutoSize = $true
$script:HeaderSubtitle.MaximumSize = New-Object System.Drawing.Size(620, 0)
$script:HeaderSubtitle.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$script:HeaderSubtitle.ForeColor = $script:UiColors.LightText
$script:HeaderSubtitle.Margin = New-Object System.Windows.Forms.Padding(0)
$headerLeft.Controls.Add($script:HeaderSubtitle, 0, 1)

$headerRight = New-Object System.Windows.Forms.FlowLayoutPanel
$headerRight.AutoSize = $true
$headerRight.WrapContents = $false
$headerRight.FlowDirection = [System.Windows.Forms.FlowDirection]::TopDown
$headerRight.BackColor = $script:UiColors.Header
$headerRight.Margin = New-Object System.Windows.Forms.Padding(0)
$headerLayout.Controls.Add($headerRight, 1, 0)

$script:BadgeLabel = New-Object System.Windows.Forms.Label
$script:BadgeLabel.Text = 'Ready'
$script:BadgeLabel.AutoSize = $true
$script:BadgeLabel.Padding = New-Object System.Windows.Forms.Padding(14, 6, 14, 6)
$script:BadgeLabel.Margin = New-Object System.Windows.Forms.Padding(0, 4, 0, 6)
$script:BadgeLabel.Font = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)
$script:BadgeLabel.ForeColor = [System.Drawing.Color]::White
$script:BadgeLabel.BackColor = $script:UiColors.Accent
$headerRight.Controls.Add($script:BadgeLabel)

$badgeHint = New-Object System.Windows.Forms.Label
$badgeHint.Text = 'Live status'
$badgeHint.AutoSize = $true
$badgeHint.Font = New-Object System.Drawing.Font('Segoe UI', 9)
$badgeHint.ForeColor = $script:UiColors.LightText
$headerRight.Controls.Add($badgeHint)

$content = New-Object System.Windows.Forms.TableLayoutPanel
$content.Dock = 'Fill'
$content.BackColor = $script:UiColors.Background
$content.ColumnCount = 2
$content.RowCount = 1
$content.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 340)))
$content.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$main.Controls.Add($content, 0, 1)

$sidebar = New-Object System.Windows.Forms.TableLayoutPanel
$sidebar.Dock = 'Fill'
$sidebar.BackColor = $script:UiColors.Background
$sidebar.ColumnCount = 1
$sidebar.RowCount = 3
$sidebar.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 200)))
$sidebar.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 220)))
$sidebar.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$content.Controls.Add($sidebar, 0, 0)

$summaryCard = New-CardPanel -Title 'Drive summary'
$summaryCard.Panel.Margin = New-Object System.Windows.Forms.Padding(0, 0, 12, 12)
$summaryCard.Body.ColumnCount = 2
$summaryCard.Body.RowCount = 4
$summaryCard.Body.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 110)))
$summaryCard.Body.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
for ($i = 0; $i -lt 4; $i++) {
    $summaryCard.Body.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
}
Add-InfoRow -Table $summaryCard.Body -LabelText 'Drive' -RowIndex 0 -ValueRef ([ref]$script:DriveValue)
Add-InfoRow -Table $summaryCard.Body -LabelText 'Device' -RowIndex 1 -ValueRef ([ref]$script:CaptionValue)
Add-InfoRow -Table $summaryCard.Body -LabelText 'Status' -RowIndex 2 -ValueRef ([ref]$script:StatusValue)
Add-InfoRow -Table $summaryCard.Body -LabelText 'Admin' -RowIndex 3 -ValueRef ([ref]$script:AdminValue)
$sidebar.Controls.Add($summaryCard.Panel, 0, 0)

$mediaCard = New-CardPanel -Title 'Disc media'
$mediaCard.Panel.Margin = New-Object System.Windows.Forms.Padding(0, 0, 12, 12)
$mediaCard.Body.ColumnCount = 2
$mediaCard.Body.RowCount = 5
$mediaCard.Body.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 110)))
$mediaCard.Body.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
for ($i = 0; $i -lt 5; $i++) {
    $mediaCard.Body.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
}
Add-InfoRow -Table $mediaCard.Body -LabelText 'Loaded' -RowIndex 0 -ValueRef ([ref]$script:MediaValue)
Add-InfoRow -Table $mediaCard.Body -LabelText 'Label' -RowIndex 1 -ValueRef ([ref]$script:VolumeValue)
Add-InfoRow -Table $mediaCard.Body -LabelText 'Format' -RowIndex 2 -ValueRef ([ref]$script:FormatValue)
Add-InfoRow -Table $mediaCard.Body -LabelText 'Capacity' -RowIndex 3 -ValueRef ([ref]$script:CapacityValue)
Add-InfoRow -Table $mediaCard.Body -LabelText 'Root' -RowIndex 4 -ValueRef ([ref]$script:RootValue)
$sidebar.Controls.Add($mediaCard.Panel, 0, 1)

$actionsCard = New-CardPanel -Title 'Actions'
$actionsCard.Panel.Margin = New-Object System.Windows.Forms.Padding(0, 0, 12, 0)
$actionsFlow = New-Object System.Windows.Forms.FlowLayoutPanel
$actionsFlow.Dock = 'Fill'
$actionsFlow.FlowDirection = [System.Windows.Forms.FlowDirection]::TopDown
$actionsFlow.WrapContents = $false
$actionsFlow.AutoScroll = $true
$actionsFlow.BackColor = $script:UiColors.Surface
$actionsCard.Body.Controls.Add($actionsFlow, 0, 0)

$script:WakeButton = New-ActionButton -Text 'Wake/Enable' -BackColor $script:UiColors.Accent
$script:WakeButton.Add_Click({
    Invoke-UiAction -Name 'Wake/Enable' -Action {
        $state = Refresh-DriveStatus
        if ($null -eq $state.Drive) {
            throw 'No Apple SuperDrive was detected.'
        }

        if ([string]::IsNullOrWhiteSpace($state.Drive.Drive)) {
            throw 'The detected SuperDrive does not expose a drive letter.'
        }

        $wake = Invoke-SuperDriveWake -DriveLetter $state.Drive.Drive
        Write-Log ('Wake sent to {0}.' -f $wake.DevicePath)
        Write-Log ('SCSI status {0}, bytes returned {1}.' -f $wake.ScsiStatus, $wake.BytesReturned)
    }
})

$script:RefreshButton = New-ActionButton -Text 'Refresh' -BackColor $script:UiColors.Slate
$script:RefreshButton.Add_Click({
    Invoke-UiAction -Name 'Refresh' -Action {
        $state = Refresh-DriveStatus
        Write-Log (Get-StateSummary -State $state)
    }
})

$script:OpenButton = New-ActionButton -Text 'Open Drive' -BackColor $script:UiColors.Success
$script:OpenButton.Add_Click({
    Invoke-UiAction -Name 'Open Drive' -Action {
        $state = Refresh-DriveStatus
        if (-not $state.Media -or -not $state.Media.IsReady) {
            throw 'No readable disc is mounted.'
        }

        Open-DriveLocation -Root $state.Media.Root
        Write-Log ('Opened {0} in Explorer.' -f $state.Media.Root)
    }
})

$script:EjectButton = New-ActionButton -Text 'Eject' -BackColor $script:UiColors.Warning
$script:EjectButton.Add_Click({
    Invoke-UiAction -Name 'Eject' -Action {
        $state = Refresh-DriveStatus
        if ($null -eq $state.Drive) {
            throw 'No Apple SuperDrive was detected.'
        }

        Invoke-SuperDriveEject -Drive $state.Drive
        Write-Log ('Eject requested for {0}.' -f $state.Drive.Drive)
    }
})

$script:CopyButton = New-ActionButton -Text 'Copy Diagnostics' -BackColor $script:UiColors.AccentSoft -ForeColor $script:UiColors.Header
$script:CopyButton.Add_Click({
    Invoke-UiAction -Name 'Copy Diagnostics' -Action {
        $report = Get-DiagnosticsText
        [System.Windows.Forms.Clipboard]::SetText($report)
        Write-Log 'Diagnostics copied to the clipboard.'
    }
})

$exitButton = New-ActionButton -Text 'Exit' -BackColor $script:UiColors.SurfaceAlt -ForeColor $script:UiColors.Header
$exitButton.FlatAppearance.BorderSize = 1
$exitButton.FlatAppearance.BorderColor = $script:UiColors.Border
$exitButton.Add_Click({ $form.Close() })

$actionsHint = New-Object System.Windows.Forms.Label
$actionsHint.Text = 'Tip: use Ctrl+R to refresh, and press Escape to close the window.'
$actionsHint.AutoSize = $true
$actionsHint.MaximumSize = New-Object System.Drawing.Size(280, 0)
$actionsHint.ForeColor = $script:UiColors.Muted
$actionsHint.Margin = New-Object System.Windows.Forms.Padding(0, 8, 0, 0)

$actionsFlow.Controls.AddRange(@(
        $script:WakeButton,
        $script:RefreshButton,
        $script:OpenButton,
        $script:EjectButton,
        $script:CopyButton,
        $exitButton,
        $actionsHint
    ))
$sidebar.Controls.Add($actionsCard.Panel, 0, 2)

$logCard = New-CardPanel -Title 'Activity log'
$logCard.Panel.Margin = New-Object System.Windows.Forms.Padding(12, 0, 0, 0)
$logCard.Body.RowCount = 2
$logCard.Body.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
$logCard.Body.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$content.Controls.Add($logCard.Panel, 1, 0)

$logHint = New-Object System.Windows.Forms.Label
$logHint.Text = 'The log records startup wake attempts, manual actions, and the latest drive state.'
$logHint.AutoSize = $true
$logHint.MaximumSize = New-Object System.Drawing.Size(520, 0)
$logHint.ForeColor = $script:UiColors.Muted
$logHint.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 10)
$logCard.Body.Controls.Add($logHint, 0, 0)

$script:LogBox = New-Object System.Windows.Forms.TextBox
$script:LogBox.Dock = 'Fill'
$script:LogBox.Multiline = $true
$script:LogBox.ScrollBars = 'Vertical'
$script:LogBox.ReadOnly = $true
$script:LogBox.WordWrap = $false
$script:LogBox.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$script:LogBox.BackColor = $script:UiColors.SurfaceAlt
$script:LogBox.ForeColor = $script:UiColors.Slate
$script:LogBox.Font = New-Object System.Drawing.Font('Consolas', 9)
$logCard.Body.Controls.Add($script:LogBox, 0, 1)

$footer = New-Object System.Windows.Forms.TableLayoutPanel
$footer.Dock = 'Fill'
$footer.BackColor = $script:UiColors.Background
$footer.ColumnCount = 2
$footer.RowCount = 1
$footer.Margin = New-Object System.Windows.Forms.Padding(0, 12, 0, 0)
$footer.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$footer.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::AutoSize)))
$main.Controls.Add($footer, 0, 2)

$script:StatusLine = New-Object System.Windows.Forms.Label
$script:StatusLine.Text = 'Ready.'
$script:StatusLine.AutoSize = $true
$script:StatusLine.ForeColor = $script:UiColors.Slate
$script:StatusLine.Margin = New-Object System.Windows.Forms.Padding(0, 6, 0, 0)
$footer.Controls.Add($script:StatusLine, 0, 0)

$footerHint = New-Object System.Windows.Forms.Label
$footerHint.Text = 'Direct USB ports are more reliable than passive adapters.'
$footerHint.AutoSize = $true
$footerHint.ForeColor = $script:UiColors.Muted
$footerHint.Margin = New-Object System.Windows.Forms.Padding(12, 6, 0, 0)
$footer.Controls.Add($footerHint, 1, 0)

$form.Add_KeyDown({
    if ($_.KeyCode -eq [System.Windows.Forms.Keys]::Escape) {
        $form.Close()
        return
    }

    if ($_.Control -and $_.KeyCode -eq [System.Windows.Forms.Keys]::R) {
        $script:RefreshButton.PerformClick()
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({
    if (-not $script:Busy) {
        [void](Refresh-DriveStatus)
    }
})
$timer.Start()

$form.Add_Shown({
    Write-Log 'Apple SuperDrive GUI started.'
    Write-Log $(if (Test-IsAdministrator) { 'Running elevated.' } else { 'Running without elevation.' })

    $state = Refresh-DriveStatus
    Write-Log (Get-StateSummary -State $state)

    try {
        if ($state.Drive -and $state.Drive.Drive -and -not ($state.Media -and $state.Media.IsReady)) {
            $wake = Invoke-SuperDriveWake -DriveLetter $state.Drive.Drive
            Write-Log ('Startup wake sent to {0}.' -f $wake.DevicePath)
            Write-Log ('SCSI status {0}, bytes returned {1}.' -f $wake.ScsiStatus, $wake.BytesReturned)

            $state = Refresh-DriveStatus
            Write-Log (Get-StateSummary -State $state)
        }
    }
    catch {
        Write-Log ('Startup wake failed: {0}' -f $_.Exception.Message)
    }
})

$form.Add_FormClosed({
    $timer.Stop()
    $timer.Dispose()
})

[void](Refresh-DriveStatus)
[void]$form.ShowDialog()
