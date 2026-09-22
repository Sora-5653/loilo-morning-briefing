param([Parameter(Mandatory = $true)][string]$ClientSecretPath)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-classroom\Scripts\python.exe'
$clientFile = (Resolve-Path -LiteralPath $ClientSecretPath).Path
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run scripts/install-classroom.ps1 first.'
}
Push-Location $projectRoot
try {
    & $python -X utf8 -m loilo_briefing.classroom auth-login --client-secret $clientFile
    exit $LASTEXITCODE
}
finally { Pop-Location }
