<#
.SYNOPSIS
Remove Agents-Core from this user account (Windows).

.DESCRIPTION
  powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 [--dry-run] [--yes] [--keep-data] [--include-credentials]

Deletes the Task Scheduler tasks of the service, its updater and user sync, removes the
Agents-Core entries from client configurations and the managed routing section from instruction
files (backups first), and removes state, data, caches and logs under %USERPROFILE%\.agents-core,
%LOCALAPPDATA% and %APPDATA%. --dry-run lists everything and changes nothing; --keep-data keeps
the user library and histories. The logic is src\uninstall.py; README "Uninstall".
#>
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

$python = $null
foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
    $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $prefix = @($candidate | Select-Object -Skip 1)
    & $command.Source @prefix -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
    if ($LASTEXITCODE -eq 0) { $python = @($command.Source) + $prefix; break }
}
if (-not $python) {
    Write-Error 'uninstall.ps1: Python 3.11 or newer is required.'
    exit 1
}

# Windows cannot delete a process's working directory: run from the profile, not the checkout.
Set-Location $env:USERPROFILE
$env:PYTHONPATH = $repoRoot
$exe = $python[0]
$rest = @($python | Select-Object -Skip 1)
& $exe @rest -P -m src.uninstall @args
exit $LASTEXITCODE
