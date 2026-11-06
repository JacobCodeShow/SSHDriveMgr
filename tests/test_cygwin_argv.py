"""Portable grammar regressions plus optional, strictly offline binary probes.

The installed-binary tests never mount, authenticate, read real SSH settings or
keys, or connect. SSHFS receives no host and always --version. SSH receives -G,
a fresh empty -F config, and CanonicalizeHostname=no; all key paths are synthetic.
"""

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from src.cygwin_argv import cygwin_command_line


EXE = r"C:\Program Files\SSHFS-Win\bin\sshfs.exe"


def _cygwin_parse(command):
    """Filesystem-free model of Cygwin 3.1.7 build_argv + quoted + globify.

    Source: winsup/cygwin/dcrt0.cc, cygwin-3_1_7-release. Model native-parent
    default glob mode, returning literal words (without performing filesystem
    globbing). The serializer quotes all metacharacters, so none may expand.
    """
    words = []
    offset = 0
    while offset < len(command):
        if command[offset].isspace():
            offset += 1
            continue
        start = offset
        while offset < len(command):
            character = command[offset]
            if character not in "\"'":
                offset += 1
            else:
                quote = character
                offset += 1
                while offset < len(command):
                    if words and quote == '"' and command[offset] == "\\":
                        offset += 2
                    elif command[offset] == quote:
                        if words and quote == '"' and command[offset:offset + 2] == '""':
                            offset += 2
                        else:
                            offset += 1
                            break
                    else:
                        offset += 1
            if offset < len(command) and command[offset].isspace():
                break
        word = command[start:offset]
        if not words:
            # Native argv[0] has no globify step or escaped embedded quotes.
            words.append(word[1:-1] if word.startswith(('"', "'")) else word)
        else:
            inner = word[1:] if word.startswith(('"', "'")) else word
            dos = bool(re.match(r"^[A-Za-z]:", inner)) or (
                len(inner) > 3 and inner.startswith("\\\\")
                and inner[2].isascii() and inner[2].isalpha() and "\\" in inner[3:]
            )
            result = []
            index = 0
            while index < len(word):
                character = word[index]
                if character not in "\"'":
                    if not dos and character == "\\" and index + 1 < len(word):
                        index += 1
                    result.append(word[index])
                    index += 1
                    continue
                quote = character
                index += 1
                while index < len(word) and word[index] != quote:
                    if (not dos and word[index] == "\\" and index + 1 < len(word)
                            and word[index + 1] in (quote, "\\")):
                        index += 1
                    result.append(word[index])
                    index += 1
                index += 1
            words.append("".join(result))
    return words


@pytest.mark.parametrize("arguments", [
    [], [""], ["F:", "-p22", "-odefault_permissions"],
    ["jacob@offline.invalid:/bmc/private", "-ovolname=Private folder"],
    ['-oUserKnownHostsFile="C:/offline/known_hosts"', "--version"],
    ['-oUserKnownHostsFile="C:/offline/User Name/known_hosts"', "-ouid=-1"],
    ['-oIdentityFile="C:/offline/O\'Name/key file"', "--version"],
    ["C:/offline/User Name/config", "-oFileSecurity=D:P(A;OICI;FA;;;S-1-5-21-1)"],
    ["-oPath=back\\slash\\\\and\\", 'literal "quotes"', "O'Name", "\t"],
    ["*?[abc]{x,y}()", "@never-read-response-file", "unicode-目录-ä"],

])
def test_portable_cygwin_round_trip(arguments):
    argv = [EXE, *arguments]
    assert _cygwin_parse(cygwin_command_line(argv)) == argv


def test_crt_encoding_regression_absorbs_version_argument():
    argv = [EXE, '-oUserKnownHostsFile="C:/offline/known_hosts"', "--version"]
    broken = _cygwin_parse(subprocess.list2cmdline(argv))
    assert len(broken) == 2
    assert "--version" in broken[1]
    assert _cygwin_parse(cygwin_command_line(argv)) == argv


@pytest.mark.parametrize("argv,exception", [
    ([], ValueError), ("sshfs.exe", TypeError), ([EXE, None], TypeError),
    ([EXE, 1], TypeError), ([EXE, b"bytes"], TypeError),
    (["sshfs.exe"], ValueError), ([r"C:sshfs.exe"], ValueError),
    ([r"\sshfs.exe"], ValueError), (["C:\\"], ValueError),
    (['C:/bad"name/sshfs.exe'], ValueError),
    ([EXE, "nul\x00argument"], ValueError), ([EXE, "cr\rargument"], ValueError),
    ([EXE, "lf\nargument"], ValueError), (["C:/bad\nname.exe"], ValueError),
    ([EXE, r"C:\bare\path"], ValueError),
    ([EXE, r"\\server\share\path"], ValueError),
    ([EXE, 'C:/bare/"quoted"'], ValueError),

])
def test_invalid_or_unsupported_inputs_fail_closed(argv, exception):
    with pytest.raises(exception):
        cygwin_command_line(argv)


def _installed(name):
    if sys.platform != "win32":
        pytest.skip("installed SSHFS-Win probes require Windows")
    for base in (r"C:\Program Files\SSHFS-Win\bin", r"C:\Program Files (x86)\SSHFS-Win\bin"):
        candidate = Path(base) / name
        if candidate.is_file():
            return str(candidate)
    pytest.skip(f"installed SSHFS-Win {name} is unavailable")


def _offline_run(executable, arguments, *, crt=False):
    argv = [executable, *arguments]
    command = argv if crt else cygwin_command_line(argv)
    environment = {"PATH": str(Path(executable).parent), "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
    return subprocess.run(
        command, executable=executable, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, timeout=10,
    )


def test_installed_sshfs_exposes_crt_quote_swallowing_without_host():
    executable = _installed("sshfs.exe")
    arguments = ['-oUserKnownHostsFile="C:/offline/known_hosts"', "--version"]
    broken = _offline_run(executable, arguments, crt=True)
    assert broken.returncode == 1
    assert b"missing host" in broken.stderr
    fixed = _offline_run(executable, arguments)
    assert fixed.returncode == 0
    assert b"SSHFS version" in fixed.stdout


@pytest.mark.parametrize("path", [
    "C:/offline/known_hosts", "C:/offline/User Name/known_hosts",
    "C:/offline/User O'Name/known_hosts", "C:/offline/trailing/",
    "C:\\offline\\User Name\\known_hosts",
])
def test_installed_sshfs_preserves_tail_without_host_or_mount(path):
    executable = _installed("sshfs.exe")
    option = '-oUserKnownHostsFile="' + path + '"'
    version = _offline_run(executable, [option, "--version"])
    assert version.returncode == 0, version.stderr
    assert b"SSHFS version" in version.stdout
    sentinels = _offline_run(executable, [option, "--version", "__ARGV_FIRST__", "__ARGV_SECOND__"])
    assert sentinels.returncode == 1
    assert sentinels.stderr == b"sshfs: invalid argument `__ARGV_SECOND__'\n"


@pytest.mark.parametrize("sentinel", [
    "__PROBE plain__", '__PROBE "inner quote"__', "__PROBE O'Name__",
    "__PROBE__\\\\end\\", "__PROBE__\\'quoted", "__PROBE__*?[a]{x,y}()", "",
])
def test_installed_sshfs_echoes_exact_encoded_nonoption(sentinel):
    executable = _installed("sshfs.exe")
    result = _offline_run(executable, ["--version", "__ARGV_FIRST__", sentinel])
    assert result.returncode == 1
    assert result.stderr == ("sshfs: invalid argument `" + sentinel + "'\n").encode()


def test_installed_ssh_G_preserves_synthetic_quoted_paths(tmp_path):
    executable = _installed("ssh.exe")
    # Only this newly created empty file is read; no real user/system SSH config.
    config = tmp_path / "empty offline config"
    config.write_text("", encoding="utf-8")
    identity = "C:/__offline__/User O'Name/id no real key"
    known_hosts = "C:/__offline__/User O'Name/known hosts"
    result = _offline_run(executable, [
        "-G", "-F", str(config).replace("\\", "/"),
        "-oCanonicalizeHostname=no", "-oProxyCommand=none", "-oProxyJump=none",
        "-oIdentityAgent=none", "-oGlobalKnownHostsFile=none",
        '-oIdentityFile="' + identity + '"',
        '-oUserKnownHostsFile="' + known_hosts + '"',
        "offline.invalid",
    ])
    assert result.returncode == 0, result.stderr
    settings = result.stdout.decode("utf-8", "replace").splitlines()
    assert "canonicalizehostname false" in settings or "canonicalizehostname no" in settings
    assert "identityfile " + identity in settings
    assert "userknownhostsfile " + known_hosts in settings
