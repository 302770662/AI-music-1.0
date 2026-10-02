param(
    [string]$Snapshot,
    [string]$Date = (Get-Date -Format 'yyyy-MM-dd'),
    [string]$Hour = (Get-Date -Format 'HH-00'),
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = 'D:\projects\local\hotspot-music-runtime'
$skillRoot = (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $runtimeRoot '.venv\Scripts\python.exe'
$dashboard = Join-Path $skillRoot 'scripts\task_dashboard.py'
$out = Join-Path $runtimeRoot "data\tasks\weibo-hourly\$Date\$Hour"
$logRoot = Join-Path $runtimeRoot 'logs'
$logPath = Join-Path $logRoot 'weibo-hourly-runner.log'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
Add-Content -LiteralPath $logPath -Value ("[{0}] start Date={1} Hour={2}" -f (Get-Date -Format o), $Date, $Hour) -Encoding UTF8
function Set-Progress([string]$status, [int]$percent, [string]$stage, [string]$message) {
    if (Test-Path -LiteralPath $python) {
        & $python $dashboard progress --task-id weibo-hourly --date $Date --hour $Hour --status $status --percent $percent --stage $stage --message $message | Out-Null
    }
}
trap {
    try { Add-Content -LiteralPath $logPath -Value ("[{0}] ERROR {1}" -f (Get-Date -Format o), $_.Exception.Message) -Encoding UTF8 } catch { }
    try { Set-Progress 'failed' 100 '本小时失败' $_.Exception.Message } catch { }
    throw $_
}
New-Item -ItemType Directory -Force -Path $out | Out-Null
Set-Progress 'running' 5 '等待微博扩展快照' "等待 Chrome 扩展自动导出 $Date $Hour 的公开热搜快照。"
$snapshotRoots = @(
    'D:\Suno歌曲下载',
    (Join-Path $env:USERPROFILE 'Downloads')
) | Select-Object -Unique
if (-not (Test-Path -LiteralPath $python)) { throw "Python runtime not found: $python" }
if (-not $env:HOTSPOT_DEEPSEEK_API_KEY) { throw "HOTSPOT_DEEPSEEK_API_KEY is not configured for the hourly task" }
if (-not $Snapshot) {
    $hourStart = [datetimeoffset]::ParseExact("$Date $Hour", 'yyyy-MM-dd HH-mm', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeLocal)
    $hourEnd = $hourStart.AddHours(1)
    $candidates = foreach ($root in $snapshotRoots) {
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -Filter 'weibo-browser-*.json' -File -ErrorAction SilentlyContinue
        }
    }
    $candidate = $null
    $deadline = (Get-Date).AddSeconds([math]::Max(0, $WaitSeconds))
    do {
        $candidates = foreach ($root in $snapshotRoots) {
            if (Test-Path -LiteralPath $root) {
                Get-ChildItem -LiteralPath $root -Filter 'weibo-browser-*.json' -File -ErrorAction SilentlyContinue
            }
        }
        $candidate = $candidates |
            Where-Object {
                try {
                    $raw = Get-Content -Raw -LiteralPath $_.FullName
                    $match = [regex]::Match($raw, '"captured_at"\s*:\s*"([^"]+)"')
                    if (-not $match.Success) { return $false }
                    $capturedForFilter = [datetimeoffset]::Parse($match.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal)
                    $capturedForFilter -ge $hourStart -and $capturedForFilter -lt $hourEnd
                } catch { $false }
            } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $candidate -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 10 }
    } while (-not $candidate -and (Get-Date) -lt $deadline)
    if (-not $candidate) { throw "No fresh Weibo Chrome snapshot for $Date $Hour in $($snapshotRoots -join ', ')" }
    $Snapshot = $candidate.FullName
}
if (-not (Test-Path -LiteralPath $Snapshot)) { throw "Snapshot not found: $Snapshot" }
$rawPayload = Get-Content -Raw -LiteralPath $Snapshot
$payload = $rawPayload | ConvertFrom-Json
if ($payload.page -notmatch '^https://(www\.)?weibo\.com/a/hot/realtime') { throw "Snapshot page is not the Weibo realtime public page" }
if (-not $payload.captured_at -or @($payload.items).Count -eq 0) { throw "Snapshot has no fresh public items" }
$capturedText = ([regex]::Match($rawPayload, '"captured_at"\s*:\s*"([^"]+)"')).Groups[1].Value
if (-not $capturedText) { throw "Snapshot captured_at is missing" }
$captured = [datetimeoffset]::Parse($capturedText, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal)
$now = [datetimeoffset](Get-Date)
$hourStart = [datetimeoffset]::ParseExact("$Date $Hour", 'yyyy-MM-dd HH-mm', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeLocal)
$hourEnd = $hourStart.AddHours(1)
if ($captured -lt $hourStart -or $captured -ge $hourEnd) {
    throw "Snapshot is not from the requested hour $Date $Hour; capture a fresh public page and retry"
}
if (@($payload.items).Count -gt 30) { throw "Snapshot exceeds the public 30-item limit" }
New-Item -ItemType Directory -Force -Path $out | Out-Null
Set-Progress 'running' 10 '导入 Chrome 扩展快照' '已找到本小时微博公开热搜快照，开始导入。'
$normalized = Join-Path $out 'weibo-browser.json'
Copy-Item -LiteralPath $Snapshot -Destination $normalized -Force
& $python (Join-Path $skillRoot 'scripts\pipeline.py') trend-ingest --sources (Join-Path $skillRoot 'config\sources.yaml') --only weibo-realtime --browser-json $normalized --browser-source weibo-realtime --output $out --date $Date
if ($LASTEXITCODE -ne 0) { throw "Weibo trend ingestion failed with exit code $LASTEXITCODE" }
Set-Progress 'running' 45 '微博热搜入库' '公开热搜已入库，开始生成 DeepSeek 摘要与歌词。'
& $python (Join-Path $skillRoot 'scripts\video_curate.py') --input $out --output $out --date $Date --config (Join-Path $skillRoot 'config\runtime.yaml') --generate-lyrics
if ($LASTEXITCODE -ne 0) { throw "Weibo curation failed with exit code $LASTEXITCODE" }
Set-Progress 'running' 75 '生成创作包' 'DeepSeek 梳理、原创歌词和 Suno 提示词已生成，正在校验队列。'
$curationPath = Join-Path $out 'curation.json'
$queueStatusPath = Join-Path $out 'suno-generation\suno-generation-status.json'
if (-not (Test-Path -LiteralPath $curationPath)) { throw "Weibo curation.json was not produced" }
$curation = Get-Content -Raw -LiteralPath $curationPath | ConvertFrom-Json
if ([int]$curation.event_count -le 0 -and @($curation.events).Count -le 0) { throw "DeepSeek produced no usable Weibo events; Suno queue not created" }
foreach ($required in @('generated-lyrics.md', 'suno-prompts.jsonl', 'suno-prompts.md')) {
    if (-not (Test-Path -LiteralPath (Join-Path $out $required))) { throw "Missing creative artifact: $required" }
}
if (-not (Test-Path -LiteralPath $queueStatusPath)) { throw "Suno queue was not created" }
$queue = Get-Content -Raw -LiteralPath $queueStatusPath | ConvertFrom-Json
if ([int]$queue.prompt_count -le 0) { throw "Suno queue contains no prompts" }
Set-Progress 'completed' 100 '本小时完成' "微博公开热搜创作包完成，队列提示词 $($queue.prompt_count) 条。"
Write-Output "Weibo public workflow completed: $out"

