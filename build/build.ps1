$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "Python not found. Install Python 3.10+ and retry." }

Write-Host "Python: $python"

# The version baked into the binary. CCAS_VERSION wins; otherwise a tag build
# in CI takes the tag the toolkit already normalised for us (REF_NAME_NORM,
# e.g. "v1.2.0"), so the release and `ccas --version` cannot disagree.
$version = $env:CCAS_VERSION
if (-not $version -and $env:REF_TYPE -eq "tag" -and $env:REF_NAME_NORM) {
    $version = $env:REF_NAME_NORM
}
if ($version) {
    $version = ($version -split "/")[-1]          # "pkg/v1.2.0" -> "v1.2.0"
    $version = $version -replace "^[vV]", ""      # "v1.2.0"     -> "1.2.0"
}

$versionFile = Join-Path $root "core\version.py"
$versionBackup = "$versionFile.orig"
if ($version) {
    Copy-Item $versionFile $versionBackup -Force
    $stamped = (Get-Content $versionFile -Raw -Encoding UTF8) -replace '(?m)^__version__ = ".*"$', "__version__ = `"$version`""
    [System.IO.File]::WriteAllText($versionFile, $stamped, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Version: $version (stamped into core/version.py for this build)"
}

try {
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
}
finally {
    if (Test-Path $versionBackup) {
        Move-Item $versionBackup $versionFile -Force
    }
}

Write-Host ""
Write-Host "Artifacts in $root\dist:"
Get-ChildItem dist
