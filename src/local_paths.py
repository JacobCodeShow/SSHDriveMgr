"""Constrain untrusted remote names before creating/opening local files.

This policy deliberately uses Windows filename rules even when tests run on
another OS. A remote basename must never become a drive, UNC path, ADS, device,
or traversal component in the Windows desktop client.
"""
from pathlib import Path
import ntpath
import re

_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$|CLOCK\$)$", re.IGNORECASE)
_FORBIDDEN = frozenset('<>:"/\\|?*')


def validate_remote_filename(name: str) -> str:
    """Return the unchanged safe basename; reject rather than silently rename."""
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("Remote file has an invalid filename")
    if name[-1] in (" ", ".") or any(ord(char) < 32 or char in _FORBIDDEN for char in name):
        raise ValueError("Remote filename contains unsafe Windows path characters")
    if ntpath.splitdrive(name)[0] or ntpath.basename(name) != name:
        raise ValueError("Remote filename must be a basename, not a local path")
    if _RESERVED.fullmatch(name.split(".", 1)[0].rstrip(" .")):
        raise ValueError("Remote filename is a reserved Windows device name")
    return name


def confined_download_path(directory: str, name: str) -> str:
    """Resolve one remote basename inside an existing private download directory.

    Resolve before the download/open sink as a second check, including existing
    symlinks. The caller must own the temporary directory and not expose it to
    other principals while the download is in progress.
    """
    validate_remote_filename(name)
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Download destination is not a directory")
    destination = (root / name).resolve()
    if destination.parent != root:
        raise ValueError("Download destination escapes its temporary directory")
    return str(destination)
