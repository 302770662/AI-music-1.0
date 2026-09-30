param([string]$ModelRoot = 'D:\projects\local\hotspot-music-runtime\models\effnet-discogs')
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null
# Official source first; optional HTTPS_PROXY is inherited from the environment.
# The legacy 400 head is hosted under music-style/genre_discogs400.
$files = @{
  'genre_discogs400-discogs-effnet-1.pb' = @(
    'https://essentia.upf.edu/models/music-style/genre_discogs400/genre_discogs400-discogs-effnet-1.pb'
  )
  'genre_discogs400-discogs-effnet-1.json' = @(
    'https://essentia.upf.edu/models/music-style/genre_discogs400/genre_discogs400-discogs-effnet-1.json'
  )
}
foreach ($name in $files.Keys) {
  $out = Join-Path $ModelRoot $name
  if (Test-Path -LiteralPath $out) { Write-Host "EXISTS $out"; continue }
  $ok = $false
  foreach ($url in $files[$name]) {
    for ($try = 1; $try -le 4 -and -not $ok; $try++) {
      try {
        Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec 120
        if ((Get-Item -LiteralPath $out).Length -gt 1024) { $ok = $true; Write-Host "DOWNLOADED $out" }
      } catch { Write-Warning "Attempt $try failed for ${name}: $($_.Exception.Message)"; Start-Sleep -Seconds ([Math]::Min(30, 3 * $try)) }
    }
    if ($ok) { break }
  }
  if (-not $ok) { if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }; Write-Warning "UNAVAILABLE $name" }
}
