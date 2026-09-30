param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskName = 'HotspotMusic-WeiboHourly-ChromeCollector'
$runner = 'D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\run-weibo-hourly-scheduled.ps1'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed $taskName"
    exit 0
}

if (-not (Test-Path -LiteralPath $runner)) { throw "Runner not found: $runner" }
$powershell = (Get-Command pwsh.exe -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"" -WorkingDirectory 'D:\projects\local\hotspot-music-runtime'
# The extension captures at the top of the hour. Process after a short grace
# period so the fresh JSON exists, while keeping the run aligned to each hour.
$now = Get-Date
$first = $now.Date.AddHours($now.Hour).AddMinutes(5)
if ($first -le $now) { $first = $first.AddHours(1) }
$trigger = New-ScheduledTaskTrigger -Once -At $first -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Process fresh public Weibo Chrome-extension snapshots every hour' -RunLevel Limited -Force | Out-Null
Write-Output "Installed $taskName; first processing run: $first. Chrome must be running with the public Weibo page open and the extension loaded."
