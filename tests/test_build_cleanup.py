"""Exercise real PowerShell cleanup functions against an isolated fake project.

Never runs PyInstaller, removes the real build tree, or changes any ACL.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build_dual.ps1"


def run_cleanup(tmp_path, body):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required to exercise Windows build cleanup")
    source = BUILD.read_text(encoding="utf-8")
    prefix = source.split("function Build-Executable", 1)[0]
    assert "& $pyinstaller" not in prefix
    assert "Push-Location" not in prefix
    script = tmp_path / "cleanup_harness.ps1"
    script.write_text(prefix + "\n" + body, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(script), "gui"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_generated_cache_cleanup_preserves_test_profiles_logs_and_user_files(tmp_path):
    for relative in ("build/SSHDriveMgr", "build/SSHDriveMgr-cli", "build/SSHDriveMgr-onedir", "dist/SSHDriveMgr-onedir"):
        folder = tmp_path / relative
        folder.mkdir(parents=True)
        (folder / "artifact.tmp").write_text("generated", encoding="utf-8")
    profile = tmp_path / "build/full-test-profile-example/SSHDriveMgr/data.db"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"test-only database sentinel")
    log = tmp_path / "build/previous-build.log"
    log.write_text("keep", encoding="utf-8")
    notes = tmp_path / "dist/user-notes.txt"
    notes.write_text("keep", encoding="utf-8")
    run_cleanup(tmp_path, """
        Remove-GeneratedBuildItem 'build/SSHDriveMgr'
        Remove-GeneratedBuildItem 'build/SSHDriveMgr-cli'
        Remove-GeneratedBuildItem 'build/SSHDriveMgr-onedir'
        Remove-GeneratedBuildItem 'dist/SSHDriveMgr-onedir'
    """)
    assert profile.read_bytes() == b"test-only database sentinel"
    assert log.read_text(encoding="utf-8") == "keep"
    assert notes.read_text(encoding="utf-8") == "keep"
    for relative in ("build/SSHDriveMgr", "build/SSHDriveMgr-cli", "build/SSHDriveMgr-onedir", "dist/SSHDriveMgr-onedir"):
        assert not (tmp_path / relative).exists()
    assert (tmp_path / "build").is_dir()
    assert (tmp_path / "dist").is_dir()


def test_cleanup_rejects_roots_unknown_profiles_and_outside_paths(tmp_path):
    sentinel = tmp_path / "build/full-test-profile-example/SSHDriveMgr/data.db"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"untouched")
    run_cleanup(tmp_path, """
        foreach ($candidate in @('build', 'dist', 'build/full-test-profile-example', '../outside')) {
            $rejected = $false
            try { Remove-GeneratedBuildItem $candidate } catch { $rejected = $true }
            if (-not $rejected) { throw "Unexpectedly accepted cleanup: $candidate" }
        }
        $rejected = $false
        try { Get-GeneratedBuildPath '../outside' } catch { $rejected = $true }
        if (-not $rejected) { throw 'Accepted outside-project output' }
    """)
    assert sentinel.read_bytes() == b"untouched"


def test_cleanup_refuses_reparse_paths_without_traversal(tmp_path):
    folder = tmp_path / "build/SSHDriveMgr"
    folder.mkdir(parents=True)
    sentinel = folder / "artifact.tmp"
    sentinel.write_text("keep", encoding="utf-8")
    run_cleanup(tmp_path, """
        function Get-Item {
            param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
            if ($LiteralPath -eq (Join-Path $PSScriptRoot 'build/SSHDriveMgr')) {
                return [pscustomobject]@{ Attributes = [System.IO.FileAttributes]::ReparsePoint }
            }
            Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
        }
        $rejected = $false
        try { Remove-GeneratedBuildItem 'build/SSHDriveMgr' } catch {
            if ($_.Exception.Message -like '*redirected build path*') { $rejected = $true } else { throw }
        }
        if (-not $rejected) { throw 'Accepted redirected cleanup directory' }
    """)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_output_guard_refuses_a_dangling_reparse_path(tmp_path):
    (tmp_path / "dist").mkdir()
    run_cleanup(tmp_path, """
        function Get-Item {
            param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
            if ($LiteralPath -eq [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist/broken.exe'))) {
                return [pscustomobject]@{ Attributes = [System.IO.FileAttributes]::ReparsePoint }
            }
            Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
        }
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'dist/broken.exe')) { throw 'Fixture must be dangling' }
        $rejected = $false
        try { Get-GeneratedBuildPath 'dist/broken.exe' } catch {
            if ($_.Exception.Message -like '*redirected build path*') { $rejected = $true } else { throw }
        }
        if (-not $rejected) { throw 'Accepted dangling redirected output' }
    """)
    assert not (tmp_path / "dist/broken.exe").exists()


def run_fake_build(tmp_path, process_setup="", *, mode="all"):
    """Run the actual script body, but only against fake outputs and fake native tooling."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required to exercise Windows build preflight")
    sentinels = []
    for relative in (
        "build/SSHDriveMgr/sentinel", "build/SSHDriveMgr-cli/sentinel",
        "build/SSHDriveMgr-onedir/sentinel", "dist/SSHDriveMgr.exe",
        "dist/SSHDriveMgr-cli.exe", "dist/SSHDriveMgr-onedir/renamed.exe",
        "dist/SSHDriveMgr-onedir/_internal/renamed.exe",
        "dist/SSHDriveMgr-onedir/_internal/cryptography/_rust.pyd",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"untouched sentinel")
        sentinels.append(path)
    for spec in ("SSHDriveMgr.spec", "SSHDriveMgr-cli.spec", "SSHDriveMgr-onedir.spec"):
        (tmp_path / spec).write_text("# fake spec; never compiled\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/sync_version.ps1").write_text("# fake version sync\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/version.txt").write_text("1.2.3", encoding="utf-8")
    mocks = """
function pyinstaller {
    Set-Content -LiteralPath (Join-Path $PSScriptRoot 'native-started') -Value 'fake only'
    throw 'FAKE_NATIVE_REACHED'
}
""" + process_setup + "\n"
    source = BUILD.read_text(encoding="utf-8")
    assert source.count("Push-Location $PSScriptRoot") == 1
    source = source.replace("Push-Location $PSScriptRoot", mocks + "Push-Location $PSScriptRoot")
    script = tmp_path / "fake_build.ps1"
    script.write_text(source, encoding="utf-8")
    wrapper = tmp_path / "run_fake.ps1"
    assert mode in ("gui", "cli", "onedir", "all")
    flags = f"-Mode {mode} -SkipStartupCheck"
    wrapper.write_text(
        f"try {{ & (Join-Path $PSScriptRoot 'fake_build.ps1') {flags}; throw 'Unexpected success' }} "
        "catch { Write-Output $_.Exception.Message }\n", encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr, sentinels


def assert_clean_caches_and_unselected_dist(tmp_path, sentinels, mode):
    selected_scopes = {
        "gui": "dist/SSHDriveMgr.exe",
        "cli": "dist/SSHDriveMgr-cli.exe",
        "onedir": "dist/SSHDriveMgr-onedir",
    }
    for path in sentinels:
        relative = path.relative_to(tmp_path).as_posix()
        if relative.startswith("build/"):
            assert not path.exists(), f"Cache survived {mode}: {relative}"
        elif mode != "all":
            selected = selected_scopes[mode]
            if relative == selected or relative.startswith(selected + "/"):
                assert not path.exists(), f"Selected output survived replacement: {relative}"
            else:
                assert path.read_bytes() == b"untouched sentinel", relative
    for cache in ("SSHDriveMgr", "SSHDriveMgr-cli", "SSHDriveMgr-onedir"):
        assert not (tmp_path / "build" / cache).exists()


@pytest.mark.parametrize("mode", ["gui", "cli", "onedir", "all"])
def test_every_mode_cleans_all_caches_but_only_replaces_selected_dist(tmp_path, mode):
    output, sentinels = run_fake_build(tmp_path, """
function Get-Process { param($ErrorAction) }
""", mode=mode)
    assert "FAKE_NATIVE_REACHED" in output
    assert (tmp_path / "native-started").exists()
    assert_clean_caches_and_unselected_dist(tmp_path, sentinels, mode)
    if mode == "all":
        # Fake native deliberately aborts the first build: later dist outputs survive.
        assert not (tmp_path / "dist/SSHDriveMgr.exe").exists()
        for path in sentinels:
            if path.relative_to(tmp_path).as_posix().startswith("dist/") and path.name != "SSHDriveMgr.exe":
                assert path.read_bytes() == b"untouched sentinel"


@pytest.mark.parametrize("mode,relative", [
    (mode, relative)
    for selected_mode, relative in (
        ("gui", "dist/SSHDriveMgr.exe"),
        ("cli", "dist/SSHDriveMgr-cli.exe"),
        ("onedir", "dist/SSHDriveMgr-onedir/renamed.exe"),
        ("onedir", "dist/SSHDriveMgr-onedir/_internal/renamed.exe"),
    )
    for mode in (selected_mode, "all")
])
def test_active_output_blocks_every_deletion_and_native_start(tmp_path, mode, relative):
    output, sentinels = run_fake_build(tmp_path, """
function Get-Process {
    param($ErrorAction)
    [pscustomobject]@{ Id = 43210; Path = (Join-Path $PSScriptRoot 'RELATIVE') }
}
""".replace("RELATIVE", relative), mode=mode)
    assert "PID 43210" in output and str(tmp_path / relative) in output
    assert "exit via tray, wait for process to stop, retry" in output
    assert not (tmp_path / "native-started").exists()
    for path in sentinels:
        assert path.read_bytes() == b"untouched sentinel"


@pytest.mark.parametrize("mode", ["gui", "cli", "onedir", "all"])
@pytest.mark.parametrize("cache", ["SSHDriveMgr", "SSHDriveMgr-cli", "SSHDriveMgr-onedir"])
def test_running_intermediate_stub_blocks_every_mode(tmp_path, mode, cache):
    relative = f"build/{cache}/SSHDriveMgr.exe"
    stub = tmp_path / relative
    stub.parent.mkdir(parents=True)
    stub.write_bytes(b"not executed")
    output, sentinels = run_fake_build(tmp_path, """
function Get-Process {
    param($ErrorAction)
    [pscustomobject]@{ Id = 21232; Path = (Join-Path $PSScriptRoot 'RELATIVE') }
}
""".replace("RELATIVE", relative), mode=mode)
    assert stub.read_bytes() == b"not executed"
    assert "PID 21232" in output and str(stub) in output
    assert "exit via tray, wait for process to stop, retry" in output
    assert not (tmp_path / "native-started").exists()
    for path in sentinels:
        assert path.read_bytes() == b"untouched sentinel"


@pytest.mark.parametrize("mode", ["gui", "cli", "onedir", "all"])
def test_non_app_file_lock_blocks_only_selected_onedir(tmp_path, mode):
    output, sentinels = run_fake_build(tmp_path, """
function Get-Process { param($ErrorAction) }
$held = [System.IO.File]::Open((Join-Path $PSScriptRoot 'dist/SSHDriveMgr-onedir/_internal/cryptography/_rust.pyd'),
    [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
""", mode=mode)
    if mode in ("onedir", "all"):
        assert "locked or unreadable" in output and "_rust.pyd" in output
        assert "exit via tray, wait for process to stop, retry" in output
        assert not (tmp_path / "native-started").exists()
        for path in sentinels:
            assert path.read_bytes() == b"untouched sentinel"
    else:
        assert "FAKE_NATIVE_REACHED" in output
        assert (tmp_path / "native-started").exists()
        assert_clean_caches_and_unselected_dist(tmp_path, sentinels, mode)


def test_same_name_other_project_inaccessible_metadata_and_stopped_process_do_not_block(tmp_path):
    other = tmp_path / "other-project/dist/SSHDriveMgr.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other project")
    sibling = tmp_path / "dist/SSHDriveMgr-onedir-other/renamed.exe"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"different tree")
    output, _ = run_fake_build(tmp_path, """
function Get-Process {
    param($ErrorAction)
    $protected = [pscustomobject]@{ Id = 7 }
    $protected | Add-Member -MemberType ScriptProperty -Name Path -Value { throw 'Access denied' }
    $protected
    [pscustomobject]@{ Id = 8; Path = $null }
    [pscustomobject]@{ Id = 9; Path = (Join-Path $PSScriptRoot 'dist/already-exited.exe') }
    [pscustomobject]@{ Id = 10; Path = (Join-Path $PSScriptRoot 'other-project/dist/SSHDriveMgr.exe') }
    [pscustomobject]@{ Id = 11; Path = (Join-Path $PSScriptRoot 'dist/SSHDriveMgr-onedir-other/renamed.exe') }
}
""")
    assert "FAKE_NATIVE_REACHED" in output
    assert (tmp_path / "native-started").exists()
    assert not (tmp_path / "build/SSHDriveMgr").exists()
    assert not (tmp_path / "dist/SSHDriveMgr.exe").exists()
    assert other.read_bytes() == b"other project"
    assert sibling.read_bytes() == b"different tree"


def test_stopped_output_process_allows_retry_and_cleanup(tmp_path):
    run_cleanup(tmp_path, """
        $target = Join-Path $PSScriptRoot 'dist/SSHDriveMgr.exe'
        New-Item -ItemType Directory -Path (Split-Path $target) | Out-Null
        Set-Content -LiteralPath $target -Value 'keep until stopped'
        $script:running = $true
        function Get-Process {
            param($ErrorAction)
            if ($script:running) { [pscustomobject]@{ Id = 123; Path = $target } }
        }
        $blocked = $false
        try { Assert-BuildOutputsAvailable } catch {
            if ($_.Exception.Message -like '*PID 123*') { $blocked = $true } else { throw }
        }
        if (-not $blocked) { throw 'Running output was accepted' }
        $script:running = $false
        Assert-BuildOutputsAvailable
        Remove-Item -LiteralPath $target -ErrorAction Stop
    """)
    assert not (tmp_path / "dist/SSHDriveMgr.exe").exists()


@pytest.mark.parametrize("mode,relative", [
    (mode, relative)
    for mode in ("gui", "cli", "onedir")
    for target_mode, relative in (
        ("gui", "dist/SSHDriveMgr.exe"),
        ("cli", "dist/SSHDriveMgr-cli.exe"),
        ("onedir", "dist/SSHDriveMgr-onedir/renamed.exe"),
        ("onedir", "dist/SSHDriveMgr-onedir/_internal/renamed.exe"),
    )
    if mode != target_mode
])
def test_unselected_dist_process_does_not_block_selected_build(tmp_path, mode, relative):
    output, sentinels = run_fake_build(tmp_path, """
function Get-Process {
    param($ErrorAction)
    [pscustomobject]@{ Id = 333; Path = (Join-Path $PSScriptRoot 'RELATIVE') }
}
""".replace("RELATIVE", relative), mode=mode)
    assert "FAKE_NATIVE_REACHED" in output
    assert (tmp_path / "native-started").exists()
    assert (tmp_path / relative).read_bytes() == b"untouched sentinel"
    assert_clean_caches_and_unselected_dist(tmp_path, sentinels, mode)


def test_released_file_lock_allows_readonly_preflight_retry(tmp_path):
    run_cleanup(tmp_path, """
        function Get-Process { param($ErrorAction) }
        $target = Join-Path $PSScriptRoot 'dist/SSHDriveMgr.exe'
        New-Item -ItemType Directory -Path (Split-Path $target) | Out-Null
        Set-Content -LiteralPath $target -Value 'unchanged'
        $held = [System.IO.File]::Open($target, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        $blocked = $false
        try {
            try { Assert-BuildOutputsAvailable } catch {
                if ($_.Exception.Message -like '*locked or unreadable*') { $blocked = $true } else { throw }
            }
        } finally { $held.Dispose() }
        if (-not $blocked) { throw 'Accepted locked output' }
        Assert-BuildOutputsAvailable
        if ((Get-Content -LiteralPath $target -Raw).Trim() -ne 'unchanged') { throw 'Probe mutated output' }
    """)


@pytest.mark.parametrize("mode,relative", [
    ("gui", "dist/SSHDriveMgr.exe"),
    ("cli", "dist/SSHDriveMgr-cli.exe"),
    ("onedir", "dist/SSHDriveMgr-onedir/renamed.exe"),
    ("all", "dist/SSHDriveMgr-onedir/_internal/renamed.exe"),
])
def test_rechecks_selected_process_immediately_before_output_replace(tmp_path, mode, relative):
    output, sentinels = run_fake_build(tmp_path, """
$script:enumerations = 0
function Get-Process {
    param($ErrorAction)
    $script:enumerations++
    if ($script:enumerations -gt 1) {
        [pscustomobject]@{ Id = 222; Path = (Join-Path $PSScriptRoot 'RELATIVE') }
    }
}
""".replace("RELATIVE", relative), mode=mode)
    assert "PID 222" in output and str(tmp_path / relative) in output
    assert not (tmp_path / "native-started").exists()
    for path in sentinels:
        if path.relative_to(tmp_path).as_posix().startswith("build/"):
            assert not path.exists()
        else:
            assert path.read_bytes() == b"untouched sentinel"


def test_preflight_rejects_nested_reparse_without_deleting_anything(tmp_path):
    nested = tmp_path / "dist/SSHDriveMgr-onedir/_internal"
    nested.mkdir(parents=True)
    sentinel = nested / "keep"
    sentinel.write_bytes(b"untouched")
    run_cleanup(tmp_path, """
        function Get-Process { param($ErrorAction) }
        function Get-ChildItem {
            param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
            $nested = Join-Path $PSScriptRoot 'dist/SSHDriveMgr-onedir/_internal'
            if ($LiteralPath -eq $nested) { throw 'Traversed a nested redirect' }
            if ($LiteralPath -eq (Join-Path $PSScriptRoot 'dist/SSHDriveMgr-onedir')) {
                return [pscustomobject]@{
                    FullName = $nested; Attributes = [System.IO.FileAttributes]::ReparsePoint
                }
            }
            Microsoft.PowerShell.Management\\Get-ChildItem -LiteralPath $LiteralPath -Force -ErrorAction Stop
        }
        $blocked = $false
        try { Assert-BuildOutputsAvailable -IncludeOneDir } catch {
            if ($_.Exception.Message -like '*redirected build path*') { $blocked = $true } else { throw }
        }
        if (-not $blocked) { throw 'Accepted nested redirect' }
    """)
    assert sentinel.read_bytes() == b"untouched"


def test_large_deep_output_validates_root_ancestors_only_once(tmp_path):
    folder = tmp_path / "dist/SSHDriveMgr-onedir"
    for index in range(8):
        folder /= f"nested-{index}"
    folder.mkdir(parents=True)
    for index in range(400):
        (folder / f"artifact-{index}.dat").write_bytes(b"readonly sentinel")
    run_cleanup(tmp_path, """
        $script:pathChecks = 0
        $script:itemChecks = 0
        $script:directoryScans = 0
        $script:originalGeneratedPath = (Get-Command Get-GeneratedBuildPath).ScriptBlock
        function Get-GeneratedBuildPath {
            param([string]$RelativePath)
            $script:pathChecks++
            if ($script:pathChecks -gt 1) { throw 'Repeated root/ancestor validation' }
            & $script:originalGeneratedPath $RelativePath
        }
        function Get-Item {
            param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
            $script:itemChecks++
            if ($script:itemChecks -gt 4) { throw 'Ancestor lookups multiplied with descendants' }
            Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
        }
        function Get-ChildItem {
            param([string]$LiteralPath, [switch]$Force, [string]$ErrorAction)
            $script:directoryScans++
            Microsoft.PowerShell.Management\\Get-ChildItem -LiteralPath $LiteralPath -Force -ErrorAction Stop
        }
        Assert-GeneratedOutputAvailable 'dist/SSHDriveMgr-onedir'
        if ($script:pathChecks -ne 1) { throw 'Root was not validated exactly once' }
        if ($script:directoryScans -ne 9) { throw 'Each directory must be scanned exactly once' }
    """)
    assert len(list(folder.iterdir())) == 400
    assert (folder / "artifact-399.dat").read_bytes() == b"readonly sentinel"


def test_default_clean_does_not_remove_build_or_dist_roots():
    source = BUILD.read_text(encoding="utf-8")
    assert "foreach ($name in @('build', 'dist'))" not in source
    assert "Remove-GeneratedBuildItem $name" in source
    assert "Remove-GeneratedBuildItem 'dist/SSHDriveMgr-onedir'" in source
    assert "Get-GeneratedBuildPath $Output" in source
    assert "Stop-Process" not in source
    assert "Set-Acl" not in source and "icacls" not in source.lower()
