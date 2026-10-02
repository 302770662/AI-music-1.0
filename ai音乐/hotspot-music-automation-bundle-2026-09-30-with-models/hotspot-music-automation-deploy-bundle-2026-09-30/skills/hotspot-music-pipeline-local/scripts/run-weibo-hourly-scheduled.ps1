param()

$ErrorActionPreference = 'Stop'
$runner = (Join-Path $PSScriptRoot 'run-weibo-browser-ingest.ps1')
$log = 'D:\projects\local\hotspot-music-runtime\logs\weibo-hourly-scheduler.log'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
$now = Get-Date
$date = $now.ToString('yyyy-MM-dd')
$hour = $now.ToString('HH-00')
Add-Content -LiteralPath $log -Value ("[{0}] dispatch Date={1} Hour={2}" -f $now.ToString('o'), $date, $hour) -Encoding UTF8
try {
    & (Get-Command pwsh.exe -ErrorAction Stop).Source `
        -NoProfile -ExecutionPolicy Bypass -File $runner -Date $date -Hour $hour -WaitSeconds 600 *>&1 |
        ForEach-Object { Add-Content -LiteralPath $log -Value $_ -Encoding UTF8 }
    if ($LASTEXITCODE -ne 0) { throw "runner exit code $LASTEXITCODE" }
    Add-Content -LiteralPath $log -Value ("[{0}] completed Date={1} Hour={2}" -f (Get-Date -Format o), $date, $hour) -Encoding UTF8
    exit 0
} catch {
    Add-Content -LiteralPath $log -Value ("[{0}] failed Date={1} Hour={2}: {3}" -f (Get-Date -Format o), $date, $hour, $_.Exception.Message) -Encoding UTF8
    exit 1
}

