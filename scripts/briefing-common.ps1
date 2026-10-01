# Shared helpers for the collector and briefing-input scripts.
# Dot-source this file; it defines functions only.

# Emit the same JSON shape the Python readers use, so the morning brief always
# receives JSON even when Python itself cannot start. Reason codes are fixed
# strings; no paths, user names, or error text are printed.
function Write-SourceUnavailable {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Reason,
        [int]$PythonExit = -1
    )
    $report = [ordered]@{ source = $Source; status = 'unavailable'; reason = $Reason }
    if ($PythonExit -ge 0) { $report.python_exit = $PythonExit }
    # Codex's elevated sandbox runs commands as a separate local user, which
    # cannot see this user's Python install or Windows Credential Manager.
    if ($env:USERNAME -like 'CodexSandbox*') { $report.sandboxed = $true }
    $report | ConvertTo-Json -Compress
}

# True when the interpreter starts and is Python 3.11 or newer.
function Test-PythonUsable {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$Prefix = @()
    )
    try {
        & $Command @Prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}
