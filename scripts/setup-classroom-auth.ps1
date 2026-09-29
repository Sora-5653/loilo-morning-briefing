param(
    [Parameter(Mandatory = $true)][string]$ClientSecretPath,
    # Skip the read check that runs right after authorization.
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-classroom\Scripts\python.exe'
$clientFile = (Resolve-Path -LiteralPath $ClientSecretPath).Path
if ($clientFile.StartsWith($projectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Keep the OAuth client JSON outside the repository.'
}
if (-not (Test-Path -LiteralPath $python)) {
    & (Join-Path $PSScriptRoot 'install-classroom.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Push-Location $projectRoot
try {
    & $python -X utf8 -m loilo_briefing.classroom auth-login --client-secret $clientFile
    if ($LASTEXITCODE -ne 0 -or $SkipVerify) { exit $LASTEXITCODE }
    # Confirm the stored authorization can actually read Classroom.
    & $python -X utf8 -m loilo_briefing.classroom collect
    exit $LASTEXITCODE
}
finally { Pop-Location }
