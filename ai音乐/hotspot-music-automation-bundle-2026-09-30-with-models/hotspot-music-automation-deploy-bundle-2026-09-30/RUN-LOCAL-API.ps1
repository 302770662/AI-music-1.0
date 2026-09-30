$ErrorActionPreference = 'Stop'
$BundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $BundleRoot '.venv\Scripts\python.exe'
$Scripts = Join-Path $BundleRoot 'skills\hotspot-music-pipeline-local\scripts'
if (-not (Test-Path -LiteralPath $VenvPython)) { throw '请先运行 SETUP-NEW-PC.ps1。' }
Push-Location $BundleRoot
try { & $VenvPython -m uvicorn creative_api:app --app-dir $Scripts --host 127.0.0.1 --port 8000 --workers 1 }
finally { Pop-Location }
