param(
  [string]$RuntimeRoot = "D:\projects\local\hotspot-music-runtime",
  [int]$Limit = 0,
  [double]$BudgetMinutes = 180,
  [double]$Delay = 2,
  [double]$PollTimeout = 900,
  [double]$PollInterval = 10
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
$QueueRoot = Join-Path $RuntimeRoot "data\tasks"
$LogRoot = Join-Path $RuntimeRoot "logs"
$LogPath = Join-Path $LogRoot "suno-api-worker.log"

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

function Write-WorkerLog([string]$Message) {
  $line = "[{0}] {1}" -f (Get-Date -Format o), $Message
  Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
  Write-Output $line
}

if (-not (Test-Path -LiteralPath $Py)) {
  throw "Python runtime not found: $Py"
}
if (-not (Test-Path -LiteralPath $QueueRoot)) {
  Write-WorkerLog "No task queue directory: $QueueRoot"
  exit 0
}
if ([string]::IsNullOrWhiteSpace($env:HOTSPOT_SUNO_API_URL)) {
  Write-WorkerLog "Skipped: HOTSPOT_SUNO_API_URL is not configured"
  exit 2
}
if ([string]::IsNullOrWhiteSpace($env:HOTSPOT_SUNO_API_KEY)) {
  Write-WorkerLog "Skipped: HOTSPOT_SUNO_API_KEY is not configured"
  exit 2
}

$Script = Join-Path $SkillRoot "scripts\suno_batch_generator.py"
$Arguments = @($Script, "run-api-all", "--input", $QueueRoot, "--budget-minutes", $BudgetMinutes, "--delay", $Delay, "--poll-timeout", $PollTimeout, "--poll-interval", $PollInterval)
if ($Limit -gt 0) { $Arguments += @("--limit", $Limit) }

Write-WorkerLog "Starting authorized Suno API worker"
& $Py -B @Arguments 2>&1 | ForEach-Object {
  Add-Content -LiteralPath $LogPath -Value ("[{0}] {1}" -f (Get-Date -Format o), $_) -Encoding UTF8
  Write-Output $_
}
$ExitCode = $LASTEXITCODE
Write-WorkerLog "Worker finished with exit code $ExitCode"
exit $ExitCode
