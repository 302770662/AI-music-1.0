# DeepSeek API 配置

本包已经包含 DeepSeek 的非敏感接口配置：

- 地址：`https://api.deepseek.com`
- 模型：`deepseek-chat`
- 环境变量：`HOTSPOT_DEEPSEEK_API_KEY`

API Key 不包含在压缩包中，也不应写入脚本、配置、日志或任务文件。新电脑解压后，运行 `SETUP-NEW-PC.ps1 -SetDeepSeekKey`，按提示输入自己的 Key；脚本只把它写入当前 Windows 用户的环境变量。

也可以在 PowerShell 当前会话中临时设置：

```powershell
$env:HOTSPOT_DEEPSEEK_API_KEY = "你的 DeepSeek Key"
```

DeepSeek 是远程服务，因此新电脑需要网络、有效 Key 和可用账户余额。没有 Key 或请求失败时，程序会记录 `unavailable`/`failed`，不会伪装成已生成。
