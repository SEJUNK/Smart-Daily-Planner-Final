# Copies application source into "To be Moved to GIT" (review bundle — originals unchanged).
# Excludes cache, env secrets, export duplicates, docs, and non-runtime assets.

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $root "api\main.py"))) { $root = $PWD.Path }

$destName = "To be Moved to GIT"
$outRoot = Join-Path $root $destName

if (Test-Path $outRoot) {
  Remove-Item -Recurse -Force $outRoot
}
New-Item -ItemType Directory -Path $outRoot -Force | Out-Null

$dirs = @("api", "agents", "tools", "config", "mcp_servers", "ui")
foreach ($d in $dirs) {
  $src = Join-Path $root $d
  if (Test-Path $src) {
    if ($d -eq "tools") {
      robocopy $src (Join-Path $outRoot $d) /E /NFL /NDL /NJH /NJS /nc /ns /np /XF *.pyc *.pyo generate_submission_ppt.py /XD __pycache__ .pytest_cache 2>&1 | Out-Null
    } else {
      robocopy $src (Join-Path $outRoot $d) /E /NFL /NDL /NJH /NJS /nc /ns /np /XF *.pyc *.pyo /XD __pycache__ .pytest_cache 2>&1 | Out-Null
    }
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $d (code $LASTEXITCODE)" }
  }
}

$rootFiles = @(
  "requirements.txt",
  "Dockerfile",
  "deploy.sh",
  "start.bat",
  "start.sh",
  "cloudbuild.yaml",
  "cloudrun-service.yaml",
  "firestore.indexes.json",
  "firestore.rules",
  ".gitignore",
  ".env.example"
)
foreach ($f in $rootFiles) {
  $p = Join-Path $root $f
  if (Test-Path $p) { Copy-Item -LiteralPath $p -Destination (Join-Path $outRoot $f) -Force }
}

$scriptsOut = Join-Path $outRoot "scripts"
New-Item -ItemType Directory -Path $scriptsOut -Force | Out-Null
$self = Join-Path $PSScriptRoot "copy_to_git_bundle.ps1"
if (Test-Path $self) { Copy-Item -LiteralPath $self -Destination (Join-Path $scriptsOut "copy_to_git_bundle.ps1") -Force }

$inc = Join-Path $PSScriptRoot "git_bundle_includes"
if (Test-Path $inc) {
  Get-ChildItem -Path $inc -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $outRoot $_.Name) -Force
  }
}

# Remove any pycache that robocopy might have missed
Get-ChildItem -Path $outRoot -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item -Recurse -Force $_.FullName }

Write-Host "Bundle ready: $outRoot"
