# 腾讯音乐启明星批量上传助手

这是一个 Chrome MV3 未打包扩展，面向 `https://y.qq.com/venus/#/venus/personal/work/demo-trade/upload`。

## 使用

1. 打开 `chrome://extensions/`，开启“开发者模式”，选择“加载已解压的扩展程序”。
2. 选择目录：`D:\projects\local\hotspot-music-runtime\qq-music-uploader`。
3. 在扩展弹窗中选择 MP3/WAV 和同名 TXT/DOC/DOCX 歌词文件，点击“导入本地队列”。
4. 打开腾讯音乐启明星上传页并登录。
5. 点击页面右上角插件面板的“开始自动上传”。

如果页面已经打开，修改扩展后必须在 `chrome://extensions/` 点击本扩展的“重新加载”，再回到上传页按 `Ctrl+R` 刷新；腾讯音乐是 SPA，单纯切换页面不会让旧内容脚本重新加载。

插件只操作可见 DOM，不读取 Cookie、密码或私有接口。上传属于第三方外部提交，开始按钮是本次队列的明确授权；出现字段或页面结构变化时会停止并显示失败原因。
