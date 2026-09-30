param(
    [switch]$InstallModelDependencies,
    [switch]$SetDeepSeekKey
)

$ErrorActionPreference = 'Stop'
$BundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillRoot = Join-Path $BundleRoot 'skills\hotspot-music-pipeline-local'
$ConfigSource = Join-Path $BundleRoot 'config\runtime-portable.yaml'
$ConfigTarget = Join-Path $SkillRoot 'config\runtime.yaml'
$Requirements = Join-Path $SkillRoot 'requirements.txt'
$ModelRequirements = Join-Path $BundleRoot 'requirements-local-models.txt'
$VenvRoot = Join-Path $BundleRoot '.venv'
$Python = Get-Command python -ErrorAction SilentlyContinue

if (-not $Python) {
    throw '未找到 Python。请先安装 Python 3.10 或 3.11，并勾选 Add Python to PATH。'
}

Write-Host "Bundle: $BundleRoot"
& $Python.Source -m venv $VenvRoot
if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败。' }

$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) { throw "虚拟环境 Python 不存在: $VenvPython" }

& $VenvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw '安装基础 Python 工具失败。' }
& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw '安装基础依赖失败。' }

if ($InstallModelDependencies) {
    & $VenvPython -m pip install -r $ModelRequirements
    if ($LASTEXITCODE -ne 0) { throw '安装本地模型依赖失败。请根据新电脑的 CUDA 版本选择匹配的 PyTorch wheel 后重试。' }
}

$portableRoot = $BundleRoot.Replace('\', '/')
$configText = Get-Content -LiteralPath $ConfigSource -Raw
$configText = $configText.Replace('./data', "$portableRoot/data")
$configText = $configText.Replace('./models', "$portableRoot/models")
$configText = $configText.Replace('./downloads', "$portableRoot/downloads")
Set-Content -LiteralPath $ConfigTarget -Value $configText -Encoding utf8

if ($SetDeepSeekKey) {
    $secureKey = Read-Host '请输入 DeepSeek API Key（输入内容不会显示）' -AsSecureString
    $keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr) }
    if ([string]::IsNullOrWhiteSpace($key)) { throw '没有输入 DeepSeek API Key。' }
    [Environment]::SetEnvironmentVariable('HOTSPOT_DEEPSEEK_API_KEY', $key, 'User')
    $env:HOTSPOT_DEEPSEEK_API_KEY = $key
    Write-Host '已为当前 Windows 用户配置 HOTSPOT_DEEPSEEK_API_KEY。'
}

Push-Location $BundleRoot
try {
    & $VenvPython -B (Join-Path $SkillRoot 'scripts\pipeline.py') setup --config $ConfigTarget
    if ($LASTEXITCODE -ne 0) { throw '本地运行时自检失败。' }
}
finally { Pop-Location }

Write-Host ''
Write-Host '部署完成。'
Write-Host "虚拟环境: $VenvPython"
Write-Host '若要使用 DeepSeek，请确认 HOTSPOT_DEEPSEEK_API_KEY 已设置且账户可用。'
Write-Host '若要使用本地音乐标签/ASR模型，请使用 -InstallModelDependencies。'
