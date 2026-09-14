"""
drive_utils.py – Utilities for Windows drive letter management.
"""

import subprocess
import string
import re
from typing import List


ALL_LETTERS = [f"{c}:" for c in string.ascii_uppercase]
# Reserve system / common letters
RESERVED = {"A:", "B:", "C:"}

# Sentinel value meaning "pick the smallest available drive letter at mount time"
AUTO_DRIVE = "AUTO"


def is_auto_drive(letter):
    """True if the stored drive-letter setting means 'auto-select at mount time'."""
    return not letter or letter.strip().upper() == AUTO_DRIVE


def resolve_drive_letter(letter, exclude=None):
    """Resolve a drive-letter setting to a concrete drive letter.

    AUTO (or empty) -> the smallest free drive letter; otherwise the value is
    normalised to uppercase with a trailing colon. Returns None when AUTO is
    requested but no drive letter is free.
    """
    if is_auto_drive(letter):
        available = get_available_drives(exclude=exclude)
        return available[0] if available else None
    letter = letter.strip().upper().rstrip("\\")
    if not letter.endswith(":"):
        letter += ":"
    return letter


def get_used_drives() -> List[str]:
    """Return list of drive letters currently in use via GetLogicalDrives bitmask."""
    import ctypes
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    used = []
    for i in range(26):
        if bitmask & (1 << i):
            used.append(f"{chr(65 + i)}:")
    return used


def get_available_drives(exclude: List[str] = None) -> List[str]:
    """Return drive letters that are free and can be used for mounting."""
    used = set(get_used_drives())
    if exclude:
        used.update(exclude)
    return [l for l in ALL_LETTERS if l not in RESERVED and l not in used]


def is_drive_in_use(letter: str) -> bool:
    """Check if a specific drive letter is currently in use."""
    return letter.upper().rstrip("\\") + ":" in get_used_drives() or \
           letter.upper() in get_used_drives()
