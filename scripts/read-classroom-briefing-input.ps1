$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-classroom\Scripts\python.exe'
Push-Location $projectRoot
try {
    # Use the same interpreter as run-classroom-collector.ps1. Packaged launchers
    # (py.exe from the Python install manager, apps like Claude desktop) can see
    # different virtualized %LOCALAPPDATA% views, so mixing them reads a stale cache.
    if (Test-Path -LiteralPath $python) {
        & $python -X utf8 -m loilo_briefing.classroom briefing-input
    }
    else {
        & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -PythonArgs @('-m', 'loilo_briefing.classroom', 'briefing-input')
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }
