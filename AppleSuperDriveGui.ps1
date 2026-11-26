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

    # The elevated copy only shows the window, so its console starts hidden.
    $argumentList = @(
        '-NoProfile'
        '-ExecutionPolicy'
        'Bypass'
        '-STA'
        '-WindowStyle'
        'Hidden'
        '-File'
        ('"{0}"' -f $PSCommandPath)
    ) + $ForwardArgs

    # Returns $false when the elevation prompt is declined, so the caller can keep running.
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argumentList | Out-Null
        return $true
    }
    catch {
        Write-Warning ('Could not restart with administrator rights: {0}' -f $_.Exception.Message)
        return $false
    }
}

function Get-ErrorMessage {
    param([Parameter(Mandatory)][System.Management.Automation.ErrorRecord]$ErrorRecord)

    # .NET exceptions from a method call arrive wrapped as
    # 'Exception calling "Wake" with "1" argument(s): ...'; report the inner message instead.
    $exception = $ErrorRecord.Exception
    if ($exception -is [System.Management.Automation.MethodInvocationException] -and $exception.InnerException) {
        $exception = $exception.InnerException
    }

    return $exception.Message
}

if (-not $NoAutoElevate -and -not (Test-IsAdministrator)) {
    if (Restart-Elevated -ForwardArgs @('-NoAutoElevate')) {
        return
    }

    # Elevation was declined. Keep running so status, Open disc and the log still work;
    # waking the drive then explains that administrator rights are needed.
}

# --- State ------------------------------------------------------------------------------

$script:LogPath = Join-Path $env:LOCALAPPDATA 'AppleSuperDriveHelper\gui.log'
$script:IsAdmin = Test-IsAdministrator
$script:DarkTheme = $false
$script:Palette = @{}
$script:Window = $null
$script:Timer = $null
$script:Ui = @{}
$script:NativeType = $null

$script:LastState = $null      # Latest snapshot from a background status poll.
$script:LastPollError = $null
$script:AutoWakeKey = $null    # Drive last auto-woken; cleared when it disappears.
$script:WokenKey = $null       # Drive that accepted the wake command.
$script:FailedKey = $null      # Drive whose last wake failed, for the reason in WakeError.
$script:WakeError = ''
$script:Notice = ''            # Error from the last manual action, shown under the buttons.

$script:Busy = $false
$script:BusyHeadline = ''
$script:BusyDetail = ''

# Drive queries and commands run in this pool so the window never waits on the drive.
$script:Pool = $null
$script:Jobs = New-Object System.Collections.ArrayList
$script:PollIntervalSeconds = 3
$script:PollInFlight = $false
$script:PollRequested = $false
$script:PollDiscard = $false
$script:NextPollAt = [datetime]::MinValue
$script:CopyResetAt = $null

# --- Background work ----------------------------------------------------------------------
# These script blocks run in pool runspaces, where the functions in this file do not exist,
# so each one is self-contained.

# Runs one piece of work and always returns { Value, Error } instead of throwing.
$script:WorkerScript = {
    param([string]$WorkText, [object[]]$WorkArgs)

    $ErrorActionPreference = 'Stop'
    try {
        $work = [scriptblock]::Create($WorkText)
        $value = & $work @WorkArgs
        [pscustomobject]@{ Value = $value; Error = $null }
    }
    catch {
        $exception = $_.Exception
        if ($exception -is [System.Management.Automation.MethodInvocationException] -and $null -ne $exception.InnerException) {
            $exception = $exception.InnerException
        }

        [pscustomobject]@{ Value = $null; Error = $exception.Message }
    }
}

# Reads the optical drives, the first Apple SuperDrive among them, and its disc state.
$script:DriveStateScript = {
    $all = @(Get-CimInstance Win32_CDROMDrive)
    $apple = @($all | Where-Object {
            $_.Caption -like '*Apple SuperDrive*' -or
            $_.Name -like '*Apple SuperDrive*' -or
            $_.PNPDeviceID -like 'USBSTOR\CDROM&VEN_APPLE&PROD_SUPERDRIVE*'
        })
    $drive = if ($apple.Count -gt 0) { $apple[0] } else { $null }

    $media = $null
    if ($null -ne $drive -and -not [string]::IsNullOrWhiteSpace($drive.Drive)) {
        $root = '{0}:\' -f $drive.Drive.Trim().TrimEnd(':')
        $media = [pscustomobject]@{
            Root        = $root
            IsReady     = $false
            VolumeLabel = ''
            DriveFormat = ''
            TotalSize   = $null
        }

        $driveInfo = [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.Name -eq $root } | Select-Object -First 1
        if ($null -ne $driveInfo -and $driveInfo.IsReady) {
            try {
                $media.VolumeLabel = $driveInfo.VolumeLabel
                $media.DriveFormat = $driveInfo.DriveFormat
                $media.TotalSize = $driveInfo.TotalSize
                $media.IsReady = $true
            }
            catch {
                # The disc left between IsReady and the volume reads; report it as not mounted.
                $media.IsReady = $false
            }
        }
    }

    [pscustomobject]@{
        AllDrives   = $all
        AppleDrives = $apple
        Drive       = $drive
        Media       = $media
    }
}

$script:WakeWork = {
    param($Native, [string]$DriveLetter)

    $Native::Wake($DriveLetter)
}

$script:EjectWork = {
    param($Native, [string]$DriveLetter)

    try {
        $Native::Eject($DriveLetter)
        'ejected'
    }
    catch {
        # The SuperDrive can ignore commands until it has been woken, so wake it and retry once.
        [void]$Native::Wake($DriveLetter)
        $Native::Eject($DriveLetter)
        'ejected-after-wake'
    }
}

function Start-Work {
    param(
        [Parameter(Mandatory)][scriptblock]$Work,
        [object[]]$WorkArgs = @(),
        $Context = $null,
        [Parameter(Mandatory)][scriptblock]$OnDone
    )

    $shell = [powershell]::Create()
    $shell.RunspacePool = $script:Pool
    [void]$shell.AddScript($script:WorkerScript.ToString())
    [void]$shell.AddArgument($Work.ToString())
    [void]$shell.AddArgument($WorkArgs)

    [void]$script:Jobs.Add(@{
            Shell   = $shell
            Handle  = $shell.BeginInvoke()
            Context = $Context
            OnDone  = $OnDone
        })
}

# Hands each finished job's result to its callback on the UI thread.
function Complete-FinishedWork {
    # Walks backwards so finished jobs can be removed by index while callbacks that start
    # new work only append behind the walk.
    for ($index = $script:Jobs.Count - 1; $index -ge 0; $index--) {
        $job = $script:Jobs[$index]
        if (-not $job.Handle.IsCompleted) {
            continue
        }

        $script:Jobs.RemoveAt($index)
        $result = $null
        try {
            $output = $job.Shell.EndInvoke($job.Handle)
            if ($output.Count -gt 0) {
                $result = $output[0]
            }
        }
        catch {
            $result = [pscustomobject]@{ Value = $null; Error = (Get-ErrorMessage $_) }
        }
        finally {
            $job.Shell.Dispose()
        }

        if ($null -eq $result) {
            $result = [pscustomobject]@{ Value = $null; Error = 'The background task returned no result.' }
        }

        & $job.OnDone $result $job.Context
    }
}

# --- Formatting -----------------------------------------------------------------------

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

# Identifies one connected drive at one drive letter; $null when there is nothing to act on.
function Get-DriveKey {
    param($Drive)

    if ($null -eq $Drive -or [string]::IsNullOrWhiteSpace($Drive.Drive)) {
        return $null
    }

    return ('{0}|{1}' -f $Drive.PNPDeviceID, $Drive.Drive)
}

function Get-CurrentLetter {
    if ($null -eq $script:LastState -or $null -eq (Get-DriveKey $script:LastState.Drive)) {
        return $null
    }

    return $script:LastState.Drive.Drive
}

function Get-MediaSummary {
    param([Parameter(Mandatory)]$Media)

    $parts = @()
    $parts += $(if ([string]::IsNullOrWhiteSpace($Media.VolumeLabel)) { 'Unlabeled disc' } else { $Media.VolumeLabel })
    if (-not [string]::IsNullOrWhiteSpace($Media.DriveFormat)) {
        $parts += $Media.DriveFormat
    }
    if ($null -ne $Media.TotalSize) {
        $parts += (Format-Bytes $Media.TotalSize)
    }
    $parts += $Media.Root

    return ($parts -join (' {0} ' -f [char]0x00B7))
}

function Get-WakeStateText {
    param($Key)

    if ($null -eq $Key) {
        return 'no drive letter'
    }
    if ($Key -eq $script:WokenKey) {
        return 'accepted the wake command'
    }
    if ($Key -eq $script:FailedKey) {
        return ('wake failed: {0}' -f $script:WakeError)
    }

    return 'not woken yet'
}

# --- Logging --------------------------------------------------------------------------

function Initialize-LogFile {
    try {
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $script:LogPath) -Force)
        Set-Content -LiteralPath $script:LogPath -Encoding UTF8 -Value ('Apple SuperDrive Helper log, started {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    }
    catch {
        # The log file is a convenience for bug reports; the window works without it.
    }
}

function Write-Log {
    param([Parameter(Mandatory)][string]$Message)

    $line = '{0}  {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message
    try {
        Add-Content -LiteralPath $script:LogPath -Encoding UTF8 -Value $line
    }
    catch {
        # Same as above: a locked or unwritable log file is not worth interrupting the user.
    }

    $logBox = $script:Ui['LogBox']
    if ($null -ne $logBox) {
        if ($logBox.Text.Length -gt 0) {
            $logBox.AppendText([Environment]::NewLine)
        }
        $logBox.AppendText($line)
        $logBox.ScrollToEnd()
    }
}

function Show-FatalError {
    param([Parameter(Mandatory)][string]$Message)

    Write-Log ('Could not start: {0}' -f $Message)
    $newLine = [Environment]::NewLine
    $text = 'Apple SuperDrive Helper could not start.' + $newLine + $newLine + $Message + $newLine + $newLine + ('Log: {0}' -f $script:LogPath)

    try {
        [void][System.Windows.MessageBox]::Show($text, 'Apple SuperDrive Helper', 'OK', 'Error')
    }
    catch {
        # WPF itself did not load, so fall back to the Windows Script Host dialog.
        [void](New-Object -ComObject WScript.Shell).Popup($text, 0, 'Apple SuperDrive Helper', 16)
    }
}

# --- Status polling and automatic wake ------------------------------------------------

function Request-StatusPoll {
    $script:PollRequested = $true
}

function Start-StatusPoll {
    $script:PollRequested = $false
    $script:PollInFlight = $true
    Start-Work -Work $script:DriveStateScript -OnDone {
        param($Result, $Context)
        Complete-StatusPoll -Result $Result
    }
}

function Complete-StatusPoll {
    param([Parameter(Mandatory)]$Result)

    $script:PollInFlight = $false
    $script:NextPollAt = [datetime]::UtcNow.AddSeconds($script:PollIntervalSeconds)

    # An action started while this poll ran, so its snapshot may already be out of date.
    if ($script:PollDiscard) {
        $script:PollDiscard = $false
        $script:PollRequested = $true
        return
    }

    if ($Result.Error) {
        # A failing query repeats every few seconds, so log each distinct failure once.
        if ($Result.Error -ne $script:LastPollError) {
            Write-Log ('Could not read the drive status: {0}' -f $Result.Error)
        }
        $script:LastPollError = $Result.Error
        return
    }

    $script:LastPollError = $null
    $previous = $script:LastState
    $script:LastState = $Result.Value
    Write-StateChange -Previous $previous -Current $script:LastState

    if ($null -eq (Get-DriveKey $script:LastState.Drive)) {
        $script:AutoWakeKey = $null
        $script:WokenKey = $null
        $script:FailedKey = $null
        $script:WakeError = ''
    }

    Update-View
    Invoke-AutoWake -Reason $(if ($null -eq $previous) { 'startup' } else { 'drive connected' })
}

function Write-StateChange {
    param($Previous, [Parameter(Mandatory)]$Current)

    $oldDrive = if ($null -ne $Previous) { $Previous.Drive } else { $null }
    $newDrive = $Current.Drive
    $oldKey = Get-DriveKey $oldDrive
    $newKey = Get-DriveKey $newDrive

    if ($null -eq $Previous) {
        if ($null -eq $newDrive) {
            Write-Log 'No SuperDrive connected.'
        }
        elseif ($null -eq $newKey) {
            Write-Log 'Found a SuperDrive without a drive letter yet.'
        }
        else {
            Write-Log ('Found the SuperDrive at {0}.' -f $newDrive.Drive)
        }
    }
    elseif ($oldKey -ne $newKey) {
        if ($null -ne $newKey) {
            Write-Log ('SuperDrive connected at {0}.' -f $newDrive.Drive)
        }
        elseif ($null -eq $newDrive) {
            Write-Log 'SuperDrive disconnected.'
        }
    }

    $wasReady = ($null -ne $Previous) -and ($null -ne $Previous.Media) -and $Previous.Media.IsReady
    $isReady = ($null -ne $Current.Media) -and $Current.Media.IsReady
    if ($isReady -and -not $wasReady) {
        Write-Log ('Disc mounted: {0}.' -f (Get-MediaSummary $Current.Media))
    }
    elseif ($wasReady -and -not $isReady -and $null -ne $newKey) {
        Write-Log 'Disc removed.'
    }
}

# Wakes the drive once each time it appears, at startup or after being plugged back in,
# because the drive needs the wake command again after every reconnect.
function Invoke-AutoWake {
    param([Parameter(Mandatory)][string]$Reason)

    $key = Get-DriveKey $script:LastState.Drive
    if ($null -eq $key -or $key -eq $script:AutoWakeKey) {
        return
    }

    # Recorded before the wake so a drive that keeps failing is not retried on every poll.
    $script:AutoWakeKey = $key

    $media = $script:LastState.Media
    if ($null -ne $media -and $media.IsReady) {
        # A mounted disc shows the drive is already awake.
        $script:WokenKey = $key
        Update-View
        return
    }

    Start-WakeAction -Reason $Reason
}

# --- Actions --------------------------------------------------------------------------

function Set-Busy {
    param(
        [Parameter(Mandatory)][string]$Headline,
        [Parameter(Mandatory)][string]$Detail
    )

    $script:Busy = $true
    $script:BusyHeadline = $Headline
    $script:BusyDetail = $Detail
    $script:Notice = ''
    if ($script:PollInFlight) {
        $script:PollDiscard = $true
    }

    Update-View
}

function Clear-Busy {
    $script:Busy = $false
    Request-StatusPoll
    Update-View
}

function Start-WakeAction {
    param([Parameter(Mandatory)][string]$Reason)

    $letter = Get-CurrentLetter
    if ($script:Busy -or $null -eq $letter) {
        return
    }

    $context = @{
        Key    = Get-DriveKey $script:LastState.Drive
        Letter = $letter
        Reason = $Reason
    }

    Set-Busy -Headline 'Waking the drive' -Detail ('Sending the wake command to {0}.' -f $letter)
    Start-Work -Work $script:WakeWork -WorkArgs @($script:NativeType, $letter) -Context $context -OnDone {
        param($Result, $Context)
        Complete-WakeAction -Result $Result -Context $Context
    }
}

function Complete-WakeAction {
    param(
        [Parameter(Mandatory)]$Result,
        [Parameter(Mandatory)]$Context
    )

    if ($Result.Error) {
        Write-Log ('Wake on {0} ({1}) failed: {2}' -f $Context.Letter, $Context.Reason, $Result.Error)
        $message = $Result.Error
        if (-not $script:IsAdmin) {
            $message = 'Windows needs administrator rights to send the wake command. Close this window, start the helper again and accept the prompt.'
        }
        Set-WakeFailure -Key $Context.Key -Message $message
    }
    elseif (-not $Result.Value.Succeeded) {
        $reply = $Result.Value.Describe()
        Write-Log ('Wake on {0} ({1}) was rejected: {2}.' -f $Context.Letter, $Context.Reason, $reply)
        Set-WakeFailure -Key $Context.Key -Message ('The drive answered with {0}. Try another USB port or cable, then press Try again.' -f $reply)
    }
    else {
        Write-Log ('Wake on {0} ({1}) returned {2}.' -f $Context.Letter, $Context.Reason, $Result.Value.Describe())
        $script:WokenKey = $Context.Key
        $script:FailedKey = $null
        $script:WakeError = ''
    }

    Clear-Busy
}

function Set-WakeFailure {
    param($Key, [Parameter(Mandatory)][string]$Message)

    $script:WokenKey = $null
    $script:FailedKey = $Key
    $script:WakeError = $Message
}

function Start-EjectAction {
    $letter = Get-CurrentLetter
    if ($script:Busy -or $null -eq $letter) {
        return
    }

    Set-Busy -Headline 'Ejecting the disc' -Detail ('Asking {0} to eject.' -f $letter)
    Start-Work -Work $script:EjectWork -WorkArgs @($script:NativeType, $letter) -Context @{ Letter = $letter } -OnDone {
        param($Result, $Context)
        Complete-EjectAction -Result $Result -Context $Context
    }
}

function Complete-EjectAction {
    param(
        [Parameter(Mandatory)]$Result,
        [Parameter(Mandatory)]$Context
    )

    if ($Result.Error) {
        Write-Log ('Eject on {0} failed: {1}' -f $Context.Letter, $Result.Error)
        $script:Notice = 'Eject failed: {0}' -f $Result.Error
    }
    elseif ($Result.Value -eq 'ejected-after-wake') {
        Write-Log ('Ejected {0} after re-sending the wake command.' -f $Context.Letter)
    }
    else {
        Write-Log ('Ejected {0}.' -f $Context.Letter)
    }

    Clear-Busy
}

function Open-Disc {
    $media = if ($null -ne $script:LastState) { $script:LastState.Media } else { $null }
    if ($null -eq $media -or -not $media.IsReady) {
        $script:Notice = 'No readable disc is mounted.'
        Update-View
        return
    }

    Start-Process -FilePath 'explorer.exe' -ArgumentList $media.Root | Out-Null
    Write-Log ('Opened {0} in File Explorer.' -f $media.Root)
}

function Invoke-PrimaryAction {
    switch ([string]$script:Ui.PrimaryButton.Tag) {
        'wake' { Start-WakeAction -Reason 'manual' }
        'open' { Open-Disc }
    }
}

function Get-DiagnosticsText {
    $state = $script:LastState
    $lines = @(
        'Apple SuperDrive Helper diagnostics'
        ('Timestamp: {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'))
        ('Windows: {0}' -f [Environment]::OSVersion.Version)
        ('PowerShell: {0}' -f $PSVersionTable.PSVersion)
        ('RunningElevated: {0}' -f (Format-BoolYesNo $script:IsAdmin))
        ''
    )

    if ($null -eq $state) {
        $lines += 'DriveState: not read yet'
    }
    else {
        $drive = $state.Drive
        $media = $state.Media
        $lines += ('AppleDriveCount: {0}' -f $state.AppleDrives.Count)
        $lines += ('OpticalDriveCount: {0}' -f $state.AllDrives.Count)

        if ($null -eq $drive) {
            $lines += 'PrimaryDrive: none'
        }
        else {
            $lines += @(
                ('PrimaryDrive: {0}' -f $drive.Caption)
                ('PnpDeviceId: {0}' -f $drive.PNPDeviceID)
                ('DriveLetter: {0}' -f $(if ($drive.Drive) { $drive.Drive } else { 'None' }))
                ('WindowsStatus: {0}' -f $(if ($drive.Status) { $drive.Status } else { 'Unknown' }))
                ('MediaLoaded: {0}' -f (Format-BoolYesNo $drive.MediaLoaded))
                ('WakeState: {0}' -f (Get-WakeStateText (Get-DriveKey $drive)))
            )

            if ($null -ne $media) {
                $lines += @(
                    ('MediaReady: {0}' -f (Format-BoolYesNo $media.IsReady))
                    ('VolumeLabel: {0}' -f $media.VolumeLabel)
                    ('FileSystem: {0}' -f $media.DriveFormat)
                    ('Capacity: {0}' -f (Format-Bytes $media.TotalSize))
                    ('MediaRoot: {0}' -f $media.Root)
                )
            }
        }

        $lines += ''
        $lines += 'Detected optical drives:'
        if ($state.AllDrives.Count -eq 0) {
            $lines += '  none'
        }
        foreach ($optical in $state.AllDrives) {
            $lines += ('  {0} | Drive={1} | Status={2} | MediaLoaded={3}' -f
                $optical.Caption,
                $(if ($optical.Drive) { $optical.Drive } else { 'None' }),
                $(if ($optical.Status) { $optical.Status } else { 'Unknown' }),
                (Format-BoolYesNo $optical.MediaLoaded))
        }
    }

    $logText = $script:Ui.LogBox.Text
    if (-not [string]::IsNullOrEmpty($logText)) {
        $lines += ''
        $lines += 'Recent log:'
        $lines += @($logText -split '\r?\n' | Select-Object -Last 25)
    }

    return ($lines -join [Environment]::NewLine)
}

function Copy-Diagnostics {
    [System.Windows.Clipboard]::SetText((Get-DiagnosticsText))
    $script:Ui.CopyButton.Content = 'Copied'
    $script:CopyResetAt = [datetime]::UtcNow.AddSeconds(2)
    Write-Log 'Diagnostics copied to the clipboard.'
}

# --- View -----------------------------------------------------------------------------

# Chooses what the status area says for the current state. Tone names a palette brush.
function Get-HeroView {
    param($Key, $Letter, $Media, [bool]$MediaReady, [bool]$MediaLoading)

    $view = @{
        Tone       = 'Neutral'
        Badge      = ''
        Spinning   = $false
        Headline   = ''
        Detail     = ''
        Action     = $null
        ActionText = ''
    }

    $hasDrive = ($null -ne $script:LastState) -and ($null -ne $script:LastState.Drive)

    if ($script:Busy) {
        $view.Tone = 'Accent'
        $view.Badge = 'Working'
        $view.Spinning = $true
        $view.Headline = $script:BusyHeadline
        $view.Detail = $script:BusyDetail
    }
    elseif ($null -eq $script:LastState) {
        $view.Badge = 'Starting'
        $view.Spinning = $true
        $view.Headline = 'Looking for your SuperDrive'
        $view.Detail = 'Checking the optical drives on this PC.'
    }
    elseif (-not $hasDrive) {
        $view.Badge = 'Not connected'
        $view.Headline = 'No SuperDrive connected'
        $view.Detail = 'Plug the drive straight into a USB port on this PC. It is woken as soon as it appears.'
    }
    elseif ($null -eq $Key) {
        $view.Tone = 'Warning'
        $view.Badge = 'Starting up'
        $view.Spinning = $true
        $view.Headline = 'SuperDrive found'
        $view.Detail = 'Waiting for Windows to give it a drive letter.'
    }
    elseif ($MediaReady) {
        $view.Tone = 'Success'
        $view.Badge = 'Disc mounted'
        $view.Headline = 'Disc ready'
        $view.Detail = Get-MediaSummary $Media
        $view.Action = 'open'
        $view.ActionText = 'Open disc'
    }
    elseif ($MediaLoading) {
        $view.Tone = 'Accent'
        $view.Badge = 'Disc inserted'
        $view.Spinning = $true
        $view.Headline = 'Reading the disc'
        $view.Detail = 'Windows is mounting it. A blank or unreadable disc stays here; use Eject to take it out.'
    }
    elseif ($Key -eq $script:WokenKey) {
        $view.Tone = 'Success'
        $view.Badge = 'Awake'
        $view.Headline = 'Ready for a disc'
        $view.Detail = 'Slide a disc into the slot. It will show up as {0}.' -f $Letter
    }
    elseif ($Key -eq $script:FailedKey) {
        $view.Tone = 'Danger'
        $view.Badge = 'Needs attention'
        $view.Headline = 'The drive did not wake'
        $view.Detail = $script:WakeError
        $view.Action = 'wake'
        $view.ActionText = 'Try again'
    }
    else {
        $view.Tone = 'Warning'
        $view.Badge = 'Asleep'
        $view.Headline = 'The drive needs waking'
        $view.Detail = 'It ignores discs until it receives the wake command.'
        $view.Action = 'wake'
        $view.ActionText = 'Wake drive'
    }

    return $view
}

function Update-View {
    $ui = $script:Ui
    $state = $script:LastState
    $drive = $null
    $media = $null
    if ($null -ne $state) {
        $drive = $state.Drive
        $media = $state.Media
    }

    $key = Get-DriveKey $drive
    $letter = if ($null -ne $key) { $drive.Drive } else { $null }
    $mediaReady = [bool](($null -ne $media) -and $media.IsReady)
    $mediaLoading = [bool](($null -ne $drive) -and ($drive.MediaLoaded -eq $true) -and -not $mediaReady)
    $view = Get-HeroView -Key $key -Letter $letter -Media $media -MediaReady $mediaReady -MediaLoading $mediaLoading

    $brush = $script:Window.Resources[$view.Tone]
    $tint = $brush.Clone()
    $tint.Opacity = 0.16
    $ui.StatusDot.Fill = $brush
    $ui.Glow.Fill = $brush
    $ui.Badge.Background = $tint
    $ui.BadgeText.Foreground = $brush
    $ui.BadgeText.Text = $view.Badge
    $ui.Spinner.Stroke = $brush
    $ui.Spinner.Visibility = if ($view.Spinning) { 'Visible' } else { 'Collapsed' }
    $ui.Headline.Text = $view.Headline
    $ui.Detail.Text = $view.Detail
    $ui.DiscArt.Visibility = if ($mediaReady) { 'Visible' } else { 'Collapsed' }

    $idle = -not $script:Busy
    $hasLetter = $null -ne $letter
    if ($null -ne $view.Action) {
        $ui.PrimaryButton.Content = $view.ActionText
        $ui.PrimaryButton.Tag = $view.Action
        $ui.PrimaryButton.Visibility = 'Visible'
    }
    else {
        $ui.PrimaryButton.Tag = $null
        $ui.PrimaryButton.Visibility = 'Collapsed'
    }
    $ui.PrimaryButton.IsEnabled = $idle
    $ui.WakeButton.Visibility = if ($view.Action -eq 'wake') { 'Collapsed' } else { 'Visible' }
    $ui.WakeButton.IsEnabled = $idle -and $hasLetter
    $ui.EjectButton.IsEnabled = $idle -and $hasLetter
    $ui.RefreshButton.IsEnabled = $idle

    $ui.Notice.Text = $script:Notice
    $ui.Notice.Visibility = if ([string]::IsNullOrEmpty($script:Notice)) { 'Collapsed' } else { 'Visible' }

    $dash = [string][char]0x2014
    $ui.AdminText.Text = if ($script:IsAdmin) { 'Running as administrator' } else { 'Not running as administrator' }
    $ui.AdminValue.Text = Format-BoolYesNo $script:IsAdmin

    if ($null -eq $drive) {
        $ui.LetterValue.Text = $dash
        $ui.DeviceValue.Text = 'Not connected'
        $ui.StatusValue.Text = $dash
        $ui.LoadedValue.Text = $dash
    }
    else {
        $ui.LetterValue.Text = if ($hasLetter) { $letter } else { 'Not assigned yet' }
        $ui.DeviceValue.Text = if ($drive.Caption) { $drive.Caption } else { $drive.Name }
        $ui.StatusValue.Text = if ($drive.Status) { $drive.Status } else { 'Unknown' }
        $ui.LoadedValue.Text = Format-BoolYesNo $drive.MediaLoaded
    }
    $ui.DeviceValue.ToolTip = $ui.DeviceValue.Text

    if ($mediaReady) {
        $ui.LabelValue.Text = if ([string]::IsNullOrWhiteSpace($media.VolumeLabel)) { '(no label)' } else { $media.VolumeLabel }
        $ui.FormatValue.Text = if ([string]::IsNullOrWhiteSpace($media.DriveFormat)) { 'Unknown' } else { $media.DriveFormat }
        $ui.CapacityValue.Text = Format-Bytes $media.TotalSize
    }
    else {
        $ui.LabelValue.Text = $dash
        $ui.FormatValue.Text = $dash
        $ui.CapacityValue.Text = $dash
    }
}

# --- Window ---------------------------------------------------------------------------

function Test-AppsUseDarkTheme {
    $setting = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name 'AppsUseLightTheme' -ErrorAction SilentlyContinue
    return ($null -ne $setting -and $setting.AppsUseLightTheme -eq 0)
}

function Get-Palette {
    param([bool]$Dark)

    if ($Dark) {
        return @{
            Bg            = '#191B1F'
            Surface       = '#22252A'
            Surface2      = '#2A2E34'
            Surface3      = '#333840'
            Stroke        = '#383D45'
            Text          = '#ECEEF1'
            Muted         = '#A0A7B1'
            Accent        = '#4C8DFF'
            AccentHover   = '#6BA1FF'
            AccentPressed = '#3B78E7'
            OnAccent      = '#0B1220'
            Success       = '#3FB950'
            Warning       = '#E0A43A'
            Danger        = '#F26B6B'
            Neutral       = '#6E7681'
            LogBg         = '#1C1F23'
        }
    }

    return @{
        Bg            = '#F3F4F6'
        Surface       = '#FFFFFF'
        Surface2      = '#F6F7F9'
        Surface3      = '#ECEEF1'
        Stroke        = '#E2E5E9'
        Text          = '#16191D'
        Muted         = '#5E6670'
        Accent        = '#2563EB'
        AccentHover   = '#1D4ED8'
        AccentPressed = '#1E40AF'
        OnAccent      = '#FFFFFF'
        Success       = '#15803D'
        Warning       = '#B45309'
        Danger        = '#DC2626'
        Neutral       = '#8A929C'
        LogBg         = '#F6F7F9'
    }
}

# Window layout. __Name__ tokens are replaced with palette colors before parsing.
$script:XamlTemplate = @'
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Apple SuperDrive Helper"
    Width="900" Height="720" MinWidth="760" MinHeight="640"
    WindowStartupLocation="CenterScreen"
    Background="__Bg__"
    Foreground="__Text__"
    FontFamily="Segoe UI Variable Text, Segoe UI"
    FontSize="13"
    UseLayoutRounding="True"
    SnapsToDevicePixels="True"
    TextOptions.TextFormattingMode="Display">

  <Window.Resources>
    <SolidColorBrush x:Key="Bg" Color="__Bg__"/>
    <SolidColorBrush x:Key="Surface" Color="__Surface__"/>
    <SolidColorBrush x:Key="Surface2" Color="__Surface2__"/>
    <SolidColorBrush x:Key="Surface3" Color="__Surface3__"/>
    <SolidColorBrush x:Key="Stroke" Color="__Stroke__"/>
    <SolidColorBrush x:Key="Text" Color="__Text__"/>
    <SolidColorBrush x:Key="Muted" Color="__Muted__"/>
    <SolidColorBrush x:Key="Accent" Color="__Accent__"/>
    <SolidColorBrush x:Key="AccentHover" Color="__AccentHover__"/>
    <SolidColorBrush x:Key="AccentPressed" Color="__AccentPressed__"/>
    <SolidColorBrush x:Key="OnAccent" Color="__OnAccent__"/>
    <SolidColorBrush x:Key="Success" Color="__Success__"/>
    <SolidColorBrush x:Key="Warning" Color="__Warning__"/>
    <SolidColorBrush x:Key="Danger" Color="__Danger__"/>
    <SolidColorBrush x:Key="Neutral" Color="__Neutral__"/>
    <SolidColorBrush x:Key="LogBg" Color="__LogBg__"/>

    <Style x:Key="Card" TargetType="Border">
      <Setter Property="Background" Value="{StaticResource Surface}"/>
      <Setter Property="BorderBrush" Value="{StaticResource Stroke}"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="12"/>
      <Setter Property="Padding" Value="20"/>
    </Style>

    <Style x:Key="CardTitle" TargetType="TextBlock">
      <Setter Property="FontSize" Value="12"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="{StaticResource Muted}"/>
      <Setter Property="Margin" Value="0,0,0,12"/>
    </Style>

    <Style x:Key="FieldLabel" TargetType="TextBlock">
      <Setter Property="Foreground" Value="{StaticResource Muted}"/>
      <Setter Property="Margin" Value="0,0,16,9"/>
    </Style>

    <Style x:Key="FieldValue" TargetType="TextBlock">
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
      <Setter Property="Margin" Value="0,0,0,9"/>
    </Style>

    <Style x:Key="ButtonBase" TargetType="Button">
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Padding" Value="16,8"/>
      <Setter Property="Margin" Value="0,0,8,8"/>
      <Setter Property="MinWidth" Value="84"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
    </Style>

    <Style x:Key="AccentButton" TargetType="Button" BasedOn="{StaticResource ButtonBase}">
      <Setter Property="Foreground" Value="{StaticResource OnAccent}"/>
      <Setter Property="Background" Value="{StaticResource Accent}"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="Chrome" Background="{TemplateBinding Background}" BorderBrush="Transparent"
                    BorderThickness="1" CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="Chrome" Property="Background" Value="{StaticResource AccentHover}"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="Chrome" Property="Background" Value="{StaticResource AccentPressed}"/>
              </Trigger>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter TargetName="Chrome" Property="BorderBrush" Value="{StaticResource Text}"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="Chrome" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="SubtleButton" TargetType="Button" BasedOn="{StaticResource ButtonBase}">
      <Setter Property="Foreground" Value="{StaticResource Text}"/>
      <Setter Property="Background" Value="{StaticResource Surface2}"/>
      <Setter Property="BorderBrush" Value="{StaticResource Stroke}"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="Chrome" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="1" CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="Chrome" Property="Background" Value="{StaticResource Surface3}"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="Chrome" Property="Background" Value="{StaticResource Stroke}"/>
              </Trigger>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter TargetName="Chrome" Property="BorderBrush" Value="{StaticResource Accent}"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="Chrome" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="LinkButton" TargetType="Button">
      <Setter Property="Foreground" Value="{StaticResource Accent}"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Padding" Value="4,2"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="Chrome" Background="Transparent" BorderBrush="Transparent" BorderThickness="1"
                    CornerRadius="4" Padding="{TemplateBinding Padding}">
              <ContentPresenter/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Foreground" Value="{StaticResource AccentHover}"/>
              </Trigger>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter TargetName="Chrome" Property="BorderBrush" Value="{StaticResource Accent}"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>

  <Grid Margin="28,22,28,24">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
    </Grid.RowDefinitions>

    <!-- Header -->
    <Grid Grid.Row="0" Margin="2,0,2,18">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <StackPanel>
        <TextBlock Text="Apple SuperDrive Helper" FontFamily="Segoe UI Variable Display, Segoe UI"
                   FontSize="20" FontWeight="SemiBold" TextOptions.TextFormattingMode="Ideal"/>
        <TextBlock Text="Wakes the drive so it accepts discs, and watches it while this window is open."
                   Foreground="{StaticResource Muted}" Margin="0,3,0,0" TextWrapping="Wrap"/>
      </StackPanel>
      <Border Grid.Column="1" VerticalAlignment="Center" CornerRadius="10" Padding="10,4"
              Background="{StaticResource Surface2}" BorderBrush="{StaticResource Stroke}" BorderThickness="1">
        <TextBlock x:Name="AdminText" FontSize="12" Foreground="{StaticResource Muted}"/>
      </Border>
    </Grid>

    <!-- Status -->
    <Border Grid.Row="1" Style="{StaticResource Card}" Padding="28,26,28,20">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>

        <Viewbox Width="220" Height="110" Margin="0,0,32,0" VerticalAlignment="Center">
          <Canvas Width="240" Height="120">
            <Ellipse x:Name="Glow" Canvas.Left="30" Canvas.Top="70" Width="180" Height="36" Opacity="0.45">
              <Ellipse.Effect>
                <BlurEffect Radius="22"/>
              </Ellipse.Effect>
            </Ellipse>
            <Path Data="M 34,20 L 206,20 L 234,58 L 6,58 Z" Stroke="#9CA3AF" StrokeThickness="0.75">
              <Path.Fill>
                <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                  <GradientStop Color="#F4F5F7" Offset="0"/>
                  <GradientStop Color="#C9CDD3" Offset="1"/>
                </LinearGradientBrush>
              </Path.Fill>
            </Path>
            <Border Canvas.Left="6" Canvas.Top="58" Width="228" Height="16" CornerRadius="0,0,7,7">
              <Border.Background>
                <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                  <GradientStop Color="#B8BDC5" Offset="0"/>
                  <GradientStop Color="#8F959E" Offset="1"/>
                </LinearGradientBrush>
              </Border.Background>
            </Border>
            <Border Canvas.Left="64" Canvas.Top="64.5" Width="112" Height="3" CornerRadius="1.5" Background="#23272E"/>
            <Ellipse x:Name="StatusDot" Canvas.Left="212" Canvas.Top="63" Width="6" Height="6"/>
            <Canvas x:Name="DiscArt" Visibility="Collapsed">
              <Ellipse Canvas.Left="62" Canvas.Top="8" Width="116" Height="30" Stroke="#9CA3AF" StrokeThickness="0.6">
                <Ellipse.Fill>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
                    <GradientStop Color="#EEF1F5" Offset="0"/>
                    <GradientStop Color="#C7D2FE" Offset="0.4"/>
                    <GradientStop Color="#FBCFE8" Offset="0.7"/>
                    <GradientStop Color="#E5E7EB" Offset="1"/>
                  </LinearGradientBrush>
                </Ellipse.Fill>
              </Ellipse>
              <Ellipse Canvas.Left="108" Canvas.Top="19.5" Width="24" Height="7" Fill="#AEB4BD"/>
              <Ellipse Canvas.Left="115" Canvas.Top="21.6" Width="10" Height="2.8" Fill="#6B7280"/>
            </Canvas>
          </Canvas>
        </Viewbox>

        <StackPanel Grid.Column="1" VerticalAlignment="Center">
          <Border x:Name="Badge" HorizontalAlignment="Left" CornerRadius="10" Padding="9,3,10,3" Margin="0,0,0,10">
            <StackPanel Orientation="Horizontal">
              <Path x:Name="Spinner" Width="12" Height="12" Margin="0,0,6,0" VerticalAlignment="Center"
                    Data="M 6,1.25 A 4.75,4.75 0 1 1 1.25,6" StrokeThickness="1.75"
                    StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                    RenderTransformOrigin="0.5,0.5" Visibility="Collapsed">
                <Path.RenderTransform>
                  <RotateTransform/>
                </Path.RenderTransform>
                <Path.Triggers>
                  <EventTrigger RoutedEvent="FrameworkElement.Loaded">
                    <BeginStoryboard>
                      <Storyboard>
                        <DoubleAnimation Storyboard.TargetProperty="(UIElement.RenderTransform).(RotateTransform.Angle)"
                                         From="0" To="360" Duration="0:0:0.9" RepeatBehavior="Forever"/>
                      </Storyboard>
                    </BeginStoryboard>
                  </EventTrigger>
                </Path.Triggers>
              </Path>
              <TextBlock x:Name="BadgeText" FontSize="12" FontWeight="SemiBold" VerticalAlignment="Center"/>
            </StackPanel>
          </Border>
          <TextBlock x:Name="Headline" FontFamily="Segoe UI Variable Display, Segoe UI" FontSize="26"
                     FontWeight="SemiBold" TextWrapping="Wrap" TextOptions.TextFormattingMode="Ideal"/>
          <TextBlock x:Name="Detail" FontSize="14" Foreground="{StaticResource Muted}" TextWrapping="Wrap"
                     Margin="0,6,0,18" LineHeight="20"/>
          <WrapPanel>
            <Button x:Name="PrimaryButton" Style="{StaticResource AccentButton}" Content="Wake drive"/>
            <Button x:Name="WakeButton" Style="{StaticResource SubtleButton}" Content="Wake"/>
            <Button x:Name="EjectButton" Style="{StaticResource SubtleButton}" Content="Eject"/>
            <Button x:Name="RefreshButton" Style="{StaticResource SubtleButton}" Content="Refresh"/>
          </WrapPanel>
          <TextBlock x:Name="Notice" Foreground="{StaticResource Danger}" TextWrapping="Wrap" Margin="0,2,0,0"
                     Visibility="Collapsed"/>
        </StackPanel>
      </Grid>
    </Border>

    <!-- Details -->
    <Grid Grid.Row="2" Margin="0,16,0,16">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="16"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>

      <Border Grid.Column="0" Style="{StaticResource Card}">
        <StackPanel>
          <TextBlock Text="Drive" Style="{StaticResource CardTitle}"/>
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="120"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <TextBlock Grid.Row="0" Text="Letter" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="0" Grid.Column="1" x:Name="LetterValue" Style="{StaticResource FieldValue}"/>
            <TextBlock Grid.Row="1" Text="Device" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="1" Grid.Column="1" x:Name="DeviceValue" Style="{StaticResource FieldValue}"/>
            <TextBlock Grid.Row="2" Text="Windows status" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="2" Grid.Column="1" x:Name="StatusValue" Style="{StaticResource FieldValue}"/>
            <TextBlock Grid.Row="3" Text="Administrator" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="3" Grid.Column="1" x:Name="AdminValue" Style="{StaticResource FieldValue}"/>
          </Grid>
        </StackPanel>
      </Border>

      <Border Grid.Column="2" Style="{StaticResource Card}">
        <StackPanel>
          <TextBlock Text="Disc" Style="{StaticResource CardTitle}"/>
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="120"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <TextBlock Grid.Row="0" Text="Disc loaded" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="0" Grid.Column="1" x:Name="LoadedValue" Style="{StaticResource FieldValue}"/>
            <TextBlock Grid.Row="1" Text="Label" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="1" Grid.Column="1" x:Name="LabelValue" Style="{StaticResource FieldValue}"/>
            <TextBlock Grid.Row="2" Text="File system" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="2" Grid.Column="1" x:Name="FormatValue" Style="{StaticResource FieldValue}"/>
            <TextBlock Grid.Row="3" Text="Capacity" Style="{StaticResource FieldLabel}"/>
            <TextBlock Grid.Row="3" Grid.Column="1" x:Name="CapacityValue" Style="{StaticResource FieldValue}"/>
          </Grid>
        </StackPanel>
      </Border>
    </Grid>

    <!-- Activity -->
    <Border Grid.Row="3" Style="{StaticResource Card}" Padding="20,14,20,16">
      <DockPanel>
        <DockPanel DockPanel.Dock="Top" Margin="0,0,0,8">
          <Button x:Name="CopyButton" DockPanel.Dock="Right" Style="{StaticResource LinkButton}" Content="Copy diagnostics"/>
          <TextBlock Text="Activity" Style="{StaticResource CardTitle}" Margin="0" VerticalAlignment="Center"/>
        </DockPanel>
        <Border Background="{StaticResource LogBg}" CornerRadius="8" Padding="10,8">
          <TextBox x:Name="LogBox" IsReadOnly="True" BorderThickness="0" Background="Transparent"
                   Foreground="{StaticResource Muted}" CaretBrush="{StaticResource Text}"
                   SelectionBrush="{StaticResource Accent}" FontFamily="Cascadia Mono, Consolas" FontSize="12"
                   TextWrapping="Wrap" VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled">
            <TextBox.Resources>
              <Style TargetType="ScrollBar">
                <Setter Property="Width" Value="8"/>
                <Setter Property="MinWidth" Value="8"/>
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate TargetType="ScrollBar">
                      <Track x:Name="PART_Track" IsDirectionReversed="True">
                        <Track.Thumb>
                          <Thumb>
                            <Thumb.Template>
                              <ControlTemplate TargetType="Thumb">
                                <Border CornerRadius="4" Background="{StaticResource Neutral}" Opacity="0.6"/>
                              </ControlTemplate>
                            </Thumb.Template>
                          </Thumb>
                        </Track.Thumb>
                      </Track>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
            </TextBox.Resources>
          </TextBox>
        </Border>
      </DockPanel>
    </Border>
  </Grid>
</Window>
'@

$script:ElementNames = @(
    'AdminText', 'Glow', 'StatusDot', 'DiscArt', 'Badge', 'Spinner', 'BadgeText', 'Headline', 'Detail',
    'PrimaryButton', 'WakeButton', 'EjectButton', 'RefreshButton', 'Notice',
    'LetterValue', 'DeviceValue', 'StatusValue', 'AdminValue',
    'LoadedValue', 'LabelValue', 'FormatValue', 'CapacityValue',
    'CopyButton', 'LogBox'
)

function New-MainWindow {
    $xamlText = $script:XamlTemplate
    foreach ($name in $script:Palette.Keys) {
        $xamlText = $xamlText.Replace(('__{0}__' -f $name), $script:Palette[$name])
    }
    if ($xamlText -match '__[A-Za-z0-9]+__') {
        throw ('The window layout uses an undefined color token {0}.' -f $Matches[0])
    }

    $window = [System.Windows.Markup.XamlReader]::Parse($xamlText)
    foreach ($name in $script:ElementNames) {
        $element = $window.FindName($name)
        if ($null -eq $element) {
            throw ('The window layout has no element named {0}.' -f $name)
        }
        $script:Ui[$name] = $element
    }

    return $window
}

# Runs an event handler body and reports its errors in the log and under the buttons,
# because an exception escaping a WPF event handler would close the window.
function Invoke-Handler {
    param([Parameter(Mandatory)][scriptblock]$Body)

    try {
        & $Body
    }
    catch {
        $message = Get-ErrorMessage $_
        Write-Log ('ERROR: {0}' -f $message)
        $script:Notice = $message
        $notice = $script:Ui['Notice']
        if ($null -ne $notice) {
            $notice.Text = $message
            $notice.Visibility = 'Visible'
        }
    }
}

function Invoke-TimerTick {
    Complete-FinishedWork

    $due = $script:PollRequested -or ([datetime]::UtcNow -ge $script:NextPollAt)
    if ($due -and -not $script:PollInFlight -and -not $script:Busy) {
        Start-StatusPoll
    }

    if ($null -ne $script:CopyResetAt -and [datetime]::UtcNow -ge $script:CopyResetAt) {
        $script:Ui.CopyButton.Content = 'Copy diagnostics'
        $script:CopyResetAt = $null
    }
}

# Handlers run later from the dispatcher, so they use $script: state and functions only.
function Register-WindowEvents {
    $script:Ui.PrimaryButton.Add_Click({ Invoke-Handler { Invoke-PrimaryAction } })
    $script:Ui.WakeButton.Add_Click({ Invoke-Handler { Start-WakeAction -Reason 'manual' } })
    $script:Ui.EjectButton.Add_Click({ Invoke-Handler { Start-EjectAction } })
    $script:Ui.RefreshButton.Add_Click({ Invoke-Handler { Request-StatusPoll } })
    $script:Ui.CopyButton.Add_Click({ Invoke-Handler { Copy-Diagnostics } })

    $script:Window.Add_KeyDown({
            param($source, $keyArgs)
            Invoke-Handler {
                if ($keyArgs.Key -eq [System.Windows.Input.Key]::Escape) {
                    $script:Window.Close()
                }
                elseif ($keyArgs.Key -eq [System.Windows.Input.Key]::F5) {
                    Request-StatusPoll
                }
            }
        })

    $script:Window.Add_SourceInitialized({
            Invoke-Handler {
                $handle = (New-Object System.Windows.Interop.WindowInteropHelper -ArgumentList $script:Window).Handle
                $bg = $script:Palette['Bg']
                [void][AppleSuperDrive.TitleBar]::Apply(
                    $handle,
                    $script:DarkTheme,
                    [Convert]::ToInt32($bg.Substring(1, 2), 16),
                    [Convert]::ToInt32($bg.Substring(3, 2), 16),
                    [Convert]::ToInt32($bg.Substring(5, 2), 16))
            }
        })

    $script:Window.Add_ContentRendered({
            Invoke-Handler {
                Write-Log 'Apple SuperDrive Helper started.'
                if (-not $script:IsAdmin) {
                    Write-Log 'Running without administrator rights; waking the drive will need them.'
                }
                Request-StatusPoll
                $script:Timer.Start()
            }
        })

    $script:Window.Add_Closed({
            $script:Timer.Stop()
        })

    $script:Window.Dispatcher.Add_UnhandledException({
            param($source, $failure)
            Write-Log ('Unexpected error: {0}' -f $failure.Exception.Message)
            $failure.Handled = $true
        })

    $script:Timer = New-Object System.Windows.Threading.DispatcherTimer
    $script:Timer.Interval = [TimeSpan]::FromMilliseconds(250)
    $script:Timer.Add_Tick({ Invoke-Handler { Invoke-TimerTick } })
}

# --- Main -----------------------------------------------------------------------------

try {
    Initialize-LogFile
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
    if (-not ('AppleSuperDrive.Native' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'SuperDriveNative.cs')
    }
    $script:NativeType = [AppleSuperDrive.Native]

    $script:DarkTheme = Test-AppsUseDarkTheme
    $script:Palette = Get-Palette -Dark $script:DarkTheme
    $script:Window = New-MainWindow
    Register-WindowEvents

    $script:Pool = [runspacefactory]::CreateRunspacePool(1, 2)
    $script:Pool.Open()

    Update-View
    [void]$script:Window.ShowDialog()
}
catch {
    Show-FatalError -Message (Get-ErrorMessage $_)
    exit 1
}
finally {
    if ($null -ne $script:Pool) {
        $script:Pool.Dispose()
    }
}
