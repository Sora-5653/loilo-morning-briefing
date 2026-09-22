$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot '.venv-classroom'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -PythonArgs @('-m', 'venv', $venv)
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m pip install --disable-pip-version-check -e ($projectRoot + '[classroom]')
exit $LASTEXITCODE
