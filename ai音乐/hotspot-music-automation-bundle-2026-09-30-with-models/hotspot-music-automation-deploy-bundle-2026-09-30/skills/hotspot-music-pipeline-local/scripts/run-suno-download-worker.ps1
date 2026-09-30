param(
    [string]$RuntimeRoot = "D:\projects\local\hotspot-music-runtime",
    [Alias("Host")]
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8766,
    [switch]$AutoStartListener,
    [switch]$KeepAlive
)

$ErrorActionPreference = "Stop"
$python = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
$worker = "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\suno_download_worker.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到本地 Python: $python"
}
if (-not (Test-Path -LiteralPath $worker)) {
    throw "找不到 Suno 下载 worker: $worker"
}

# Avoid two scheduled/manual listeners racing on one port and state directory.
function Test-PortListener {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

if (Test-PortListener) {
    Write-Output "Suno 下载 worker 已在 $BindHost`:$Port 运行，跳过重复启动。"
    exit 0
}

$workerArgs = @('-B', $worker, '--host', $BindHost, '--port', $Port)
if ($AutoStartListener) {
    $workerArgs += '--autostart-listener'
}

do {
    & $python @workerArgs
    if (-not $KeepAlive) {
        break
    }
    Start-Sleep -Seconds 5
    if (Test-PortListener) {
        Write-Output "检测到已有 Suno 下载 worker 接管 $BindHost`:$Port，退出重复守护进程。"
        break
    }
} while ($true)
