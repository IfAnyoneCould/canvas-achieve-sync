# Checks that the sync is still running and still authorised, and says so out
# loud. The failure this exists for is silent: a revoked Google token kills the
# Tasks push part way through a run, leaving sync.log as the only evidence.
#
#   .\check_sync.ps1              # check now, notify if anything is wrong
#   .\check_sync.ps1 -Quiet       # exit code only, no notification
#   .\check_sync.ps1 -Register    # check every day at 09:30
#   .\check_sync.ps1 -Unregister
[CmdletBinding()]
param(
    [int]$MaxAgeHours = 3,
    [switch]$Quiet,
    [switch]$Register,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logPath = Join-Path $root 'sync.log'
$healthPath = Join-Path $root 'health.log'
$taskName = 'CanvasAchieveSyncCheck'

# A run is a burst of log lines; a gap longer than this starts a new one.
# Cheaper than teaching sync.py to mark its own boundaries.
$runGap = [TimeSpan]::FromMinutes(5)

function Show-Alert {
    param([string]$Text)

    # BurntToast is not installed and a standard user cannot install it
    # machine-wide, so use the tray balloon, which Windows 11 renders as an
    # ordinary notification. The icon has to outlive ShowBalloonTip or nothing
    # is ever drawn.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Warning
        $icon.Visible = $true
        $icon.BalloonTipIcon = 'Warning'
        $icon.BalloonTipTitle = 'Coursework sync needs attention'
        $icon.BalloonTipText = $Text
        $icon.ShowBalloonTip(20000)
        Start-Sleep -Seconds 10
        $icon.Dispose()
    }
    catch {
        Write-Warning "Could not raise a notification: $($_.Exception.Message.Trim())"
    }
}

if ($Unregister) {
    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop }
    catch { & schtasks.exe /Delete /TN $taskName /F | Out-Null }
    Write-Output "Removed scheduled task '$taskName'."
    return
}

if ($Register) {
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    $pwshPath = if ($pwsh) { $pwsh.Source } else { (Get-Command powershell).Source }
    # Not -NonInteractive: the balloon needs the interactive session to appear in.
    $tr = "`"$pwshPath`" -NoProfile -WindowStyle Hidden -File `"$root\check_sync.ps1`""
    & schtasks.exe /Create /TN $taskName /TR $tr /SC DAILY /ST 09:30 /IT /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks.exe failed with exit code $LASTEXITCODE." }
    Write-Output "Registered '$taskName': daily at 09:30."
    Write-Output "Check now: .\check_sync.ps1"
    return
}

if (-not (Test-Path $logPath)) {
    $msg = "sync.log is missing - the sync has never run from $root."
    Write-Warning $msg
    if (-not $Quiet) { Show-Alert $msg }
    exit 1
}

$entries = foreach ($line in Get-Content $logPath) {
    if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+(.*)$') {
        [pscustomobject]@{
            Time    = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
            Level   = $Matches[2]
            Message = $Matches[3].Trim()
        }
    }
}

if (-not $entries) {
    $msg = 'sync.log holds no recognisable entries.'
    Write-Warning $msg
    if (-not $Quiet) { Show-Alert $msg }
    exit 1
}

$last = $entries[-1]
$start = $entries.Count - 1
while ($start -gt 0 -and ($entries[$start].Time - $entries[$start - 1].Time) -le $runGap) { $start-- }
$lastRun = @($entries[$start..($entries.Count - 1)])

$problems = @()

$age = (Get-Date) - $last.Time
if ($age.TotalHours -gt $MaxAgeHours) {
    $problems += 'No run in {0:N1} hours (last was {1:yyyy-MM-dd HH:mm}).' -f $age.TotalHours, $last.Time
}

$revoked = @($lastRun | Where-Object { $_.Message -match 'invalid_grant|expired or revoked' })
if ($revoked) {
    $problems += 'Google authorisation was revoked. Move token.json aside, then: .\run.ps1 --login'
}

# The tell-tale of the silent failure: coursework was collected, then nothing
# was ever pushed, because the Tasks call died on the way out.
$collected = @($lastRun | Where-Object { $_.Message -match '^\d+ item\(s\) to sync' })
$pushed = @($lastRun | Where-Object { $_.Message -match '^Google Tasks:' })
if ($collected -and -not $pushed) {
    $problems += 'The run collected coursework but never reached Google Tasks.'
}

# Anything else the run complained about, minus the auth error already named.
foreach ($e in $lastRun) {
    if ($e.Level -eq 'ERROR' -and $e.Message -notmatch 'invalid_grant|expired or revoked') {
        $problems += $e.Message
    }
}

if ($problems.Count -eq 0) {
    Write-Output ('OK: last run {0:yyyy-MM-dd HH:mm}, {1} line(s), no errors.' -f $last.Time, $lastRun.Count)
    exit 0
}

$summary = $problems -join "`n"
Write-Warning $summary
Add-Content -Path $healthPath -Value ("{0:yyyy-MM-dd HH:mm} {1}" -f (Get-Date), ($problems -join ' | '))
if (-not $Quiet) { Show-Alert $summary }
exit 1
