[CmdletBinding()]
param(
    [string]$DriveLetter,
    [switch]$NoAutoElevate,
    # Wait for Enter before closing, so a double-clicked or elevated window stays readable.
    [switch]$PauseOnExit
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
        '-File'
        ('"{0}"' -f $PSCommandPath)
    ) + $ForwardArgs

    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argumentList | Out-Null
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

function Get-AppleSuperDrive {
    $drives = Get-CimInstance Win32_CDROMDrive | Where-Object {
        $_.Caption -like '*Apple SuperDrive*' -or
        $_.Name -like '*Apple SuperDrive*' -or
        $_.PNPDeviceID -like 'USBSTOR\CDROM&VEN_APPLE&PROD_SUPERDRIVE*'
    }

    if ($DriveLetter) {
        $normalized = '{0}:' -f $DriveLetter.Trim().TrimEnd(':')
        $drives = $drives | Where-Object { $_.Drive -eq $normalized }
    }

    return @($drives)
}

# Wakes every connected SuperDrive and returns $true when all of them accepted the command.
function Invoke-SuperDriveWake {
    if (-not ('AppleSuperDrive.Native' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'SuperDriveNative.cs')
    }

    $targets = @(Get-AppleSuperDrive)
    if ($targets.Count -eq 0) {
        $where = if ($DriveLetter) { ' at {0}:' -f $DriveLetter.Trim().TrimEnd(':') } else { '' }
        throw ('No Apple USB SuperDrive was found{0}. Connect it directly to a USB port and try again.' -f $where)
    }

    $results = foreach ($target in $targets) {
        if ([string]::IsNullOrWhiteSpace($target.Drive)) {
            [pscustomobject]@{
                Drive  = '(none)'
                Result = 'Skipped'
                Detail = 'Windows has not assigned a drive letter yet.'
            }
        }
        else {
            $wake = [AppleSuperDrive.Native]::Wake($target.Drive)
            [pscustomobject]@{
                Drive  = $target.Drive
                Result = $(if ($wake.Succeeded) { 'Awake' } else { 'Rejected' })
                Detail = $wake.Describe()
            }
        }
    }

    $results | Format-Table -AutoSize | Out-Host

    $failed = @($results | Where-Object { $_.Result -ne 'Awake' })
    if ($failed.Count -eq 0) {
        Write-Host 'The SuperDrive accepted the wake command. Insert a disc.' -ForegroundColor Green
        return $true
    }

    Write-Host 'The SuperDrive did not accept the wake command. Try another USB port or cable.' -ForegroundColor Red
    return $false
}

$pauseBeforeExit = $PauseOnExit.IsPresent
$exitCode = 0

try {
    if ((Test-IsAdministrator) -or $NoAutoElevate) {
        if (-not (Invoke-SuperDriveWake)) {
            $exitCode = 1
        }
    }
    else {
        # SCSI pass-through needs administrator rights. The elevated copy opens its own window
        # and always pauses there, so this window has nothing left to show.
        $forwardArgs = @('-PauseOnExit')
        if ($DriveLetter) {
            $forwardArgs += @('-DriveLetter', $DriveLetter)
        }

        try {
            Restart-Elevated -ForwardArgs $forwardArgs
        }
        catch {
            throw ('Waking the drive needs administrator rights, and elevation did not start: {0}' -f (Get-ErrorMessage $_))
        }

        $pauseBeforeExit = $false
    }
}
catch {
    Write-Host ('ERROR: {0}' -f (Get-ErrorMessage $_)) -ForegroundColor Red
    $exitCode = 1
}
finally {
    if ($pauseBeforeExit) {
        [void](Read-Host 'Press Enter to close')
    }
}

exit $exitCode
