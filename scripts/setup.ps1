param(
    [string]$PythonExecutable
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Test-Python312 {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    try {
        $version = & $Executable @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return $LASTEXITCODE -eq 0 -and $version.Trim() -eq '3.12'
    }
    catch {
        return $false
    }
}

$selectedPython = $null
if ($PythonExecutable) {
    if (-not (Test-Python312 -Executable $PythonExecutable)) {
        throw 'The supplied Python executable is not Python 3.12.'
    }
    $selectedPython = [pscustomobject]@{ Executable = $PythonExecutable; PrefixArguments = @() }
}

if ($null -eq $selectedPython) {
    $activePython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $activePython -and (Test-Python312 -Executable $activePython.Source)) {
        $selectedPython = [pscustomobject]@{ Executable = $activePython.Source; PrefixArguments = @() }
    }
}

if ($null -eq $selectedPython) {
    $bundledCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Codex\python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Codex\resources\python\python.exe'),
        (Join-Path $env:USERPROFILE '.codex\python\python.exe')
    )
    foreach ($candidate in $bundledCandidates) {
        if ((Test-Path -LiteralPath $candidate) -and (Test-Python312 -Executable $candidate)) {
            $selectedPython = [pscustomobject]@{ Executable = $candidate; PrefixArguments = @() }
            break
        }
    }
}

if ($null -eq $selectedPython -and (Get-Command py -CommandType Application -ErrorAction SilentlyContinue)) {
    if (Test-Python312 -Executable 'py' -PrefixArguments @('-3.12')) {
        $selectedPython = [pscustomobject]@{ Executable = 'py'; PrefixArguments = @('-3.12') }
    }
}

if ($null -eq $selectedPython) {
    throw 'Python 3.12 was not found. Install Python 3.12, then run this script again.'
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $venvArguments = @($selectedPython.PrefixArguments) + @('-m', 'venv', '.venv')
    & $selectedPython.Executable @venvArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.12 could not create .venv. Resolve the Python error and run setup again.'
    }
}

$pipIndexEnvironmentNames = @(
    'PIP_CONFIG_FILE', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_TRUSTED_HOST',
    'PIP_FIND_LINKS', 'PIP_CERT', 'PIP_CLIENT_CERT'
)
$previousPipEnvironment = @{}
foreach ($name in $pipIndexEnvironmentNames) {
    if (Test-Path -LiteralPath ("Env:{0}" -f $name)) {
        $previousPipEnvironment[$name] = (Get-Item -LiteralPath ("Env:{0}" -f $name)).Value
        Remove-Item -LiteralPath ("Env:{0}" -f $name)
    }
}
$pipIsolationConfig = Join-Path $env:TEMP ("goldbook-pip-" + [guid]::NewGuid().ToString() + ".ini")
Set-Content -LiteralPath $pipIsolationConfig -Value @"
[global]
index-url = https://pypi.org/simple
extra-index-url =
trusted-host =
"@
$env:PIP_CONFIG_FILE = $pipIsolationConfig
try {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw 'pip upgrade failed. Resolve the pip error and run setup again.'
    }

    & $venvPython -m pip install -r requirements.txt -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) {
        throw 'Project dependency installation failed. Resolve the pip error and run setup again.'
    }
}
finally {
    foreach ($name in $pipIndexEnvironmentNames) {
        if (Test-Path -LiteralPath ("Env:{0}" -f $name)) {
            Remove-Item -LiteralPath ("Env:{0}" -f $name)
        }
    }
    foreach ($name in $previousPipEnvironment.Keys) {
        Set-Item -LiteralPath ("Env:{0}" -f $name) -Value $previousPipEnvironment[$name]
    }
    if (Test-Path -LiteralPath $pipIsolationConfig) {
        Remove-Item -LiteralPath $pipIsolationConfig -Force
    }
}

if ($null -eq (Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Warning 'ffmpeg was not found on PATH. Install ffmpeg before processing Bilibili audio.'
}

& $venvPython -m yt_dlp --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'yt-dlp check failed after installation. Re-run setup and inspect pip output.'
}

Write-Host 'Setup complete. Copy .env.example to .env and set local values before starting.'
