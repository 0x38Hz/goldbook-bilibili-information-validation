$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Import-LocalDotenv {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    $lineNumber = 0
    foreach ($line in [System.IO.File]::ReadLines($Path)) {
        $lineNumber++
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) {
            throw "invalid .env entry at line $lineNumber"
        }
        $name = $line.Substring(0, $separator)
        $value = $line.Substring($separator + 1)
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw "invalid .env variable name at line $lineNumber"
        }
        if ($values.ContainsKey($name)) {
            throw "duplicate .env variable at line $lineNumber"
        }
        $values[$name] = $value
    }
    foreach ($name in $values.Keys) {
        Set-Item -LiteralPath ("Env:{0}" -f $name) -Value $values[$name]
    }
}

$dotenvPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $dotenvPath)) {
    throw '.env is required. Copy .env.example to .env and configure it locally before starting.'
}
Import-LocalDotenv -Path $dotenvPath

$activateScript = Join-Path $projectRoot '.venv\Scripts\Activate.ps1'
if (-not (Test-Path -LiteralPath $activateScript)) {
    throw '.venv was not found. Run .\scripts\setup.ps1 first.'
}

. $activateScript
python -m goldbook serve
