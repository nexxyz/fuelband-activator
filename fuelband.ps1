[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("attach", "detach", "status", "set-time", "set-name", "set-target", "mark-imprinted")]
    [string]$Command,

    [Parameter(Position = 1)]
    [string]$Name,

    [string]$BusId,

    [string]$Distribution,

    [string]$Fuel,

    [switch]$VerboseOutput,

    [switch]$Experimental,

    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$FuelBandPidMap = @{
    "11ac:317d" = "supported SE/current protocol"
    "11ac:6565" = "legacy/original protocol (unsupported framing)"
}

function Stop-Clearly([string]$Message) {
    throw $Message
}

function Confirm-MarkImprintedSwitches {
    if ($Command -eq "mark-imprinted") {
        if (-not $Experimental -or -not $Yes) {
            Stop-Clearly "mark-imprinted is experimental and requires both -Experimental and -Yes; no write was attempted."
        }
    } elseif ($Experimental -or $Yes) {
        Stop-Clearly "-Experimental and -Yes are valid only with mark-imprinted; no write was attempted."
    }
}

function Get-RequiredCommand([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        Stop-Clearly "Required command '$Name' was not found. Install/enable it and retry."
    }
    return $command
}

function Get-WslDistros($WslCommand) {
    $lines = @(& $WslCommand.Source --list --quiet 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-Clearly "WSL is unavailable or could not list distributions. Install WSL2 and a distribution first."
    }
    return @(
        $lines |
            ForEach-Object { ("$_").Trim().Trim([char]0) } |
            Where-Object { $_ -ne "" }
    )
}

function Confirm-Wsl($WslCommand) {
    $distros = @(Get-WslDistros $WslCommand)
    if ($distros.Count -eq 0) {
        Stop-Clearly "No WSL distribution is installed. Install WSL2 and a distribution first."
    }
    if (-not [string]::IsNullOrWhiteSpace($Distribution)) {
        if ($distros -notcontains $Distribution) {
            Stop-Clearly "WSL distribution '$Distribution' was not found. Available: $($distros -join ', ')"
        }
    }
}

function Test-WslHidraw($WslCommand, [string]$ExpectedPid) {
    $probePath = Join-Path $PSScriptRoot "wsl_hidraw_probe.py"
    if (-not (Test-Path -LiteralPath $probePath)) {
        Stop-Clearly "wsl_hidraw_probe.py is missing from the release directory."
    }
    $linuxProbe = Convert-ReleasePathToWsl $PSScriptRoot "wsl_hidraw_probe.py"
    $arguments = @()
    if (-not [string]::IsNullOrWhiteSpace($Distribution)) {
        $arguments += @("-d", $Distribution)
    }
    # The helper is a static file; ExpectedPid is passed as data, never code.
    $arguments += @("--user", "root", "--", "python3", $linuxProbe, $ExpectedPid)
    $null = & $WslCommand.Source @arguments 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Wait-WslHidraw($WslCommand, [string]$ExpectedPid, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-WslHidraw $WslCommand $ExpectedPid) {
            return $true
        }
        if ((Get-Date) -ge $deadline) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ($true)
    return $false
}

function Get-UsbipdDeviceList($UsbipdCommand) {
    $lines = @(& $UsbipdCommand.Source list 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Stop-Clearly "usbipd could not list USB devices. Is usbipd-win installed and running?"
    }
    return @($lines | ForEach-Object { "$_" })
}

function Find-UsbipdRow([string[]]$DeviceList, [string]$RequestedBusId) {
    $rows = @(
        $DeviceList | Where-Object {
            $fields = ("$_").Trim() -split "\s+"
            $fields.Count -gt 0 -and $fields[0] -eq $RequestedBusId
        }
    )
    if ($rows.Count -eq 0) {
        Stop-Clearly "USB device '$RequestedBusId' was not found. Run 'usbipd list' and use an existing BUSID."
    }
    if ($rows.Count -ne 1) {
        Stop-Clearly "USB bus ID '$RequestedBusId' matched multiple usbipd rows; refusing an ambiguous device."
    }
    return $rows[0]
}

function Get-FuelBandSelections([string[]]$DeviceList) {
    $selections = @()
    foreach ($line in $DeviceList) {
        $fields = ("$line").Trim() -split "\s+"
        $devicePid = if ($fields.Count -ge 2) { $fields[1].ToLowerInvariant() } else { "" }
        if ($FuelBandPidMap.ContainsKey($devicePid)) {
            $selections += [PSCustomObject]@{
                BusId = $fields[0]
                Pid = $devicePid
                Row = "$line"
            }
        }
    }
    return $selections
}

function Resolve-FuelBandSelection($UsbipdCommand, [string]$RequestedBusId) {
    $deviceList = @(Get-UsbipdDeviceList $UsbipdCommand)
    if (-not [string]::IsNullOrWhiteSpace($RequestedBusId)) {
        $row = Find-UsbipdRow $deviceList $RequestedBusId
        $fields = ("$row").Trim() -split "\s+"
        $devicePid = if ($fields.Count -ge 2) { $fields[1].ToLowerInvariant() } else { "" }
        if (-not $FuelBandPidMap.ContainsKey($devicePid)) {
            Stop-Clearly "BUSID '$RequestedBusId' is not a known FuelBand-family device (11AC:317D or 11AC:6565)."
        }
        $selection = [PSCustomObject]@{
            BusId = $fields[0]
            Pid = $devicePid
            Row = "$row"
        }
    } else {
        $selections = @(Get-FuelBandSelections $deviceList)
        if ($selections.Count -eq 0) {
            Stop-Clearly "No known FuelBand-family USB row was found. Run 'usbipd list' and pass -BusId if needed."
        }
        if ($selections.Count -ne 1) {
            Stop-Clearly "Multiple FuelBand-family devices were found; pass -BusId to select one explicitly."
        }
        $selection = $selections[0]
    }
    Write-Host ("Selected FuelBand BUSID {0}, VID:PID {1}" -f $selection.BusId, $selection.Pid)
    return $selection
}

function Confirm-SupportedProtocol($Selection) {
    if ($Selection.Pid -ieq "11ac:6565") {
        Stop-Clearly "Legacy/original FuelBand PID 11AC:6565 detected. This CLI does not support its protocol framing; attach/detach only, no WSL command was invoked."
    }
}

function Convert-ReleasePathToWsl([string]$WindowsPath, [string]$LinuxFileName) {
    $resolved = (Resolve-Path -LiteralPath $WindowsPath).Path
    if ($resolved.Length -lt 3 -or $resolved[1] -ne ":") {
        Stop-Clearly "The release directory is not on a local drive that WSL can mount: $resolved"
    }
    $drive = $resolved.Substring(0, 1).ToLowerInvariant()
    $rest = $resolved.Substring(2).Replace("\", "/")
    return "/mnt/$drive$rest/$LinuxFileName"
}

function Convert-ScriptPathToWsl([string]$WindowsPath) {
    return Convert-ReleasePathToWsl $WindowsPath "fuelband_cli.py"
}

function Invoke-WslCli($WslCommand) {
    Confirm-Wsl $WslCommand
    $linuxScript = Convert-ScriptPathToWsl $PSScriptRoot
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "fuelband_cli.py"))) {
        Stop-Clearly "fuelband_cli.py is missing from the release directory."
    }

    $arguments = @()
    if (-not [string]::IsNullOrWhiteSpace($Distribution)) {
        $arguments += @("-d", $Distribution)
    }
    $arguments += @("--user", "root", "--", "python3", $linuxScript)
    if ($VerboseOutput) {
        $arguments += "--verbose"
    }
    $arguments += $Command
    if ($Command -eq "set-name") {
        if ([string]::IsNullOrWhiteSpace($Name)) {
            Stop-Clearly "set-name requires a NAME argument, for example -Name Alice."
        }
        $arguments += $Name
    }
    if ($Command -eq "set-time" -and -not [string]::IsNullOrWhiteSpace($Name)) {
        $arguments += $Name
    }
    if ($Command -eq "set-target") {
        if ([string]::IsNullOrWhiteSpace($Fuel)) {
            Stop-Clearly "set-target requires a -Fuel value from 1 through 0xffffffff."
        }
        $arguments += $Fuel
    }
    if ($Command -eq "mark-imprinted") {
        $arguments += @("--experimental", "--yes")
    }

    & $WslCommand.Source @arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Clearly "WSL FuelBand command '$Command' failed with exit code $LASTEXITCODE. Check that exactly one 11AC:317D hidraw device is attached."
    }
}

try {
    Confirm-MarkImprintedSwitches
    $usbipd = Get-RequiredCommand "usbipd.exe"

    if ($Command -eq "attach") {
        $selection = Resolve-FuelBandSelection $usbipd $BusId
        $wsl = Get-RequiredCommand "wsl.exe"
        Confirm-Wsl $wsl
        & $usbipd.Source attach --wsl --busid $selection.BusId
        if ($LASTEXITCODE -ne 0) {
            Stop-Clearly "usbipd attach failed for '$($selection.BusId)'. Confirm the one-time admin bind was completed."
        }
        if (-not (Wait-WslHidraw $wsl $selection.Pid 10)) {
            Stop-Clearly "usbipd attach succeeded, but WSL did not expose a matching $($selection.Pid) hidraw collection within 10 seconds. Clean up with: .\fuelband.ps1 detach -BusId $($selection.BusId)"
        }
        Write-Host "Attached '$($selection.BusId)' to WSL."
        exit 0
    }

    if ($Command -eq "detach") {
        $selection = Resolve-FuelBandSelection $usbipd $BusId
        & $usbipd.Source detach --busid $selection.BusId
        if ($LASTEXITCODE -ne 0) {
            Stop-Clearly "usbipd detach failed for '$($selection.BusId)'."
        }
        Write-Host "Detached '$($selection.BusId)'."
        exit 0
    }

    $selection = Resolve-FuelBandSelection $usbipd $BusId
    Confirm-SupportedProtocol $selection
    $wsl = Get-RequiredCommand "wsl.exe"
    Invoke-WslCli $wsl
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
