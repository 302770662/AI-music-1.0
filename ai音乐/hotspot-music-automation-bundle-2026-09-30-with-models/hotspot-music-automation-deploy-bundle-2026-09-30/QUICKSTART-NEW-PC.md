# 新电脑使用方法

1. 解压整个目录，不要只复制其中一个子目录。
2. 安装 Python 3.10/3.11，并确保 `python` 命令可用。
3. 在此目录打开 PowerShell，运行：

```powershell
.\SETUP-NEW-PC.ps1 -InstallModelDependencies -SetDeepSeekKey
```

4. 按提示输入自己的 DeepSeek API Key。Key 不会写入压缩包。
5. 初始化完成后，使用包内 `.venv` 运行脚本；本地 API 可用：

```powershell
.\RUN-LOCAL-API.ps1
```

说明：DeepSeek 是联网 API，不是随包离线运行的模型；本地音乐标签和 faster-whisper base 模型已随包提供。若新电脑没有 WSL，Effnet 编码器的 WSL 路径不可用，但 Discogs-MAEST-519 本地标签模型仍可在 Windows 上运行；可根据需要安装 WSL/Ubuntu 和匹配的 Essentia 依赖。
