param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug',
    [ValidateSet('Planned', 'Encoder', 'Simulated')]
    [string]$Feedback = 'Planned',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$FullApp,
    [string]$RobotIp,
    [double]$PullOutClearanceM
)

$ErrorActionPreference = 'Stop'
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
if (!$FullApp) {
    if ($PSBoundParameters.ContainsKey('RobotIp') -or $PSBoundParameters.ContainsKey('PullOutClearanceM')) {
        throw 'RobotIp and PullOutClearanceM require -FullApp.'
    }
    $appOptions += '--flex-only'
}
if ($PSBoundParameters.ContainsKey('RobotIp')) { $appOptions += @('--robot-ip', $RobotIp) }
if ($PSBoundParameters.ContainsKey('PullOutClearanceM')) {
    if ([double]::IsNaN($PullOutClearanceM) -or [double]::IsInfinity($PullOutClearanceM) -or $PullOutClearanceM -lt 0) {
        throw 'PullOutClearanceM must be finite and nonnegative.'
    }
    $appOptions += @('--pull-out-clearance-m', $PullOutClearanceM.ToString([Globalization.CultureInfo]::InvariantCulture))
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
        if ($FullApp) { $driverOptions.ArgumentList += '--planned-full' }
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
