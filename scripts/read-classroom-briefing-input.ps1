$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-classroom\Scripts\python.exe'
. (Join-Path $PSScriptRoot 'briefing-common.ps1')
Push-Location $projectRoot
try {
    # Use the same interpreter as run-classroom-collector.ps1. Packaged launchers
    # (py.exe from the Python install manager, apps like Claude desktop) can see
    # different virtualized %LOCALAPPDATA% views, so mixing them reads a stale cache.
    if (Test-Path -LiteralPath $python) {
        if (-not (Test-PythonUsable -Command $python)) {
            Write-SourceUnavailable -Source 'classroom' -Reason 'python_start_failed' -PythonExit $LASTEXITCODE
            exit 2
        }
        & $python -X utf8 -m loilo_briefing.classroom briefing-input
    }
    else {
        & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -Source 'classroom' -PythonArgs @('-m', 'loilo_briefing.classroom', 'briefing-input')
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }
