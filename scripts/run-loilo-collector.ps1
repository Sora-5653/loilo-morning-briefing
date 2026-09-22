param(
    [switch]$IncludeSharedNotes
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $collectorArgs = @('-m', 'loilo_briefing', 'collect')
    if ($IncludeSharedNotes) {
        $collectorArgs += '--include-shared-notes'
    }
    & (Join-Path $PSScriptRoot 'invoke-loilo-python.ps1') -PythonArgs $collectorArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
