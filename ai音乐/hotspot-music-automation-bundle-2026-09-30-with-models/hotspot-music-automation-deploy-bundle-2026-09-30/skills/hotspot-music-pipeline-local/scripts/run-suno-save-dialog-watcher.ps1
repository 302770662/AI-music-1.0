param(
    [int]$PollMilliseconds = 500,
    [switch]$Hidden
)

$ErrorActionPreference = "Stop"
$watcher = "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\suno-save-dialog-watcher.ps1"
if (-not (Test-Path -LiteralPath $watcher)) {
    throw "找不到保存对话框 watcher: $watcher"
}

$existing = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -match [regex]::Escape($watcher) }
if ($existing) {
    Write-Output "Suno 保存对话框 watcher 已在运行。"
    exit 0
}

$args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $watcher, '-PollMilliseconds', $PollMilliseconds)
$windowStyle = if ($Hidden) { 'Hidden' } else { 'Normal' }
Start-Process -FilePath 'PowerShell.exe' -ArgumentList $args -WindowStyle $windowStyle | Out-Null
Write-Output "Suno 保存对话框 watcher 已启动。"
