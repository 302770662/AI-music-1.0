# 微博公开热搜采集扩展

该扩展只读取当前可见微博页面的公开 DOM，并导出最多 30 条热点元数据。它不读取 Cookie、密码、本地存储，不调用私有接口，不绕过验证码、登录、风控或付费限制，也不下载微博媒体。

## 安装

1. 打开 Chrome 的 `chrome://extensions/`。
2. 开启“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择：`D:\projects\local\hotspot-music-runtime\weibo-public-collector`。
5. 打开 `https://weibo.com/a/hot/realtime`，点击扩展图标。

扩展会下载 `weibo-browser-YYYY-MM-DD-HH-mm.json` 到 Chrome 默认下载目录。

安装或重新加载扩展后会建立一个与北京时间整点对齐的 Chrome 扩展闹钟，每小时整点自动采集微博实时热点页；如果页面未打开，扩展会在 Chrome 中自动创建一个后台标签页，并在整点后 1、2、3 分钟自动重试。它不会读取页面 Cookie/密码/本地存储；页面出现验证码/风控或没有公开条目时，该小时记录失败，不导出快照。Chrome 本身必须保持运行，重新加载扩展后即可无人值守。

## 每小时接续创作包

以普通用户权限安装本地每小时处理任务：

```powershell
& "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\install-weibo-hourly-collector.ps1"
```

它在每小时整点后约 5 分钟处理扩展自动导出的当前小时 JSON，并运行公开热点采集、DeepSeek 梳理、原创歌词和 Suno 创作包；脚本最多等待 3 分钟接收扩展快照。Chrome 下载目录为 `D:\Suno歌曲下载` 时会自动读取该目录；没有当前小时的新鲜 JSON 会失败，不会复用旧快照。卸载：

```powershell
& "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\install-weibo-hourly-collector.ps1" -Uninstall
```

## 接续完整流程

```powershell
& "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\run-weibo-browser-ingest.ps1"
```

脚本会选择最新导出的 JSON，写入 `data\tasks\weibo-hourly\YYYY-MM-DD\HH-mm\`，然后固定运行“公开元数据 → DeepSeek 摘要/去重 → DeepSeek 原创歌词 → 音乐画像融合 → Suno prompt → 6 倍 Suno 本地队列”。没有新鲜 JSON、DeepSeek Key 或可用事件时会失败，不复用旧快照。队列只表示进入本地 Suno 队列，不代表已经提交官网。
