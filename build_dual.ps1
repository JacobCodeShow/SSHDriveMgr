# build_dual.ps1
# Main build script for SSHDiskManager.
#
# Builds:
#   - SSHDriveMgr.exe      (GUI, onefile)
#   - SSHDriveMgr-cli.exe  (CLI companion, onefile, console subsystem)
#   - SSHDriveMgr-onedir\  (GUI, onedir — only with -OneDir)
#
# Usage:
#   .\build_dual.ps1              # onefile GUI + CLI only (default, fast)
#   .\build_dual.ps1 -OneDir      # onefile + onedir GUI + CLI  (recommended
#                                 # for development / testing — onedir cold
#                                 # starts ~3× faster than onefile)
#   .\build_dual.ps1 -OneDir -NoClean   # keep existing build/dist dirs (faster
#                                       # when only iterating on .qss/.py)

param(
    [switch]$OneDir,
    [switch]$NoClean,
    [Alias('h')]
    [switch]$Help
)

# PowerShell only auto-binds the single-dash forms -Help / -h. The GNU-style
# "--help" (and the conventional "-?") are not recognised as parameter names;
# because this is a simple (non-advanced) script they land in $args unbound.
# Detect them manually so `.\build_dual.ps1 --help` behaves as users expect
# instead of falling through to a full build.
$ShowHelp = $Help -or
    ($args -contains '--help') -or ($args -contains '-?') -or ($args -contains '--?')

if ($ShowHelp) {
    @'
build_dual.ps1 - Build SSHDriveMgr executables with PyInstaller.

Usage:
    .\build_dual.ps1                       Build onefile GUI + CLI (default)
    .\build_dual.ps1 -OneDir              Also build the onedir GUI variant
    .\build_dual.ps1 -OneDir -NoClean     Keep build\ and dist\ (faster iteration)
    .\build_dual.ps1 -Help                Show this help

Parameters:
    -OneDir      Additionally build dist\SSHDriveMgr-onedir\ (unpacked folder,
                 ~3x faster cold start than the onefile exe; distribute as zip
                 or via the installer).
    -NoClean    Skip deleting build\ and dist\ before building.
    -Help (-h)  Show this message and exit.

Steps:
    1. Sync file_version_info.txt from src\version.txt
    2. Terminate any running SSHDriveMgr instances (release file locks)
    3. Clean build\ and dist\ (unless -NoClean)
    4. Build dist\SSHDriveMgr.exe          (GUI, onefile)
    5. Build dist\SSHDriveMgr-cli.exe      (CLI companion, console)
    6. Build dist\SSHDriveMgr-onedir\      (GUI, onedir; only with -OneDir)

The PyInstaller executable is taken from .venv\Scripts\ when present.
'@
    exit 0
}

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve the venv pyinstaller (consistent with how PyInstaller hooks run)
$pyinstaller = if (Test-Path ".venv/Scripts/pyinstaller.exe") {
    ".venv/Scripts/pyinstaller.exe"
} else {
    "pyinstaller"
}

# 1) Version sync
Write-Host "[1/5] Syncing version from src\version.txt..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "scripts\sync_version.ps1")

# 2) Kill running instances so dist files aren't locked
Write-Host "[2/5] Terminating existing instances..." -ForegroundColor Cyan
Stop-Process -Name "SSHDriveMgr"    -Force -ErrorAction SilentlyContinue
Stop-Process -Name "SSHDriveMgr-cli" -Force -ErrorAction SilentlyContinue
Stop-Process -Name "SSHDriveMgrDebug" -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800

# 3) Clean artifacts — but only if not asked to preserve
if ($NoClean) {
    Write-Host "[3/5] Skipping clean (-NoClean specified)." -ForegroundColor DarkGray
} else {
    Write-Host "[3/5] Cleaning build artifacts..." -ForegroundColor Cyan
    Remove-Item -Path build, dist -Recurse -Force -ErrorAction SilentlyContinue
}

# 4) Onefile GUI
Write-Host "[4/5] Building SSHDriveMgr.exe (GUI, onefile)..." -ForegroundColor Cyan
& $pyinstaller --noconfirm SSHDriveMgr.spec

# 5) CLI companion (console subsystem so stdin/stdout stay in caller terminal)
Write-Host "[5/5] Building SSHDriveMgr-cli.exe (console)..." -ForegroundColor Cyan
& $pyinstaller --noconfirm SSHDriveMgr-cli.spec

# Optional: onedir GUI — much faster cold start, good for dev iteration
if ($OneDir) {
    Write-Host "[6/6] Building SSHDriveMgr-onedir (GUI, onedir)..." -ForegroundColor Cyan
    & $pyinstaller --noconfirm SSHDriveMgr-onedir.spec
    Write-Host ""
    Write-Host "Done!" -ForegroundColor Green
    Write-Host "  onefile  : dist\SSHDriveMgr.exe" -ForegroundColor Green
    Write-Host "  CLI      : dist\SSHDriveMgr-cli.exe" -ForegroundColor Green
    Write-Host "  onedir   : dist\SSHDriveMgr-onedir\SSHDriveMgr.exe" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Done!" -ForegroundColor Green
    Write-Host "  onefile  : dist\SSHDriveMgr.exe" -ForegroundColor Green
    Write-Host "  CLI      : dist\SSHDriveMgr-cli.exe" -ForegroundColor Green
    Write-Host ""
    Write-Host "Tip: add -OneDir for the fast-cold-start onedir variant." -ForegroundColor DarkGray
}
