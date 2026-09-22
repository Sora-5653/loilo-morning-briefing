$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -PythonArgs @('-m', 'loilo_briefing', 'briefing-input')
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
