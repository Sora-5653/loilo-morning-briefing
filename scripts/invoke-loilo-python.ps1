param(
    [Parameter(Mandatory = $true)]
    [string[]]$PythonArgs,
    # Source name for the fallback JSON when no usable Python is found.
    [string]$Source = 'loilonote'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'briefing-common.ps1')

$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $candidates += @{ Command = 'py'; Prefix = @('-3') }
}
foreach ($path in @(
    (Join-Path $env:LOCALAPPDATA 'Python\bin\python.exe')
)) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $candidates += @{ Command = $path; Prefix = @() }
    }
}
foreach ($command in @('python', 'python3')) {
    if (Get-Command $command -ErrorAction SilentlyContinue) {
        $candidates += @{ Command = $command; Prefix = @() }
    }
}

foreach ($candidate in $candidates) {
    $prefixArgs = $candidate.Prefix
    if (Test-PythonUsable -Command $candidate.Command -Prefix $prefixArgs) {
        & $candidate.Command @prefixArgs -X utf8 @PythonArgs
        exit $LASTEXITCODE
    }
}

[Console]::Error.WriteLine('Python 3.11 or newer is required for the morning briefing collectors.')
Write-SourceUnavailable -Source $Source -Reason 'python_unavailable'
exit 2
