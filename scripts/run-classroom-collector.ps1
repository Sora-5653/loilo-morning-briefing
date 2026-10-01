$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-classroom\Scripts\python.exe'
. (Join-Path $PSScriptRoot 'briefing-common.ps1')
Push-Location $projectRoot
try {
    if (Test-Path -LiteralPath $python) {
        # The venv launcher exits 101 when it cannot start its base interpreter
        # (e.g. the base install is not readable for the current user).
        if (-not (Test-PythonUsable -Command $python)) {
            Write-SourceUnavailable -Source 'classroom' -Reason 'python_start_failed' -PythonExit $LASTEXITCODE
            exit 2
        }
        & $python -X utf8 -m loilo_briefing.classroom collect
    }
    else {
        & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -Source 'classroom' -PythonArgs @('-m', 'loilo_briefing.classroom', 'collect')
    }
    exit $LASTEXITCODE
}
finally { Pop-Location }
