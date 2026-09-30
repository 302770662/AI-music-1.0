# Suno Queue Runner (1.8.50)

本版本明确排除 Suno Advanced 的 `Cowriter prompt`/`Ask anything` 输入框，防止歌词误写到 Cowriter；只有可见且独立的 Lyrics 与 Styles 字段都回读成功后才允许继续。

这是本地热点音乐队列的 Chrome 可见页面执行器，仅处理：

- `bilibili-daily`
- `douyin-curation`
- `weibo-hourly`

汽水音乐、抖音浏览、网易云和 QQ 音乐不会进入此队列。

## 启动

1.8.9 修复了多标签页下后台持续使用旧 `/create` 页面的问题：每次派发前
必须重新确认页面仍为创作页、扩展连接正常且 Lyrics/Styles 表单已就绪；否则
自动选择另一页，避免“已经填入但没有点击”或任务发到空白页。
1.8.12 修复 Styles 被共享卡片中的 Lyrics 邻近标签误过滤、导致两字段定位到同一编辑器的问题；并保留后台 Worker 地址修复和 Suno Advanced 内部滚动卡片兼容：歌词编辑器即使相对浏览器视口坐标为负，
只要节点仍在可见卡片布局中就不会被误判为旧编辑器；仍会排除 `display:none`、
`visibility:hidden` 和零尺寸节点。
当前 Advanced 页面无稳定 Styles placeholder 时，按可见 Styles 区域定位其
专用 textarea，避免把整个 Lyrics 卡片误判为 Styles。
1.8.13 优先使用 Suno 当前稳定的 `aria-label="Lyrics editor"` 精确节点，避免
React 重绘时附近标签兼容逻辑选中旧歌词节点；只有歌词和 Styles 回读一致后才会
进入 `filled` 并执行一次 `Create`。
1.8.14 增加页面级执行锁，防止扩展刷新期间多个 content-script 实例同时写入同一条歌词。
1.8.15 表单定位或回读失败时保留页面现场并直接阻塞当前项，不再自动回队清空并重复粘贴；
连接状态会报告扩展版本，便于确认 Chrome 是否加载了最新代码。
1.8.16 修复看板刷新/扩展重载时 dashboard bridge 对失效连接直接调用 `disconnect()` 导致的
`Extension context invalidated` 报错。
1.8.17 统一错误提示为“保留页面现场并阻塞当前项”，避免把已停止的任务误显示为自动回队。
1.8.18 增加表单状态的最终互斥保护：即使样式定位回退误返回 Lyrics，也会重新从独立
Styles textarea 选择，禁止两个字段引用同一节点。
1.8.19 禁止歌词写入失败后的第二次重试，并移除 Lexical 不接受的直接 DOM 替换；
写入失败立即保留现场并阻塞，避免“粘贴后消失再粘贴”。
1.8.20 统一 Lyrics/Styles 的回读比较规则，兼容 Lexical 换行差异；失败信息增加歌词与
风格匹配结果，避免内容已正确却被格式差异阻塞。
1.8.21 移除 Lyrics 清空时的直接 DOM 替换，改为仅使用 Lexical 可识别的删除操作，
避免页面看似清空但内部仍保留旧歌词，导致下一次输入重复合并。
1.8.22 每条任务开始前优先点击 Suno 自带的 `Clear all form inputs`，同步清空
Lexical 应用状态和页面内容，避免旧歌词与新歌词合并。
1.8.54 在 1.8.53 基础上增加后台下载派发锁：扩展重连、页面重载或 Suno 页面更新时，同一任务的命令租约保持 30 分钟，不会因为轮询再次点击 Download；只有完成/错误回执或任务真正切换后才释放。页面内仍保留跨内容脚本实例锁。临时取消后的 worker 状态会在下载授权有效时自动恢复 `active` 监听；用户点击“停止下载”后仍保持停止。Chrome 个别版本把已收齐字节的 Suno 媒体保持为 `in_progress`、并使用随机 `.tmp` 后缀时，当 `bytesReceived == totalBytes` 扩展提交一次完成回执，worker 仍强制校验 MP3/MP4 文件头后才归档。格式选择按钮和最终 Download 优先使用原生控件 click，并校验最终按钮解除禁用；未生效时最多补一次可见鼠标事件，避免重复点击和超时。下载入口严格排除 `Download Cover Image`、封面图片和 JPEG，并排除封面区域中仅暴露通用 `Download` 标签的图标；兼容歌曲菜单按钮同时使用 `aria-label=\"Download\"` 和可见文字 `Download` 的页面结构；只进入歌曲操作菜单中的精确 `Download` 后选择 `MP3 Audio` 或 `Video/Video Pro`；
不会新建第二个 Create 页面。

1. 在 PowerShell 运行：

   ```powershell
   & "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\run-suno-browser-worker.ps1"
   ```

2. Chrome 打开 `chrome://extensions`，开启“开发者模式”，选择“加载已解压的扩展”，目录选择本目录；在扩展“详细信息”中打开“允许访问文件网址”。修改扩展文件后要点击扩展卡片上的刷新按钮：

   ```text
   D:\projects\local\hotspot-music-runtime\suno-chrome-runner
   ```

3. 打开 `D:\projects\local\hotspot-music-runtime\dashboard.html`，确认队列范围后点击“开始”；这一次点击会同步打开/复用 <https://suno.com/create>，授予本次持续监听自动填写 Lyrics/Styles、点击 `Create` 和接收未来队列的权限；若勾选“开始后自动下载”，也会同时启动独立 MP3/Video 下载监听。直到点击“停止”前，所有新建的 B 站日期队列、抖音梳理队列和微博小时队列都会自动加入，不会漏掉轮询间隔内产生的多个新队列。
4. 看板显示“Queue Runner 已连接”后，才表示扩展已连上可见 Suno 页面。

如需 Windows 登录后自动恢复本地监听，可在 PowerShell 运行：

```powershell
& "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\install-suno-browser-listener.ps1"
```

该任务不会读取凭据；登录启动时会恢复已启用的监听状态。首次仍需在看板点击“开始”建立监听授权；点击“停止”会撤销自动恢复。

## 行为

- “开始”才会启动本批次，默认允许自动点击 `Create`。
- “开始”本身就是本批次自动提交授权，前端不会因复选框状态把 `auto_create` 降为 `false`；点击“停止”后才撤销授权。
- 点击“开始”后，扩展会自动打开 Suno 创作页；如果看板仍提示等待扩展，请在 `chrome://extensions` 中重新加载本扩展。
- 多个 Suno `/create` 页面同时打开时，1.8.6 及以上版本只向已连接且确认存在独立 Lyrics/Styles 字段的页面发送任务，避免发到空白表单；升级后请在 `chrome://extensions/` 点击“重新加载”。
- 每条任务填写前都会先清空 contenteditable；清空会触发 React 删除事件并硬清空 DOM，随后连续多次确认编辑器为空且页面计数为 `0/5000`（或计数消失）。字段识别优先使用当前字段自己的 aria/placeholder/testid；新版页面两个编辑器没有语义属性时，只在同一创作卡片内按几何距离选择另一个独立控件，绝不把单个匿名控件当作 Styles。计数只从当前可见歌词编辑器附近的短 DOM 节点读取，并按几何距离选择最近值，不扫描整页文本，因此旧编辑器或其他卡片残留的 `5000/5000` 不会阻塞新任务。歌词写入使用一次完整的浏览器编辑事件，不再按小块连续复制粘贴；随后在同一可见节点上连续稳定回读，确认 Lyrics、Styles 和标题后才允许 Create。最终提交前必须重新定位当前可见、已连接且可用的 `Create` 按钮，不能点击填表阶段保存的旧节点；提交时执行一次可见的鼠标激活链，并检查按钮是否进入处理中，未被接收则回队。React 重绘导致回读失败时只回队一次并进入短暂冷却，禁止同一条任务立即重复粘贴形成循环。回读不一致或页面计数接近 `5000/5000` 时不会点击 `Create`，而是先清空歌词、风格和标题，再自动回队重试，避免第三条任务继承上一条歌词。
- 执行器一次只处理一个队列变体，成功观察到新的 `/song/<id>` 链接后才记录完成，再继续下一条。
- “停止”不会开始新条目；已点击 `Create` 的条目继续等待结果，尚未提交的条目回到 `queued`。
- 当前队列暂时为空时，worker 保持监听，不会自动退出；扩展服务重连后会自动恢复心跳。可运行 `install-suno-browser-listener.ps1` 注册 Windows 登录启动任务；登录启动会恢复监听，只有上次用户明确启用过自动 Create 且未点击“停止”时才恢复提交权限。

提交判定补充：点击当前可见的 `Create` 后立即记录 `submitted` 并等待新的官方歌曲链接；不能因为按钮在短时间内仍显示可用就清空表单、回队或重复填写。只有明确页面错误、登录/验证码、额度/风控或等待超时才记录失败。
- 页面出现登录、验证码、额度不足、风控或安全提示时会停止并标记 `blocked`。
- 不读取 Cookie、密码、localStorage 或私有接口，不上传源文件，不绕过网站保护。

## 结果

每个队列目录会写入：

- `suno-browser-results.jsonl`：页面观察到的全部歌曲链接回执
- `suno-generation-plan.jsonl`：逐条变体队列
- `suno-generation-status.json`：队列状态和统计
- `suno-generation-report.md`：人可读报告

## 独立 MP3 / Video 下载

生成队列收到真实的 Suno `/song/<id>` 回执后，下载 worker 会在同一个
Suno MP3/MP4 下载使用独立归档规则：校验通过的真实媒体文件统一归档到
`D:\Suno歌曲下载\<song-id>\`，不保存封面图，不转换扩展名，也不与 MP3/Video 混淆。
Chrome 可能先把浏览器临时文件放在系统“下载”目录；worker 完成校验后会自动复制到上述
固定目录，最终文件以 `archive_file` 回执为准。

如开启 Chrome 的“下载前询问每个文件的保存位置”，必须在每次弹出的系统“另存为”窗口中把
保存位置切换到 `D:\Suno歌曲下载\<song-id>\` 后再点击“保存”。要持续无人值守下载，请在 Chrome
下载设置中关闭该询问，并把默认下载位置设为 `D:\Suno歌曲下载`。

如果必须保留 Chrome 的“下载前询问每个文件的保存位置”，可启动本地保存对话框 watcher，
它只识别 Chrome 的“另存为/Save As”窗口并自动点击“保存/Save”；文件仍先落到 Chrome 默认
下载目录，再由 worker 校验并归档：

```powershell
& "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\run-suno-save-dialog-watcher.ps1" -Hidden
```

`suno-generation/suno-download/` 目录幂等建立两个独立任务：`MP3 Audio`
和 `Video`。下载需要在看板单独点击“开始下载”；生成队列完成不等于文件已下载。

下载 worker 启动命令：

```powershell
& "D:\projects\local\.codex\skills\hotspot-music-pipeline-local\scripts\run-suno-download-worker.ps1"
```

扩展通过 Suno 歌曲页的可见菜单执行 `Download -> MP3/MP4 video asset`；选格式后必须等待
Suno 准备媒体，再点击弹窗中的最终 `Download` 或 `Unlock & Download`。随后用 Chrome
`downloads` 回执确认文件已经完成，复制到：

```text
<任务日期或小时目录>\suno-generation\suno-download\files\<song-id>\

验证后的 MP3/MP4 同步归档到：`D:\Suno歌曲下载\<song-id>\`
```

Video/Video Pro 出现 `Generating video...` 时最多保持等待 30 分钟。遇到登录、验证码、额度、风控
或菜单结构无法确认，下载项会标为 `blocked`，不会绕过网站保护。

队列中的 `queued` 只表示本地排队，不表示 Suno 已经生成歌曲；看板中的“当前处理中”是正在等待页面回执的队列项，“已生成歌曲”是已收到真实 Suno 歌曲 ID/链接的独立统计。
