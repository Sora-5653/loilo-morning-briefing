$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-classroom\Scripts\python.exe'
Push-Location $projectRoot
try {
    if (Test-Path -LiteralPath $python) {
        & $python -X utf8 -m loilo_briefing.classroom collect
    }
    else {
        & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -PythonArgs @('-m', 'loilo_briefing.classroom', 'collect')
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }
