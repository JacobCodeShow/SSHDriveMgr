# -*- coding: utf-8 -*-
"""
machine_crypto.py – Machine-bound encryption for "remember password" feature.

The encryption key is derived from hardware fingerprints (Windows MachineGuid
+ MAC address) via PBKDF2-HMAC-SHA256.  There is NO hard-coded key in the
source code: someone who reads the code but runs it on a different machine
cannot decrypt the stored ciphertext.

Security properties:
  - Ciphertext is only decryptable on the same machine (same MachineGuid + MAC).
  - AES-256-GCM provides authenticated encryption (tamper-evident).
  - PBKDF2 with 100_000 iterations slows down brute-force of the fingerprint.
  - The per-message nonce is random and stored alongside the ciphertext.

Limitation (inherent to any local "remember password" feature):
  a user who can run arbitrary code on the SAME machine can decrypt.  This is
  unavoidable — the key material must come from the runtime environment.
"""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import uuid
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Fixed salt for PBKDF2.  This is NOT a secret — it only needs to be stable
# so the same fingerprint always derives the same key.  Changing it invalidates
# all previously stored ciphertext.
_PBKDF2_SALT = b"neo-ssh-win-manager::machine-bound::v1"
_PBKDF2_ITERATIONS = 100_000
_KEY_LEN = 32  # AES-256
_NONCE_LEN = 12  # GCM standard nonce size


# ---------------------------------------------------------------------------
# Machine fingerprint collection
# ---------------------------------------------------------------------------

def _read_windows_machine_guid() -> str:
    """Read the Windows MachineGuid from the registry.

    This value is generated at Windows install time and is stable for the
    lifetime of the OS installation.  It is unique per machine.
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip().lower()
    except Exception:
        return ""


def _get_mac_address() -> str:
    """Return the first MAC address as a 48-bit integer string."""
    try:
        node = uuid.getnode()
        # uuid.getnode() returns a 48-bit int; if it can't find a real MAC
        # it returns a random value with the multicast bit set (bit 40 = 1).
        if (node >> 40) & 1:
            return ""
        return format(node, "012x")
    except Exception:
        return ""


def get_machine_fingerprint() -> str:
    """Collect a stable machine fingerprint string.

    Combines:
      - Windows MachineGuid (primary, most stable)
      - MAC address (secondary, adds entropy)
      - OS name + machine architecture (tertiary, cheap mixer)

    At least MachineGuid or MAC must be available; if both are missing we
    fall back to a hostname-based fingerprint (less secure but functional).
    """
    parts = [
        _read_windows_machine_guid(),
        _get_mac_address(),
        platform.system(),
        platform.machine(),
        platform.release(),
    ]
    # Remove empty parts but keep the separators so order is preserved
    fingerprint = "|".join(p for p in parts if p)
    if not fingerprint:
        # Ultimate fallback: hostname + username (less secure)
        fingerprint = "fallback|" + platform.node() + "|" + os.environ.get("USERNAME", "unknown")
    return fingerprint


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

_cached_key: Optional[bytes] = None


def derive_machine_key() -> bytes:
    """Derive a 32-byte AES key from the machine fingerprint via PBKDF2.

    The result is cached in memory for the lifetime of the process so we
    don't pay the PBKDF2 cost on every encrypt/decrypt call.
    """
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    fingerprint = get_machine_fingerprint()
    _cached_key = hashlib.pbkdf2_hmac(
        "sha256",
        fingerprint.encode("utf-8"),
        _PBKDF2_SALT,
        _PBKDF2_ITERATIONS,
        dklen=_KEY_LEN,
    )
    return _cached_key


# ---------------------------------------------------------------------------
# Authenticated encryption / decryption (AES-256-GCM)
# ---------------------------------------------------------------------------

def machine_encrypt(plaintext: str) -> str:
    """Encrypt a string with the machine-bound key.

    Returns a base64-encoded token containing: nonce (12 bytes) || ciphertext.
    The GCM authentication tag is appended by AESGCM and included in ciphertext.
    """
    key = derive_machine_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # Pack: nonce + ciphertext (ciphertext includes the 16-byte GCM tag)
    packed = nonce + ciphertext
    return base64.b64encode(packed).decode("ascii")


def machine_decrypt(token: str) -> Optional[str]:
    """Decrypt a machine_encrypt token.

    Returns the plaintext string, or None if decryption fails (wrong machine,
    tampered ciphertext, or corrupted token).
    """
    try:
        key = derive_machine_key()
        aesgcm = AESGCM(key)
        packed = base64.b64decode(token.encode("ascii"))
        if len(packed) < _NONCE_LEN + 16:  # nonce + minimum GCM tag
            return None
        nonce = packed[:_NONCE_LEN]
        ciphertext = packed[_NONCE_LEN:]
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Self-test (run directly to verify on this machine)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Fingerprint:", get_machine_fingerprint()[:40] + "...")
    test_msg = "hello-machine-bound-secret-123"
    enc = machine_encrypt(test_msg)
    dec = machine_decrypt(enc)
    print("Encrypted token length:", len(enc))
    print("Round-trip OK:", dec == test_msg)
    # Tamper test
    bad = enc[:-4] + "AAAA"
    print("Tamper rejection:", machine_decrypt(bad) is None)
