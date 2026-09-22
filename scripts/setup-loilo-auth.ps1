$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    throw 'Node.js is required. Install Node.js 20 or newer and run npm install in the repository root.'
}
$node = $nodeCommand.Source
$modules = Join-Path $projectRoot 'node_modules'
$script = Join-Path $PSScriptRoot 'loilo-auth-login.cjs'

if (-not (Test-Path -LiteralPath (Join-Path $modules 'playwright'))) {
    throw 'Playwright is not installed. Run npm install in the repository root first.'
}

$env:NODE_PATH = $modules
& $node $script
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
