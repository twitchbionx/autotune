# Build autotune.exe on Windows.
#
# Prereqs:
#   - Python 3.10+ on PATH
#   - An Admin PowerShell isn't strictly required for the build itself,
#     but PyInstaller occasionally hits AV heuristics that require it.
#
# Usage:
#     .\build.ps1
#     .\build.ps1 -Clean            # wipe build/ dist/ first
#     .\build.ps1 -SkipInstall      # don't pip install, assume deps present

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

if ($Clean) {
    Write-Step "Cleaning build/ and dist/"
    if (Test-Path build) { Remove-Item -Recurse -Force build }
    if (Test-Path dist)  { Remove-Item -Recurse -Force dist }
}

if (-not $SkipInstall) {
    Write-Step "Installing PyInstaller"
    python -m pip install --upgrade pip
    python -m pip install pyinstaller==6.*
}

Write-Step "Verifying Python + autotune package import"
python -c "import autotune; print('autotune', autotune.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "autotune package failed to import. Are you in the repo root?"
}

Write-Step "Running PyInstaller"
python -m PyInstaller --noconfirm autotune.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $PSScriptRoot "dist\autotune.exe"
if (-not (Test-Path $exe)) {
    throw "Build claimed success but $exe does not exist."
}

$size = "{0:N1}" -f ((Get-Item $exe).Length / 1MB)
Write-Host ""
Write-Host "+-----------------------------------------------------------+"
Write-Host "|  Built: $exe ($size MB)"
Write-Host "|"
Write-Host "|  Quick smoke test (shows help, does not run the tuner):"
Write-Host "|      dist\autotune.exe --help"
Write-Host "|"
Write-Host "|  First real use (prompts for UAC elevation):"
Write-Host "|      dist\autotune.exe run --config config.yaml"
Write-Host "+-----------------------------------------------------------+"
