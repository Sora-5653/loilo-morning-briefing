param(
    [Parameter(Mandatory = $true)]
    [string[]]$PythonArgs
)

$ErrorActionPreference = 'Stop'
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
    try {
        & $candidate.Command @prefixArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' *> $null
        $usable = $LASTEXITCODE -eq 0
    }
    catch {
        $usable = $false
    }
    if ($usable) {
        & $candidate.Command @prefixArgs -X utf8 @PythonArgs
        exit $LASTEXITCODE
    }
}

throw 'Python 3.11 or newer is required for the LoiLoNote collector.'
