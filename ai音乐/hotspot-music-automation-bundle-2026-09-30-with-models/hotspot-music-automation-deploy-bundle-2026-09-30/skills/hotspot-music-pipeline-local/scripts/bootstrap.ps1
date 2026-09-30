param(
  [string]$RuntimeRoot = "D:\projects\local\hotspot-music-runtime",
  [switch]$InstallOptional
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = "C:\Users\Qmy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { throw "Bundled Python not found: $Py" }

New-Item -ItemType Directory -Force -Path $RuntimeRoot, "$RuntimeRoot\cache\pip", "$RuntimeRoot\cache\huggingface", "$RuntimeRoot\cache\torch", "$RuntimeRoot\data", "$RuntimeRoot\models", "$RuntimeRoot\logs" | Out-Null
$env:PIP_CACHE_DIR = "$RuntimeRoot\cache\pip"
$env:HF_HOME = "$RuntimeRoot\cache\huggingface"
$env:HUGGINGFACE_HUB_CACHE = "$RuntimeRoot\cache\huggingface\hub"
$env:TORCH_HOME = "$RuntimeRoot\cache\torch"
$env:XDG_CACHE_HOME = "$RuntimeRoot\cache"

& $Py -m venv "$RuntimeRoot\.venv"
$VenvPy = "$RuntimeRoot\.venv\Scripts\python.exe"
& $VenvPy -m pip install --upgrade pip setuptools wheel
& $VenvPy -m pip install -r "$SkillRoot\requirements.txt"
if ($InstallOptional) {
  & $VenvPy -m pip install faster-whisper transformers datasets peft accelerate bitsandbytes opencv-python pillow pytesseract huggingface_hub
  Write-Host "Essentia-TensorFlow is installed separately because Windows Python 3.12 may not have a compatible wheel."
  & $VenvPy -m pip install essentia-tensorflow
  if ($LASTEXITCODE -ne 0) { Write-Warning "Essentia-TensorFlow was not available for this Python/Windows combination; use WSL2 or Python 3.10/3.11 for Essentia." }
}

@"
HOTSPOT_RUNTIME_ROOT=$RuntimeRoot
PIP_CACHE_DIR=$env:PIP_CACHE_DIR
HF_HOME=$env:HF_HOME
HUGGINGFACE_HUB_CACHE=$env:HUGGINGFACE_HUB_CACHE
TORCH_HOME=$env:TORCH_HOME
HOTSPOT_DEEPSEEK_API_KEY=
HOTSPOT_SUNO_API_URL=
HOTSPOT_SUNO_API_KEY=
HOTSPOT_SUNO_API_POLL_URL_TEMPLATE=
"@ | Set-Content -Encoding ascii "$RuntimeRoot\runtime.env.example"
Write-Host "Installed runtime: $RuntimeRoot"
Write-Host "Python: $VenvPy"
