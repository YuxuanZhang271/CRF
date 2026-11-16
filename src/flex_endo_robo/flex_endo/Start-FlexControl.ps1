param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug',
    [ValidateSet('Planned', 'Encoder', 'Simulated')]
    [string]$Feedback = 'Planned',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$FullApp = $true,
    [string]$RobotIp,
    [double]$PullOutClearanceM,
    [ValidateRange(-180.0, 180.0)]
    [double]$CameraRollOffsetDeg,
    [switch]$FlexOnly
)

$ErrorActionPreference = 'Stop'

function ConvertFrom-PullOutClearance([string]$Text) {
    [double]$meters = 0
    if (![double]::TryParse($Text, [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$meters) -or
        [double]::IsNaN($meters) -or [double]::IsInfinity($meters) -or $meters -lt 0) {
        throw 'Enter a finite nonnegative distance in metres. Use a decimal point for fractions.'
    }
    return $meters
}

function Get-PullOutClearance([string]$SettingsPath) {
    if ($SettingsPath -and (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
        try {
            return (ConvertFrom-PullOutClearance (Get-Content -LiteralPath $SettingsPath -Raw))
        } catch {
            Write-Warning "Could not read the saved Pull Out clearance. $($_.Exception.Message)"
        }
    }

    Write-Host 'Pull Out needs the bench-verified distance from the RCM to the final tip on the withdrawal side.'
    while ($true) {
        try {
            $entry = Read-Host 'Pull Out clearance in metres (Enter to skip)'
        } catch {
            Write-Warning 'Pull Out clearance is unset. Run the launcher interactively to enter it.'
            return
        }
        if ([string]::IsNullOrWhiteSpace($entry)) { return }
        try {
            $meters = ConvertFrom-PullOutClearance $entry
        } catch {
            Write-Warning $_.Exception.Message
            continue
        }
        if ($SettingsPath) {
            try {
                $value = $meters.ToString('R', [Globalization.CultureInfo]::InvariantCulture)
                Set-Content -LiteralPath $SettingsPath -Value $value -Encoding Ascii
            } catch {
                Write-Warning "Pull Out clearance will be used for this session. Could not save it to '$SettingsPath'."
            }
        }
        return $meters
    }
}

if ($FlexOnly -and $FullApp -and $PSBoundParameters.ContainsKey('FullApp')) {
    throw 'Choose either -FullApp or -FlexOnly.'
}
$runFullApp = $FullApp -and !$FlexOnly
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).ProviderPath
$buildFolder = Join-Path $PSScriptRoot "build/Win32/$Configuration"
$driverPath = Join-Path $buildFolder 'FlexEndoscopeDriver.exe'
if (!(Test-Path -LiteralPath $driverPath -PathType Leaf)) {
    throw "Driver executable missing at '$driverPath'. Build the Visual Studio solution in $Configuration | x86."
}
if (Get-Process -Name 'FlexEndoscopeDriver' -ErrorAction SilentlyContinue) {
    throw 'Close the current Flex driver before starting this control session.'
}

$python = Get-Command python -CommandType Application | Select-Object -First 1
if ($env:CONDA_DEFAULT_ENV -ne 'crf') {
    throw 'Activate the crf Conda environment before running this script.'
}

$appOptions = @('--port-web', $Port)
if (!$runFullApp) {
    if ($PSBoundParameters.ContainsKey('RobotIp') -or $PSBoundParameters.ContainsKey('PullOutClearanceM')) {
        throw 'RobotIp and PullOutClearanceM require full-app mode.'
    }
    $appOptions += '--flex-only'
}
if ($PSBoundParameters.ContainsKey('RobotIp')) { $appOptions += @('--robot-ip', $RobotIp) }
if ($PSBoundParameters.ContainsKey('CameraRollOffsetDeg')) {
    $appOptions += @('--camera-roll-offset-deg', $CameraRollOffsetDeg.ToString([Globalization.CultureInfo]::InvariantCulture))
}
$clearance = $null
if ($PSBoundParameters.ContainsKey('PullOutClearanceM')) {
    if ([double]::IsNaN($PullOutClearanceM) -or [double]::IsInfinity($PullOutClearanceM) -or $PullOutClearanceM -lt 0) {
        throw 'PullOutClearanceM must be finite and nonnegative.'
    }
    $clearance = $PullOutClearanceM
} elseif ($runFullApp) {
    if ($PSBoundParameters.ContainsKey('RobotIp')) {
        $clearance = Get-PullOutClearance
    } else {
        $settingsPath = Join-Path $repoRoot 'config.local.pull-out-clearance-m'
        $clearance = Get-PullOutClearance $settingsPath
    }
}
if ($null -ne $clearance) {
    $appOptions += @('--pull-out-clearance-m', $clearance.ToString('R', [Globalization.CultureInfo]::InvariantCulture))
}

$driverOptions = @{
    FilePath = $driverPath
    WorkingDirectory = $buildFolder
    NoNewWindow = $true
    PassThru = $true
    ArgumentList = @('--managed-session')
}
switch ($Feedback) {
    'Planned' {
        if ($runFullApp) { $driverOptions.ArgumentList += '--planned-full' }
        else { $driverOptions.ArgumentList += '--manual-bench' }
    }
    'Simulated' { $driverOptions.ArgumentList += '--simulate' }
}

$driver = Start-Process @driverOptions
try {
    if ($driver.WaitForExit(400)) {
        throw "The Flex driver exited during startup (code $($driver.ExitCode)). See its console output."
    }
    Write-Host "Flex Web UI: http://127.0.0.1:$Port"
    Write-Host 'Use Ctrl+C to stop the session.'
    & $python.Source (Join-Path $repoRoot 'src/app.py') @appOptions
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1073741510) {
        throw "The Web UI exited with code $LASTEXITCODE."
    }
} finally {
    # The driver stops on heartbeat expiry and exits even after an app failure.
    [void]$driver.WaitForExit(21000)
    $driver.Dispose()
}
