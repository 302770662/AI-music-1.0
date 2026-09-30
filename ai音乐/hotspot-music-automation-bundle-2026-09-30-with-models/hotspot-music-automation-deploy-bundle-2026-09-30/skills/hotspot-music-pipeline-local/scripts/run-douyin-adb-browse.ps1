param(
    [string]$Date = (Get-Date -Format 'yyyy-MM-dd'),
    [int]$MaxMinutes = 180,
    [int]$SampleIntervalSeconds = 20,
    [string]$Device = '127.0.0.1:16384'
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = 'D:\projects\local\hotspot-music-runtime'
$skillRoot = 'D:\projects\local\.codex\skills\hotspot-music-pipeline-local'
$taskRoot = Join-Path $runtimeRoot "data\tasks\douyin-browse\$Date"
$adb = 'D:\Android\Mumu\MuMuPlayer\nx_main\adb.exe'
$python = Join-Path $runtimeRoot '.venv\Scripts\python.exe'
$dashboard = Join-Path $skillRoot 'scripts\task_dashboard.py'
$rights = Join-Path $runtimeRoot 'data\rights\douyin-authorized.json'

if (-not (Test-Path -LiteralPath $adb)) { throw "MuMu ADB not found: $adb" }
if (-not (Test-Path -LiteralPath $python)) { throw "Python runtime not found: $python" }
New-Item -ItemType Directory -Force -Path $taskRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $taskRoot 'recordings') | Out-Null

function Progress([string]$status, [int]$percent, [string]$stage, [string]$message) {
    & $python $dashboard progress --task-id douyin-browse --date $Date --status $status --percent $percent --stage $stage --message $message | Out-Null
}
trap {
    try { Progress 'failed' 100 '抖音 ADB 浏览失败' $_.Exception.Message } catch { }
    throw $_
}
function Run-Adb([string[]]$adbArgs) {
    & $adb @adbArgs
    if ($LASTEXITCODE -ne 0) { throw "ADB command failed: $($adbArgs -join ' ')" }
}
function Get-UiXml {
    $last = ''
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        $dumpOutput = & $adb '-s' $Device 'shell' 'uiautomator' 'dump' '/sdcard/douyin-window.xml' 2>&1
        $last = ($dumpOutput -join ' ')
        if ($LASTEXITCODE -eq 0 -or $dumpOutput -match 'UI hierchary dumped') {
            $raw = (& $adb '-s' $Device 'shell' 'cat' '/sdcard/douyin-window.xml') -join "`n"
            if ($raw -and $raw -match '<hierarchy') { return [xml]$raw }
        }
        Start-Sleep -Seconds 2
    }
    throw "抖音 UI dump 失败: $last"
}

function Get-ScreenText([string]$screenshotPath) {
    $code = @'
import sys
try:
    import pytesseract
    from PIL import Image
    text = pytesseract.image_to_string(Image.open(sys.argv[1]), lang="chi_sim+eng")
    print(text.replace("\x00", " ").strip())
except Exception as exc:
    print("OCR_UNAVAILABLE: " + str(exc))
'@
    $output = $code | & $python -c - $screenshotPath 2>$null
    return @($output | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ })
}
function Get-VisibleText([xml]$xml) {
    @($xml.SelectNodes('//*[@text]') | ForEach-Object { [string]$_.text.Trim() } | Where-Object {
        $_ -and $_.Length -ge 2 -and $_ -notin @('首页','朋友','加号','消息','我','推荐','关注','精选','热点','直播','商城')
    } | Select-Object -Unique)
}
function Save-Screenshot([int]$index) {
    $path = Join-Path $taskRoot ("douyin-{0:D5}.png" -f $index)
    & $adb '-s' $Device 'exec-out' 'screencap' '-p' > $path
    return $path
}
function Get-RecordingStatus {
    if (-not (Test-Path -LiteralPath $rights)) { return 'not_authorized' }
    try {
        $value = Get-Content -Raw -LiteralPath $rights | ConvertFrom-Json
        $valid = @($value.items | Where-Object { $_.authorized -eq $true -and $_.expires_at -and ([datetimeoffset]::Parse($_.expires_at) -gt [datetimeoffset]::Now) })
        if ($valid.Count -gt 0) { return 'authorized_not_started' }
    } catch { }
    return 'not_authorized'
}

Progress 'running' 5 '连接 MuMu 抖音' '正在连接 MuMu ADB 并打开抖音首页推荐。'
& $adb connect $Device | Out-Null
if (-not (& $adb devices | Select-String ([regex]::Escape($Device)) | Select-String '\bdevice\s*$')) { throw 'MuMu ADB device is not online' }
Run-Adb @('-s', $Device, 'shell', 'monkey', '-p', 'com.ss.android.ugc.aweme', '1') | Out-Null
$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    $focus = (& $adb '-s' $Device 'shell' 'dumpsys' 'activity' 'activities' 2>$null) -join ' '
    if ($focus -match 'com\.ss\.android\.ugc\.aweme/(?!\.splash)') { $ready = $true; break }
    # 抖音视频渲染期间 dumpsys 可能短暂保留 SplashActivity；先给可见首页足够启动时间，
    # 后续 UI dump/OCR 再决定页面是否真的可读。
    if ($attempt -ge 5) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw '抖音仍停留在启动页，未进入可见首页推荐；请检查 MuMu 网络和抖音页面状态' }
$recordingStatus = Get-RecordingStatus
$items = @()
$seen = [Collections.Generic.HashSet[string]]::new()
$started = Get-Date
$index = 0
Progress 'running' 10 '浏览首页推荐' "已进入抖音首页推荐；录屏状态：$recordingStatus。"

while (((Get-Date) - $started).TotalMinutes -lt $MaxMinutes) {
    $index++
    $screenshot = Save-Screenshot $index
    $texts = @()
    try {
        $xml = Get-UiXml
        $texts = @(Get-VisibleText $xml)
    } catch {
        # 某些 MuMu/抖音视频渲染帧无法被 UIAutomator 锁定，但截图仍是公开可见页面。
        # 使用本地 OCR 记录可见文本，不读取应用私有数据；OCR 失败时保留截图并继续滑动。
        $texts = @(Get-ScreenText $screenshot)
    }
    $title = ($texts | Where-Object { $_.Length -ge 4 -and $_ -notmatch '^\d+(\.\d+)?[万w]?$' -and $_ -notmatch '^(作者声明|合集|章节要点|下一章|点赞|评论|分享|收藏|打开|更多)$' } | Select-Object -First 1)
    $author = ($texts | Where-Object { $_ -match '^@' } | Select-Object -First 1)
    $topics = @($texts | Where-Object { $_ -match '#|话题|推荐|热点' } | Select-Object -First 10)
    $likes = ($texts | Where-Object { $_ -match '^\d+(\.\d+)?[万w]?$' } | Select-Object -First 1)
    $key = "$title|$author"
    if ($title -and $seen.Add($key)) {
        $items += [ordered]@{
            title = $title
            author = $author
            topics = $topics
            likes_display = $likes
            comments_display = $null
            shares_display = $null
            captured_at = (Get-Date).ToUniversalTime().ToString('o')
            page = 'douyin://android-visible-ui/home/recommend'
            position = "visible_frame_$index"
            screenshot = $screenshot
            source_mode = 'android_adb_visible_ui'
            license_status = 'metadata_only_public'
        }
    }
    Progress 'running' ([math]::Min(95, 10 + [int](((Get-Date) - $started).TotalMinutes / [math]::Max(1, $MaxMinutes) * 85))) '浏览首页推荐' "已浏览 $index 个可见画面，记录 $($items.Count) 条去重公开元数据。"
    Run-Adb @('-s', $Device, 'shell', 'input', 'swipe', '960', '820', '960', '300', '500')
    Start-Sleep -Seconds ([math]::Max(5, $SampleIntervalSeconds))
}

if ($items.Count -eq 0) {
    throw '抖音页面可见且已保存截图，但未能读取公开标题/作者元数据；UIAutomator 不可用且本机未安装 Tesseract OCR，不能把截图-only 结果标记为成功。'
}

$payload = [ordered]@{
    page = 'douyin://android-visible-ui/home/recommend'
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    source_mode = 'android_adb_visible_ui'
    license_status = 'metadata_only_public'
    recording_status = $recordingStatus
    items = $items
    notes = '通过 MuMu ADB 可见界面浏览抖音首页推荐；不下载视频或音轨。'
}
$json = Join-Path $taskRoot 'douyin-browser.json'
$payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $json -Encoding UTF8
@{ status = 'ok'; item_count = $items.Count; recording_status = $recordingStatus; captured_at = $payload.captured_at } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskRoot 'trend-status.json') -Encoding UTF8
Progress 'completed' 100 '抖音首页推荐浏览完成' "完成 $MaxMinutes 分钟首页推荐浏览，记录 $($items.Count) 条公开元数据；录屏状态：$recordingStatus。"
Write-Output "Douyin ADB browse completed: $taskRoot"
