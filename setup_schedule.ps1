# Registers (or re-registers) the recurring sync in Windows Task Scheduler.
#   .\setup_schedule.ps1                 # every 2 hours
#   .\setup_schedule.ps1 -IntervalHours 1
#   .\setup_schedule.ps1 -Remove
#
# Works without administrator rights: the task is registered under the current
# user with an interactive principal. If the ScheduledTasks cmdlets are denied
# (they need admin on some policies), it falls back to schtasks.exe, which a
# standard user may use for their own tasks.
[CmdletBinding()]
param(
    [int]$IntervalHours = 2,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'CanvasAchieveSync'

function Test-Registered {
    $null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
}

if ($Remove) {
    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop }
    catch { & schtasks.exe /Delete /TN $taskName /F | Out-Null }
    if (Test-Registered) { throw "Could not remove '$taskName'." }
    Write-Output "Removed scheduled task '$taskName'."
    return
}

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
$pwshPath = if ($pwsh) { $pwsh.Source } else { (Get-Command powershell).Source }
$scriptArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$root\run.ps1`""

# --- preferred path: full settings via the ScheduledTasks module -------------
$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $pwshPath -Argument $scriptArgs -WorkingDirectory $root

    # One trigger at logon, one that repeats all day, so it stays fresh whether
    # the machine was rebooted or just left running.
    $atLogon = New-ScheduledTaskTrigger -AtLogOn
    $repeating = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
        -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)

    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RunOnlyIfNetworkAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

    Register-ScheduledTask -TaskName $taskName `
        -Action $action -Trigger $atLogon, $repeating `
        -Principal $principal -Settings $settings `
        -Description 'Syncs Canvas + Achieve coursework into Google Tasks' `
        -Force -ErrorAction Stop | Out-Null

    $registered = Test-Registered
}
catch {
    Write-Output "ScheduledTasks module route failed ($($_.Exception.Message.Trim())); trying schtasks.exe..."
}

# --- fallback: schtasks.exe -------------------------------------------------
if (-not $registered) {
    $tr = "`"$pwshPath`" $scriptArgs"
    # /IT = run only when this user is logged on, which avoids needing to store
    # a password (a standard user cannot register a password-backed task).
    & schtasks.exe /Create /TN $taskName /TR $tr /SC HOURLY /MO $IntervalHours `
        /ST 00:05 /IT /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks.exe failed with exit code $LASTEXITCODE." }

    # Best effort: ask for missed-run catch-up. Harmless if the policy blocks it.
    try {
        $t = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $t.Settings.StartWhenAvailable = $true
        $t.Settings.DisallowStartIfOnBatteries = $false
        $t.Settings.StopIfGoingOnBatteries = $false
        Set-ScheduledTask -InputObject $t -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Output "Note: could not set missed-run catch-up ($($_.Exception.Message.Trim()))."
    }
    $registered = Test-Registered
}

if (-not $registered) { throw "Registration reported success but '$taskName' does not exist." }

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "Registered '$taskName': every $IntervalHours hour(s)."
Write-Output "  next run: $($info.NextRunTime)"
Write-Output "Run now:  Start-ScheduledTask -TaskName $taskName"
Write-Output "Status:   Get-ScheduledTaskInfo -TaskName $taskName"
Write-Output "Logs:     $root\sync.log"
