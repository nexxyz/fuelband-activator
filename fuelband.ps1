[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("attach", "detach", "status", "set-time", "set-name", "mark-imprinted")]
    [string]$Command,

    [Parameter(Position = 1)]
    [string]$Name,

    [string]$BusId,

    [string]$Distribution,

    [switch]$VerboseOutput,

    [switch]$Experimental,

    [switch]$Yes
)

$ErrorActionPreference = "Stop"

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

function Confirm-BusId($UsbipdCommand, [string]$RequestedBusId) {
    if ([string]::IsNullOrWhiteSpace($RequestedBusId)) {
        Stop-Clearly "A USB bus ID is required. Run 'usbipd list', then pass -BusId BUSID."
    }
    $deviceList = @(Get-UsbipdDeviceList $UsbipdCommand)
    $row = Find-UsbipdRow $deviceList $RequestedBusId
    if ($row -notmatch "(?i)(^|\s)11ac:317d(\s|$)") {
        Stop-Clearly "BUSID '$RequestedBusId' is not the supported FuelBand VID:PID 11AC:317D."
    }
}

function Convert-ScriptPathToWsl([string]$WindowsPath) {
    $resolved = (Resolve-Path -LiteralPath $WindowsPath).Path
    if ($resolved.Length -lt 3 -or $resolved[1] -ne ":") {
        Stop-Clearly "The release directory is not on a local drive that WSL can mount: $resolved"
    }
    $drive = $resolved.Substring(0, 1).ToLowerInvariant()
    $rest = $resolved.Substring(2).Replace("\", "/")
    return "/mnt/$drive$rest/fuelband_cli.py"
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
        $wsl = Get-RequiredCommand "wsl.exe"
        Confirm-Wsl $wsl
        Confirm-BusId $usbipd $BusId
        & $usbipd.Source attach --wsl --busid $BusId
        if ($LASTEXITCODE -ne 0) {
            Stop-Clearly "usbipd attach failed for '$BusId'. Confirm the one-time admin bind was completed."
        }
        Write-Host "Attached '$BusId' to WSL."
        exit 0
    }

    if ($Command -eq "detach") {
        Confirm-BusId $usbipd $BusId
        & $usbipd.Source detach --busid $BusId
        if ($LASTEXITCODE -ne 0) {
            Stop-Clearly "usbipd detach failed for '$BusId'."
        }
        Write-Host "Detached '$BusId'."
        exit 0
    }

    $wsl = Get-RequiredCommand "wsl.exe"
    Invoke-WslCli $wsl
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
