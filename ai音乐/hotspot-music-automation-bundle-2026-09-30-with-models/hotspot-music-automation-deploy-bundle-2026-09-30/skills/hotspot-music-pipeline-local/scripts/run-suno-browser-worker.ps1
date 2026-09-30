param(
 [string]$RuntimeRoot = "D:\projects\local\hotspot-music-runtime",
 [string]$BindHost = "127.0.0.1",
 [int]$Port = 8765,
 [switch]$AutoStartListener,
 [switch]$KeepAlive
)
$ErrorActionPreference = "Stop"
$python = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
$worker = Join-Path $PSScriptRoot "suno_browser_worker.py"
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $worker)) { throw "Worker not found: $worker" }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { exit 0 }
$workerArgs = @('-B', $worker, '--host', $BindHost, '--port', $Port)
if ($AutoStartListener) { $workerArgs += '--autostart-listener' }
& $python @workerArgs
exit $LASTEXITCODE

