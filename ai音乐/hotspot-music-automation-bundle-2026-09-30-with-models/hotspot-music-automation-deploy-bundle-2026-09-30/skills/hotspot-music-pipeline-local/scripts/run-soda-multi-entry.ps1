param(
    [string]$Date = (Get-Date -Format 'yyyy-MM-dd'),
    [int]$MinutesPerEntry = 60,
    [int]$SampleIntervalSeconds = 30,
    [int]$MaxSwipesPerEntry = 120
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = 'D:\projects\local\hotspot-music-runtime'
$skillRoot = Split-Path -Parent $PSScriptRoot
$taskRoot = Join-Path $runtimeRoot "data\tasks\soda-daily\$Date"
$adb = 'D:\Android\Mumu\MuMuPlayer\nx_main\adb.exe'
$device = '127.0.0.1:16384'
$python = Join-Path $runtimeRoot '.venv\Scripts\python.exe'
$dashboard = Join-Path $skillRoot 'scripts\task_dashboard.py'
$sourceConfig = Join-Path $skillRoot 'config\sources.yaml'

if (-not (Test-Path -LiteralPath $adb)) { throw "MuMu ADB not found: $adb" }
if (-not (Test-Path -LiteralPath $python)) { throw "Python runtime not found: $python" }
New-Item -ItemType Directory -Force -Path $taskRoot | Out-Null

function Progress([string]$status, [int]$percent, [string]$stage, [string]$message) {
    & $python $dashboard progress --task-id soda-daily --date $Date --status $status --percent $percent --stage $stage --message $message | Out-Null
}
trap {
    try { Progress 'failed' 100 '汽水三入口任务失败' $_.Exception.Message } catch { }
    throw $_
}

function Run-Adb([string[]]$adbArgs) {
    & $adb @adbArgs
    if ($LASTEXITCODE -ne 0) { throw "ADB command failed: $($adbArgs -join ' ')" }
}

function Get-UiXml {
    $last = ''
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $dump = & $adb '-s' $device 'shell' 'uiautomator' 'dump' '/sdcard/window.xml' 2>&1
        $last = ($dump -join ' ')
        if ($dump -match 'UI hierchary dumped') {
            $localXml = Join-Path $taskRoot 'visible-ui.xml'
            & $adb '-s' $device 'pull' '/sdcard/window.xml' $localXml 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $localXml)) {
                try {
                    $raw = Get-Content -LiteralPath $localXml -Raw -Encoding utf8
                    if ($raw -match '<hierarchy') { return [xml]$raw }
                } catch { $last = $_.Exception.Message }
            }
        }
        Start-Sleep -Seconds 3
    }
    throw "汽水音乐 UI dump 失败: $last"
}

function Get-VisibleSongs([xml]$xml, [string]$entry, [int]$rankOffset) {
    $songs = @()
    $artists = $xml.SelectNodes("//*[@resource-id='com.luna.music:id/ahd']")
    foreach ($artistNode in $artists) {
        $texts = @($artistNode.ParentNode.SelectNodes('.//*[@text]') | ForEach-Object { [string]$_.text } | Where-Object { $_.Trim() })
        if ($texts.Count -lt 2) { continue }
        $artistText = $texts[-1].Trim()
        $title = ($texts | Where-Object { $_ -ne $artistText } | Select-Object -Last 1).Trim()
        if (-not $title -or $title -in @('播放全部','热歌榜','新歌榜','欧美榜')) { continue }
        $songs += [ordered]@{
            title = $title
            artist = ($artistText -replace '^\s*','').Trim()
            rank = $rankOffset + $songs.Count + 1
            entry = $entry
            page = 'soda://android-visible-ui/discover'
            captured_at = (Get-Date).ToUniversalTime().ToString('o')
            source_mode = 'android_adb_visible_ui'
            license_status = 'metadata_only_public'
        }
    }
    return $songs
}

function Get-DiscoverSongs([xml]$xml, [hashtable]$entry, [int]$rankOffset) {
    $songs = @()
    if ($entry.id -eq 'hot-chart') {
        foreach ($node in @($xml.SelectNodes("//*[@resource-id='com.luna.music:id/dbn']"))) {
            $title = ([string]$node.text).Trim()
            if ($title) {
                $songs += [ordered]@{ title=$title; artist='汽水音乐热歌榜'; rank=$rankOffset+$songs.Count+1; entry=$entry.label; page='soda://android-visible-ui/discover'; captured_at=(Get-Date).ToUniversalTime().ToString('o'); source_mode='android_adb_visible_ui'; license_status='metadata_only_public' }
            }
        }
    } else {
        $cards = @($xml.SelectNodes("//*[@resource-id='com.luna.music:id/dab']"))
        $index = if ($entry.id -eq 'left-dynamic') { 0 } else { 2 }
        if ($cards.Count -gt $index) {
            $title = ([string]$cards[$index].text).Trim()
            $sub = $cards[$index].ParentNode.SelectSingleNode(".//*[@resource-id='com.luna.music:id/daa']")
            $artist = if ($sub) { ([string]$sub.text).Trim() } else { '汽水音乐公开入口' }
            if ($title) { $songs += [ordered]@{ title=$title; artist=$artist; rank=$rankOffset+1; entry=$entry.label; page='soda://android-visible-ui/discover'; captured_at=(Get-Date).ToUniversalTime().ToString('o'); source_mode='android_adb_visible_ui'; license_status='metadata_only_public' } }
        }
    }
    return $songs
}

function Save-Screenshot([string]$entryId) {
    $path = Join-Path $taskRoot "soda-$entryId-$Date.png"
    & $adb '-s' $device 'exec-out' 'screencap' '-p' > $path
    if (-not (Test-Path -LiteralPath $path)) { throw "Screenshot was not written: $path" }
    return $path
}

Progress 'running' 5 '连接 MuMu' '正在连接 MuMu ADB 并打开汽水音乐。'
& $adb connect $device | Out-Null
$deviceLine = & $adb devices | Select-String ([regex]::Escape($device))
if (-not $deviceLine -or $deviceLine.ToString() -notmatch '\bdevice\s*$') { throw 'MuMu ADB device is not online' }
Run-Adb @('-s', $device, 'shell', 'monkey', '-p', 'com.luna.music', '1') | Out-Null
Start-Sleep -Seconds 5
# 汽水首次启动可能显示公开页面上的 VIP 促销遮罩；只点击其可见关闭按钮，
# 不登录、不勾选协议、不绕过任何验证。
Start-Sleep -Seconds 5
# 启动后可能停留在上次播放页；先用可见返回键回到发现页，再点击发现标签。
Run-Adb @('-s', $device, 'shell', 'input', 'keyevent', '4')
Start-Sleep -Seconds 3
Run-Adb @('-s', $device, 'shell', 'input', 'tap', '205', '1848')
Start-Sleep -Seconds 8
# 右下角浮动迷你播放器会阻塞 MuMu UIAutomator idle；只关闭浮动控件，不影响歌曲浏览。
Run-Adb @('-s', $device, 'shell', 'input', 'tap', '1053', '1380')
Start-Sleep -Seconds 2

$entries = @(
    @{ id = 'left-dynamic'; label = '第一行左侧第一个动态热点曲目'; x = 220; y = 350 },
    @{ id = 'hot-chart'; label = '第一行中间热歌榜/思念曲目'; x = 580; y = 350 },
    @{ id = 'cover'; label = '第一行右侧爆火翻唱'; x = 1010; y = 350 }
)
$all = @()
$perEntrySeconds = [math]::Max(60, $MinutesPerEntry * 60)
$entryIndex = 0
foreach ($entry in $entries) {
    $entryIndex++
    $start = Get-Date
    $entrySongs = @()
    $swipeCount = 0
    Progress 'running' ([math]::Min(95, 5 + (($entryIndex - 1) * 30))) "进入 $($entry.label)" "按固定第一行位置打开入口；标题变化不影响选择。"
    if ($entryIndex -gt 1) {
        # Each entry is selected from the Discover grid; return there before
        # tapping the next fixed-position card.
        Run-Adb @('-s', $device, 'shell', 'input', 'keyevent', '4')
        Start-Sleep -Seconds 3
        Run-Adb @('-s', $device, 'shell', 'input', 'tap', '205', '1848')
        Start-Sleep -Seconds 3
        Run-Adb @('-s', $device, 'shell', 'input', 'tap', '1053', '1380')
        Start-Sleep -Seconds 2
    }
    $discoverXml = Get-UiXml
    foreach ($song in @(Get-DiscoverSongs $discoverXml $entry $entrySongs.Count)) { $entrySongs += $song }
    Run-Adb @('-s', $device, 'shell', 'input', 'tap', [string]$entry.x, [string]$entry.y)
    Start-Sleep -Seconds 4
    # 进入入口后也可能再次弹出登录/VIP促销遮罩；只关闭可见关闭按钮。
    Run-Adb @('-s', $device, 'shell', 'input', 'tap', '835', '978')
    Start-Sleep -Seconds 2
    $screenshot = Save-Screenshot $entry.id
    do {
        if (((Get-Date) - $start).TotalSeconds -lt $perEntrySeconds -and $swipeCount -lt $MaxSwipesPerEntry) {
            Run-Adb @('-s', $device, 'shell', 'input', 'swipe', '960', '820', '960', '420', '600')
            $swipeCount++
            Start-Sleep -Seconds ([math]::Max(10, $SampleIntervalSeconds))
        }
    } while (((Get-Date) - $start).TotalSeconds -lt $perEntrySeconds -and $swipeCount -lt $MaxSwipesPerEntry)
    $all += $entrySongs
    $entryJson = [ordered]@{ entry = $entry.label; entry_id = $entry.id; screenshot = $screenshot; items = $entrySongs }
    $entryJson | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $taskRoot "soda-$($entry.id).json") -Encoding UTF8
}

$unique = @($all | Group-Object { "$($_.title)|$($_.artist)|$($_.entry)" } | ForEach-Object { $_.Group[0] })
$payload = [ordered]@{
    page = 'soda://android-visible-ui/discover/multi-entry'
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    source_mode = 'android_adb_visible_ui'
    license_status = 'metadata_only_public'
    official_chart = $false
    entry_count = 3
    minutes_per_entry = $MinutesPerEntry
    items = $unique
    notes = '三个固定第一行入口各浏览指定时长；入口标题允许变化，选择依据为可见位置。'
}
$browserJson = Join-Path $taskRoot 'soda-browser.json'
$payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $browserJson -Encoding UTF8
if ($unique.Count -eq 0) {
    throw '三个汽水入口已通过 ADB 浏览并保存截图，但歌曲播放页的 UIAutomator 无法读取歌曲/歌手文本，未取得可用公开元数据；本次不生成空日报。'
}
Progress 'running' 90 '生成汽水日报' "三个入口浏览完成，共记录 $($unique.Count) 条去重公开歌曲。"
& $python (Join-Path $skillRoot 'scripts\pipeline.py') trend-ingest --sources $sourceConfig --only soda-song-chart --browser-json $browserJson --browser-source soda-song-chart --output $taskRoot --date $Date
if ($LASTEXITCODE -ne 0) { throw '汽水趋势入库失败' }
foreach ($required in @('trend-snapshot.jsonl','hotspot.json','trend-status.json','trend-report.md')) {
    if (-not (Test-Path -LiteralPath (Join-Path $taskRoot $required))) { throw "Missing Soda artifact: $required" }
}
Progress 'completed' 100 '汽水三入口浏览完成' "三个入口各浏览 $MinutesPerEntry 分钟，已生成公开元数据日报。"
Write-Output "Soda multi-entry workflow completed: $taskRoot"

