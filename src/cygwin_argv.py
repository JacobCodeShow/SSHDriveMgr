"""Encode native Windows launches for the bundled Cygwin command-line parser.

This is NOT a cmd.exe, PowerShell, POSIX-shell, or Windows-CRT serializer.
Cygwin's native-parent ``build_argv``/``globify`` grammar requires outer
quotes even when an argument containing literal quotes has no whitespace.

Call Popen(cygwin_command_line(argv), executable=argv[0], shell=False, ...).
The caller must supply a trusted executable and a controlled child environment:
this encoding targets Cygwin's default glob mode, not CYGWIN=noglob.
No filesystem access, response-file expansion, or subprocess launch occurs here.
"""

from collections.abc import Sequence
import ntpath
import re


def _dos_path(argument: str) -> bool:
    """Match Cygwin 3.1.7 globify's special DOS/UNC path classification."""
    return bool(re.match(r"^[A-Za-z]:", argument)) or (
        len(argument) > 3
        and argument.startswith("\\\\")
        and argument[2].isascii()
        and argument[2].isalpha()
        and "\\" in argument[3:]
    )


def cygwin_command_line(argv: Sequence[str]) -> str:
    """Return a raw Windows command line preserving the supplied Cygwin argv.

    ``argv[0]`` must be a trusted, fully qualified Windows executable path.
    Pass the result as a STRING with ``executable=argv[0]`` and ``shell=False``;
    passing it through subprocess.list2cmdline again reintroduces the bug.

    Bare DOS/UNC arguments containing backslashes or literal double quotes are
    deliberately rejected: Cygwin applies a different globify grammar to these.
    Normalize bare paths to forward slashes first. Backslashes and literal quotes
    inside ordinary option arguments (for example -oIdentityFile=...) are handled.
    The SSHFS drive argument ``F:`` is supported unchanged.
    """
    if isinstance(argv, (str, bytes, bytearray)) or not isinstance(argv, Sequence):
        raise TypeError("argv must be a sequence of strings")
    if not argv:
        raise ValueError("argv must contain an executable")
    for argument in argv:
        if not isinstance(argument, str):
            raise TypeError("every argv item must be a string")
        if any(character in argument for character in ("\x00", "\r", "\n")):
            raise ValueError("argv items must not contain NUL, CR, or LF")

    executable = argv[0]
    drive, tail = ntpath.splitdrive(executable)
    if (
        not executable
        or not drive
        or not ntpath.isabs(executable)
        or not tail
        or executable.endswith(("/", "\\"))
        or '"' in executable
    ):
        raise ValueError("argv[0] must be a trusted absolute Windows executable path")

    # Cygwin treats argv[0] specially: remove its enclosing quotes without
    # globification or backslash unescaping. Do not double its path separators.
    encoded = ['"' + executable + '"']
    for argument in argv[1:]:
        if _dos_path(argument) and ("\\" in argument or '"' in argument):
            raise ValueError("bare DOS/UNC arguments must use forward slashes without literal quotes")
        # Always add OUTER quotes. Without them, the CRT encoding of an inner
        # quote can cause Cygwin to absorb every following argument into this one.
        escaped = argument.replace("\\", "\\\\").replace('"', '\\"')
        encoded.append('"' + escaped + '"')
    return " ".join(encoded)


__all__ = ["cygwin_command_line"]
