param(
    [string]$RuntimeRoot = "D:\projects\local\hotspot-music-runtime",
    [string]$TaskName = "HotspotMusic-SunoDownloadListener"
)

$ErrorActionPreference = "Stop"
$runner = "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\run-suno-download-worker.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "找不到下载 worker 启动脚本: $runner"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -RuntimeRoot `"$RuntimeRoot`" -AutoStartListener -KeepAlive"
$action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Hotspot Music local Suno MP3/Video download listener" -Force | Out-Null
Write-Output "已注册登录启动任务: $TaskName"
