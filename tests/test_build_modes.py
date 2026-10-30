"""Run the real build dispatcher in fake projects; never invoke real PyInstaller."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPECS = {"gui": "SSHDriveMgr.spec", "cli": "SSHDriveMgr-cli.spec", "onedir": "SSHDriveMgr-onedir.spec"}
OUTPUTS = {"gui": "dist/SSHDriveMgr.exe", "cli": "dist/SSHDriveMgr-cli.exe", "onedir": "dist/SSHDriveMgr-onedir/SSHDriveMgr.exe"}
CACHES = ("build/SSHDriveMgr", "build/SSHDriveMgr-cli", "build/SSHDriveMgr-onedir")


def fake_project(tmp_path):
    root = tmp_path / "mode project"
    root.mkdir()
    for cache in CACHES:
        folder = root / cache
        folder.mkdir(parents=True)
        (folder / "old-version").write_text("OLD", encoding="utf-8")
    for output in OUTPUTS.values():
        path = root / output
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("OLD", encoding="utf-8")
    (root / "dist/SSHDriveMgr-onedir/old-extra-file").write_text("OLD", encoding="utf-8")
    (root / "build/keep.log").write_text("KEEP", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src/version.txt").write_text("1.2.3", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts/sync_version.ps1").write_text(
        "Add-Content -LiteralPath (Join-Path $PSScriptRoot '../version-synced') -Value 'yes'\n", encoding="utf-8")
    for spec in SPECS.values():
        (root / spec).write_text("# fake, never executed\n", encoding="utf-8")
    source = (ROOT / "build_dual.ps1").read_text(encoding="utf-8")
    # Replace tooling through normal PowerShell command resolution only. Keep the
    # actual dispatcher, preflight, cleanup, output replacement and checks intact.
    mocks = r'''
$script:fakeBuilt = @{}
function Get-Process { param($ErrorAction) }
function Get-Item {
    param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
    $item = Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    if ($script:fakeBuilt.ContainsKey($item.FullName)) {
        return [pscustomobject]@{
            FullName = $item.FullName; Attributes = $item.Attributes; PSIsContainer = $false;
            Length = $item.Length; LastWriteTimeUtc = $item.LastWriteTimeUtc;
            VersionInfo = [pscustomobject]@{ ProductVersion = '1.2.3' }
        }
    }
    return $item
}
function pyinstaller {
    $spec = $args[-1]
    $calls = Join-Path $PSScriptRoot 'calls.txt'
    if (-not (Test-Path -LiteralPath $calls)) {
        foreach ($cache in @('build/SSHDriveMgr','build/SSHDriveMgr-cli','build/SSHDriveMgr-onedir')) {
            if (Test-Path -LiteralPath (Join-Path $PSScriptRoot $cache)) { throw "Old cache survived before native build: $cache" }
        }
    }
    Add-Content -LiteralPath $calls -Value $spec -Encoding UTF8
    switch ($spec) {
        'SSHDriveMgr.spec' { $relative='dist/SSHDriveMgr.exe'; $cache='build/SSHDriveMgr' }
        'SSHDriveMgr-cli.spec' { $relative='dist/SSHDriveMgr-cli.exe'; $cache='build/SSHDriveMgr-cli' }
        'SSHDriveMgr-onedir.spec' { $relative='dist/SSHDriveMgr-onedir/SSHDriveMgr.exe'; $cache='build/SSHDriveMgr-onedir' }
        default { throw "Unexpected spec: $spec" }
    }
    $output=[System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $relative))
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $output) | Out-Null
    Set-Content -LiteralPath $output -Value ('NEW:'+$spec)
    $script:fakeBuilt[$output]=$true
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot $cache) | Out-Null
    Set-Content -LiteralPath (Join-Path $PSScriptRoot "$cache/new-version") -Value 'NEW'
    if ($spec -eq 'SSHDriveMgr-onedir.spec') {
        $data=Join-Path $PSScriptRoot 'dist/SSHDriveMgr-onedir/_internal/src'
        New-Item -ItemType Directory -Force -Path $data | Out-Null
        Set-Content -LiteralPath (Join-Path $data 'version.txt') -Value '1.2.3'
    }
    $global:LASTEXITCODE=0
}
'''
    assert source.count("Push-Location $PSScriptRoot") == 1
    source = source.replace("Push-Location $PSScriptRoot", mocks + "\nPush-Location $PSScriptRoot")
    (root / "build_dual.ps1").write_text(source, encoding="utf-8")
    return root


def invoke(root, arguments):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell required for dispatcher integration")
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(root / "build_dual.ps1"), *arguments],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=40)


def assert_untouched(root):
    for cache in CACHES:
        assert (root / cache / "old-version").read_text(encoding="utf-8") == "OLD"
    for output in OUTPUTS.values():
        assert (root / output).read_text(encoding="utf-8") == "OLD"
    assert not (root / "calls.txt").exists()
    assert not (root / "version-synced").exists()


@pytest.mark.parametrize("arguments", [[], ["-Help"], ["-h"], ["--help"], ["help"], ["-?"]])
def test_help_has_no_clean_version_sync_or_build_side_effects(tmp_path, arguments):
    root = fake_project(tmp_path)
    result = invoke(root, arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gui|cli|onedir|all" in result.stdout
    assert "No mode:" in result.stdout or arguments == ["-?"]
    assert_untouched(root)


@pytest.mark.parametrize("arguments", [["unknown"], ["gui", "cli"], ["-OneDir"], ["gui", "-NoClean"]])
def test_invalid_or_removed_arguments_fail_before_touching_artifacts(tmp_path, arguments):
    root = fake_project(tmp_path)
    result = invoke(root, arguments)
    assert result.returncode != 0
    assert_untouched(root)


@pytest.mark.parametrize("mode", ["gui", "cli", "onedir", "all"])
@pytest.mark.parametrize("named", [False, True])
def test_mode_dispatch_is_independent_and_always_clears_old_build_versions(tmp_path, mode, named):
    root = fake_project(tmp_path)
    arguments = (["-Mode", mode] if named else [mode]) + ["-SkipStartupCheck"]
    result = invoke(root, arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    selected = list(SPECS) if mode == "all" else [mode]
    assert (root / "calls.txt").read_text(encoding="utf-8-sig").splitlines() == [SPECS[name] for name in selected]
    for name, output in OUTPUTS.items():
        text = (root / output).read_text(encoding="utf-8").strip()
        assert text == ("NEW:" + SPECS[name] if name in selected else "OLD")
    for name, cache in zip(SPECS, CACHES):
        assert not (root / cache / "old-version").exists()
        assert (root / cache / "new-version").exists() is (name in selected)
    assert (root / "build/keep.log").read_text(encoding="utf-8") == "KEEP"
    assert (root / "version-synced").exists()
    assert (root / "dist/SSHDriveMgr-onedir/old-extra-file").exists() is ("onedir" not in selected)
    assert "Build complete:" in result.stdout


def test_release_workflows_and_installer_use_explicit_modes():
    for name in ("manual-build.yml", "auto-release.yml"):
        text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "run: .\\build_dual.ps1 all" in text
        assert "build_dual.ps1 -OneDir" not in text
    installer = (ROOT / "installer/build_installer.ps1").read_text(encoding="utf-8")
    assert "@('gui', 'cli')" in installer
    assert "-Mode" in installer and "$LASTEXITCODE" in installer
