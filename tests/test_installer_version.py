"""Installer VERSIONINFO regression; compile tiny fixtures, never run installers."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRECTIVES = {
    "VersionInfoVersion": "{#MyAppVersion}.0",
    "VersionInfoProductVersion": "{#MyAppVersion}.0",
    "VersionInfoTextVersion": "{#MyAppVersion}",
    "VersionInfoProductTextVersion": "{#MyAppVersion}",
}


def installer_version_fields():
    source = (ROOT / "installer/SSHDriveMgr.iss").read_text(encoding="utf-8-sig")
    return {match.group(1): match.group(2).strip() for match in re.finditer(
        r"(?m)^(AppVersion|VersionInfo\w+)\s*=\s*(.*?)\s*$", source)}


def test_inno_sets_numeric_and_text_versions_from_one_source():
    fields = installer_version_fields()
    assert fields["AppVersion"] == "{#MyAppVersion}"
    for key, value in DIRECTIVES.items():
        assert fields.get(key) == value, (key, fields.get(key))
    source = (ROOT / "installer/SSHDriveMgr.iss").read_text(encoding="utf-8-sig")
    assert 'src\\version.txt' in source
    # These are resource strings vs numeric tuples, not a reason to weaken gates.
    script = (ROOT / "installer/build_installer.ps1").read_text(encoding="utf-8")
    assert "$productVersion -cne $version" in script
    assert "$fileVersion -cne $version" in script
    assert "ProductVersion='$productVersion'" in script
    assert "FileVersion='$fileVersion'" in script


def find_iscc():
    if sys.platform != "win32":
        pytest.skip("Native Windows VERSIONINFO check")
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(variable)
        if base:
            for version in ("6", "7"):
                candidate = Path(base) / f"Inno Setup {version}" / "ISCC.exe"
                if candidate.is_file():
                    return str(candidate)
    compiler = shutil.which("ISCC.exe") or shutil.which("iscc")
    if compiler:
        return compiler
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail("CI must install Inno Setup before the installer metadata tests")
    pytest.skip("Inno Setup not installed; static metadata contract still runs")


@pytest.mark.parametrize("release_version", ["1.4.0", "12.34.56"])
def test_native_installer_pe_text_and_fixed_versions_agree(tmp_path, release_version):
    compiler = find_iscc()
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell required to inspect Windows FileVersionInfo")
    fields = installer_version_fields()
    # Reuse the production directives with two versions to reject hardcoding.
    version_lines = "\n".join(f"{key}={value}" for key, value in fields.items())
    script = tmp_path / "metadata.iss"
    script.write_text(
        f'#define MyAppVersion "{release_version}"\n'
        '[Setup]\nAppName=Installer metadata regression\n' + version_lines + '\n'
        'CreateAppDir=no\nUninstallable=no\nCompression=none\n'
        'OutputBaseFilename=metadata\n', encoding="utf-8")
    result = subprocess.run([compiler, "/Qp", f"/O{tmp_path}", str(script)],
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    installer = tmp_path / "metadata.exe"
    assert installer.is_file()
    environment = dict(os.environ, METADATA_TEST_EXE=str(installer))
    inspect = r"""$v = (Get-Item -LiteralPath $env:METADATA_TEST_EXE).VersionInfo
    [pscustomobject]@{
      ProductVersion=$v.ProductVersion; FileVersion=$v.FileVersion
      ProductParts=@($v.ProductMajorPart,$v.ProductMinorPart,$v.ProductBuildPart,$v.ProductPrivatePart)
      FileParts=@($v.FileMajorPart,$v.FileMinorPart,$v.FileBuildPart,$v.FilePrivatePart)
    } | ConvertTo-Json -Compress
    """
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", inspect],
                            env=environment, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout)
    assert actual["ProductVersion"] == release_version
    assert actual["FileVersion"] == release_version
    expected_parts = [int(value) for value in release_version.split(".")] + [0]
    assert actual["ProductParts"] == actual["FileParts"] == expected_parts
