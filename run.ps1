# Wrapper the scheduled task calls. Keeps the venv detail out of Task Scheduler.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$root\.venv\Scripts\python.exe" "$root\sync.py" @args
exit $LASTEXITCODE
