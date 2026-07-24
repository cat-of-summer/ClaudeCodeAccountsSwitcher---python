$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "Python not found. Install Python 3.10+ and retry." }

Write-Host "Python: $python"

$scratch = Join-Path $root "build\__pycache__"
$env:PYTHONPYCACHEPREFIX = $scratch

if ($env:SKIP_TESTS -ne "true") {
    & $python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw "tests failed, build stopped" }
}

& $python -m pip install --upgrade --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw "could not install pyinstaller" }

& $python -m PyInstaller --clean --noconfirm --distpath dist --workpath $scratch build/ccas.spec
if ($LASTEXITCODE -ne 0) { throw "build failed" }

Write-Host ""
Write-Host "Artifacts in $root\dist:"
Get-ChildItem dist
