"""Pure mocked VERSIONINFO checks: no compiler, build, installer or PE required."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/artifact_version.ps1"
PARTS = [f"{kind}{part}Part" for kind in ("Product", "File")
         for part in ("Major", "Minor", "Build", "Private")]
POWERSHELLS = list(dict.fromkeys(filter(None, (
    shutil.which("pwsh"), shutil.which("powershell")))))


def version_info(version="1.4.1"):
    numbers = [int(part) for part in version.split(".")] + [0]
    return {"ProductVersion": version, "FileVersion": version,
            **dict(zip(PARTS, numbers * 2))}


def mock_cases():
    cases = []

    def add(name, *, expected="1.4.1", actual="1.4.1", changes=None,
            missing=(), accepted=False, message=None):
        info = version_info(actual)
        info.update(changes or {})
        for field in missing:
            del info[field]
        cases.append(dict(name=name, expected=expected, info=info,
                          accepted=accepted, message=message))

    add("normal", accepted=True)
    add("non-hardcoded", expected="12.34.56", actual="12.34.56", accepted=True)
    add("zero", expected="0.0.0", actual="0.0.0", accepted=True)
    add("upper-bound", expected="65535.65535.65535", actual="65535.65535.65535",
        accepted=True)
    for field in ("ProductVersion", "FileVersion"):
        for label, padding in (("space", " " * 24), ("nul", "\0\0"),
                               ("mixed", " \0  \0 ")):
            add(f"{field}-{label}", changes={field: "1.4.1" + padding}, accepted=True)
        for label, text in (
                ("wrong-version", "1.4.0"),
                ("wrong-version-padded", "1.4.0      \0"),
                ("leading-space", " 1.4.1"),
                ("leading-nul", "\0" + "1.4.1"),
                ("internal-space", "1.4. 1"),
                ("internal-nul", "1.4.\0" + "1"),
                ("zero-fourth-component", "1.4.1.0"),
                ("nonzero-fourth-component", "1.4.1.9"),
                ("suffix", "1.4.1-preview"),
                ("suffix-padded", "1.4.1+build   \0"),
                ("trailing-tab", "1.4.1\t"),
                ("trailing-newline", "1.4.1\n"),
                ("trailing-cr", "1.4.1\r"),
                ("trailing-nbsp", "1.4.1\u00a0"),
                ("trailing-em-space", "1.4.1\u2003"),
                ("tab-before-padding", "1.4.1\t \0"),
                ("empty", ""),
                ("padding-only", " \0 "),
                ("null", None),
                ("non-string", 141)):
            add(f"{field}-{label}", changes={field: text}, message=field)
        add(f"{field}-missing", missing=(field,), message=field)
    add("both-padded", changes={"ProductVersion": "1.4.1   \0 ",
                                "FileVersion": "1.4.1\0  "}, accepted=True)
    for field in PARTS:
        add(f"{field}-wrong", changes={field: version_info()[field] + 1}, message=field)
        add(f"{field}-missing", missing=(field,), message=field)
        add(f"{field}-null", changes={field: None}, message=field)
    for label, value in (("string", "1"), ("fraction", 1.5), ("boolean", True),
                         ("negative", -1), ("overflow", 65536)):
        add(f"fixed-{label}", changes={"ProductMajorPart": value}, message="ProductMajorPart")
    for index, expected in enumerate((
            "", "1", "1.4", "1.4.1.0", "1.4.1.2", "1.4.1-suffix", "v1.4.1",
            " 1.4.1", "1.4.1 ", "1.4.1\n", "1.4.1\0", "1.4.+1", "1.-4.1",
            "1.4.\u0661", "65536.4.1", "1.65536.1", "1.4.65536",
            "999999999999999999999999999999.4.1")):
        add(f"invalid-expected-{index}", expected=expected, message="Invalid expected artifact version")
    cases.append(dict(name="missing-info", expected="1.4.1", info=None,
                      accepted=False, message="missing or null"))
    return cases


# Run every case in a single process per available edition, including Windows
# PowerShell 5.1 when installed. Host diagnostics stay separate from the pipeline.
MOCK_RUNNER = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dotSourceOutput = @(. $env:ARTIFACT_VERSION_HELPER)
if ($dotSourceOutput.Count -ne 0) { throw 'Dot-sourcing produced pipeline output' }
$cases = ConvertFrom-Json -InputObject $env:ARTIFACT_VERSION_CASES
$results = @(foreach ($case in $cases) {
    $accepted = $false
    $failure = ''
    $pipeline = @()
    $information = @()
    try {
        $pipeline = @(Assert-ArtifactVersion -VersionInfo $case.info -Version $case.expected `
            -Artifact 'mock artifact.exe' -InformationVariable information 6>$null)
        $accepted = $true
    } catch {
        $failure = $_.Exception.Message
    }
    [pscustomobject]@{
        name = $case.name
        accepted = $accepted
        pipelineCount = $pipeline.Count
        failure = $failure
        diagnostics = (($information | ForEach-Object { $_.MessageData.ToString() }) -join "`n")
    }
})
$results | ConvertTo-Json -Depth 5 -Compress
"""


@pytest.mark.parametrize("powershell", POWERSHELLS or [None],
                         ids=lambda executable: Path(executable).name if executable else "unavailable")
def test_mock_artifact_version_contract(powershell):
    if powershell is None:
        pytest.skip("pwsh or powershell required for mocked artifact VERSIONINFO tests")
    cases = mock_cases()
    environment = dict(os.environ, ARTIFACT_VERSION_HELPER=str(HELPER),
                       ARTIFACT_VERSION_CASES=json.dumps(cases, ensure_ascii=True))
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", MOCK_RUNNER], env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert len(observed) == len(cases)
    for case, outcome in zip(cases, observed):
        assert outcome["name"] == case["name"]
        assert outcome["accepted"] is case["accepted"], (case, outcome)
        assert outcome["pipelineCount"] == 0, (case["name"], outcome)
        if case["message"]:
            assert case["message"] in outcome["failure"], (case["name"], outcome)
        if not case["name"].startswith("invalid-expected-"):
            for text in ("mock artifact.exe", "expected text='1.4.1'" if case["expected"] == "1.4.1"
                         else f"expected text='{case['expected']}'", "expected fixed=[",
                         "ProductVersion normalized=", "FileVersion normalized=",
                         "normalized length=", "raw length=", "Product fixed=[", "File fixed=["):
                assert text in outcome["diagnostics"], (case["name"], text, outcome)
            if not case["accepted"]:
                assert "Artifact embedded version mismatch" in outcome["failure"]
                assert "raw length=" in outcome["failure"]
    by_name = {item["name"]: item for item in observed}
    assert "ProductVersion normalized='1.4.1' (normalized length=5, raw length=29)" in (
        by_name["ProductVersion-space"]["diagnostics"])
    assert "FileVersion normalized='1.4.1' (normalized length=5, raw length=7)" in (
        by_name["FileVersion-nul"]["diagnostics"])
    assert "ProductMajorPart=2" in by_name["ProductMajorPart-wrong"]["failure"]
    assert "expected fixed=[1,4,1,0]" in by_name["normal"]["diagnostics"]
