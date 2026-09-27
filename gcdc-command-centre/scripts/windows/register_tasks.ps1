<#
.SYNOPSIS
  Registers the GCDC Command Centre scheduled tasks for the signed-in Windows user.

.DESCRIPTION
  GCDC Backup       daily 06:15                            gcdc backup
  GCDC Refresh      daily 06:30, then hourly 08:00-18:00   gcdc refresh
  GCDC Daily Brief  Monday-Friday 07:00                    gcdc brief   (skips QLD public holidays itself)
  GCDC Mailbox Sync every 30 min 06:00-20:00 (optional)    gcdc sync outlook

  Tasks run only while you are signed in (the OneDrive client must be running to upload the
  export for Power BI Service). "Run as soon as possible after a scheduled start is missed" is
  on, so a laptop that was asleep catches up when it wakes. Output goes to logs\gcdc-YYYY-MM.log.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\register_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\windows\register_tasks.ps1 -IncludeMailboxSync
  powershell -ExecutionPolicy Bypass -File scripts\windows\register_tasks.ps1 -Unregister
#>
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$Gcdc = "",
    [switch]$IncludeMailboxSync,
    [switch]$BriefWithAI,
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$Folder = "\GCDC\"
$Names = @("GCDC Backup", "GCDC Refresh", "GCDC Daily Brief", "GCDC Mailbox Sync")

if ($Unregister) {
    foreach ($n in $Names) {
        Unregister-ScheduledTask -TaskName $n -TaskPath $Folder -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host "GCDC scheduled tasks removed."
    return
}

if (-not $Gcdc) { $Gcdc = Join-Path $ProjectDir ".venv\Scripts\gcdc.exe" }
if (-not (Test-Path $Gcdc)) {
    throw "gcdc not found at $Gcdc. Create the venv first (see README: py -3.11 -m venv .venv; .venv\Scripts\pip install -e .)"
}
$Runner = Join-Path $PSScriptRoot "run_gcdc.cmd"
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir "logs") | Out-Null

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

function New-Action([string]$Arguments) {
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$Runner`" `"$Gcdc`" $Arguments`"" -WorkingDirectory $ProjectDir
}

function New-RepeatingDailyTrigger([string]$At, [int]$EveryMinutes, [int]$ForHours) {
    $t = New-ScheduledTaskTrigger -Daily -At $At
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
        -RepetitionDuration (New-TimeSpan -Hours $ForHours)).Repetition
    $t
}

function Register([string]$Name, $Triggers, [string]$Arguments, [string]$Description) {
    Register-ScheduledTask -TaskName $Name -TaskPath $Folder -Action (New-Action $Arguments) -Trigger $Triggers `
        -Principal $Principal -Settings $Settings -Description $Description -Force | Out-Null
    Write-Host "Registered $Folder$Name"
}

Register "GCDC Backup" @(
    (New-ScheduledTaskTrigger -Daily -At "06:15")
) "backup" "Consistent daily copy of the central database (keeps the newest 30)."

Register "GCDC Refresh" @(
    (New-ScheduledTaskTrigger -Daily -At "06:30"),
    (New-RepeatingDailyTrigger -At "08:00" -EveryMinutes 60 -ForHours 10)
) "refresh" "Reconcile, data-quality checks, daily snapshot and Power BI export (deterministic, no AI)."

$BriefArgs = if ($BriefWithAI) { "brief --ai" } else { "brief" }
Register "GCDC Daily Brief" @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "07:00")
) $BriefArgs "Daily BI Agent: morning management brief (runs after the 06:30 refresh)."

if ($IncludeMailboxSync) {
    Register "GCDC Mailbox Sync" @(
        (New-RepeatingDailyTrigger -At "06:00" -EveryMinutes 30 -ForHours 14)
    ) "sync outlook" "Outlook sent items, replies and bounces into the central database (Microsoft Graph, read-only)."
}

Write-Host ""
Write-Host "Done. Test now with:  Start-ScheduledTask -TaskPath '$Folder' -TaskName 'GCDC Refresh'"
Write-Host "Logs: $(Join-Path $ProjectDir 'logs')"
