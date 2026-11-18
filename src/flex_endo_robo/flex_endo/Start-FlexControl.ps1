param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug',
    [ValidateSet('Planned', 'Encoder', 'Simulated')]
    [string]$Feedback = 'Planned',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$FullApp = $true,
    [string]$RobotIp,
    [ValidateRange(-180.0, 180.0)]
    [double]$CameraRollOffsetDeg,
    [switch]$FlexOnly
)

$ErrorActionPreference = 'Stop'

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
    if ($PSBoundParameters.ContainsKey('RobotIp')) {
        throw 'RobotIp requires full-app mode.'
    }
    $appOptions += '--flex-only'
}
if ($PSBoundParameters.ContainsKey('RobotIp')) { $appOptions += @('--robot-ip', $RobotIp) }
if ($PSBoundParameters.ContainsKey('CameraRollOffsetDeg')) {
    $appOptions += @('--camera-roll-offset-deg', $CameraRollOffsetDeg.ToString([Globalization.CultureInfo]::InvariantCulture))
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
    # Heartbeat expiry stops motion. Closing the app ends the managed driver session.
    [void]$driver.WaitForExit(21000)
    $driver.Dispose()
}
