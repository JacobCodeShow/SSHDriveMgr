"""Qt-free updater policy. GitHub/TLS is the trust root, not a publisher signature.

AppData is untrusted. Records only identify an official release to re-resolve;
they never authorize a path, executable type, URL or checksum by themselves.
"""

import hashlib
import hmac
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPOSITORY = "JacobCodeShow/SSHDriveMgr"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}/releases"
LATEST_URL = API_ROOT + "/latest"
CHECKSUM_NAMES = ("sha256sums.txt", "checksums.txt", "SHA256SUMS")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class UpdateSecurityError(ValueError):
    pass


def checked_version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdateSecurityError("Invalid update version")
    return value


def checked_asset_name(version, name, asset_type):
    checked_version(version)
    expected = {"installer": f"SSHDriveMgr-Setup-{version}.exe",
                "portable_exe": "SSHDriveMgr.exe"}.get(asset_type)
    if expected is None or name != expected:
        raise UpdateSecurityError("Unknown update type or unexpected asset filename/version")
    return name


def release_asset_url(tag, name):
    return (f"https://github.com/{REPOSITORY}/releases/download/"
            f"{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(name, safe='')}")


class _HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        # GitHub serves release binaries via this CDN. Never follow HTTP,
        # credentials, ports, or arbitrary third-party redirects.
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or parsed.hostname not in {"github.com", "api.github.com",
                                           "release-assets.githubusercontent.com",
                                           "objects.githubusercontent.com"}):
            raise UpdateSecurityError("Untrusted update redirect")
        if (parsed.hostname == "github.com" and not parsed.path.startswith(f"/{REPOSITORY}/releases/download/")):
            raise UpdateSecurityError("Redirect leaves the official release repository")
        if parsed.hostname == "api.github.com" and not parsed.path.startswith(f"/repos/{REPOSITORY}/releases/"):
            raise UpdateSecurityError("Redirect leaves the official release API")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_https(url, timeout=15):
    if not url.startswith((API_ROOT + "/", f"https://github.com/{REPOSITORY}/releases/download/")):
        raise UpdateSecurityError("Untrusted update URL")
    req = urllib.request.Request(url, headers={"User-Agent": "SSHDriveMgr-Updater"})
    return urllib.request.build_opener(_HTTPSRedirects()).open(req, timeout=timeout)


def fetch_release(url=LATEST_URL):
    with open_https(url) as response:
        data = json.loads(response.read(4 * 1024 * 1024 + 1).decode("utf-8"))
    if not isinstance(data, dict) or data.get("draft"):
        raise UpdateSecurityError("Invalid or draft GitHub release")
    return data


def _asset_identity(asset, tag):
    if (not isinstance(asset, dict) or not isinstance(asset.get("id"), int)
            or isinstance(asset.get("id"), bool) or asset["id"] <= 0
            or type(asset.get("size")) is not int or asset["size"] <= 0
            or asset.get("browser_download_url") != release_asset_url(tag, asset.get("name", ""))):
        raise UpdateSecurityError("Invalid canonical GitHub asset metadata")
    return asset


def resolve_release(version, name, asset_type):
    """Resolve exact version + asset from the fixed repository, including SHA256."""
    checked_asset_name(version, name, asset_type)
    # Accept both repository tag conventions, but never a tag supplied by a marker.
    import urllib.error
    try:
        release = fetch_release(API_ROOT + "/tags/v" + urllib.parse.quote(version, safe=""))
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        release = fetch_release(API_ROOT + "/tags/" + urllib.parse.quote(version, safe=""))
    tag = release.get("tag_name")
    if tag not in (version, "v" + version):
        raise UpdateSecurityError("GitHub release version does not match request")
    matches = [a for a in release.get("assets", []) if a.get("name") == name]
    if len(matches) != 1:
        raise UpdateSecurityError("Release asset missing or ambiguous")
    asset = _asset_identity(matches[0], tag)
    digest = asset.get("digest")
    if digest is not None:
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not _SHA256.fullmatch(digest[7:]):
            raise UpdateSecurityError("Invalid GitHub SHA256 asset digest")
        expected = digest[7:].lower()
        source = "github-api-digest"
    else:
        checksums = [a for a in release.get("assets", []) if a.get("name") in CHECKSUM_NAMES]
        if len(checksums) != 1:
            raise UpdateSecurityError("Release requires a SHA256 digest or one official checksum asset")
        checksum = _asset_identity(checksums[0], tag)
        with open_https(checksum["browser_download_url"]) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise UpdateSecurityError("Checksum file too large")
        entries = []
        for line in raw.decode("utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                entries.append(parts[0])
        if len(entries) != 1 or not _SHA256.fullmatch(entries[0]):
            raise UpdateSecurityError("Missing, ambiguous or malformed SHA256 entry")
        expected = entries[0].lower()
        source = "github-release-checksum"
    return {"version": version, "asset_name": name, "asset_type": asset_type,
            "release_tag": tag, "asset_id": asset["id"], "size": asset["size"],
            "download_url": asset["browser_download_url"], "sha256": expected,
            "integrity_source": source}


def staging_path(root, version, name, asset_type, supplied=None):
    checked_asset_name(version, name, asset_type)
    root = Path(root).absolute()
    canonical_root = root.resolve()
    # Reject redirected staging roots too (including ancestor junctions). A
    # marker must never turn cleanup/download into access outside AppData staging.
    if canonical_root != root:
        raise UpdateSecurityError("Update staging directory is redirected")
    expected = root / f"{version}-{name}"
    if expected.is_symlink() or expected.resolve().parent != canonical_root:
        raise UpdateSecurityError("Update payload escapes canonical staging directory")
    if supplied is not None:
        candidate = Path(supplied)
        if (not candidate.is_absolute() or candidate.absolute() != expected
                or candidate.resolve() != expected.resolve() or candidate.is_symlink()):
            raise UpdateSecurityError("Marker has an unauthorized update payload path")
    return expected


def validate_record(root, record, require_file=True):
    if not isinstance(record, dict) or type(record.get("schema")) is not int or record.get("schema") != 1:
        raise UpdateSecurityError("Unverified/legacy update marker; download the update again")
    if not isinstance(record.get("installer_path"), str) or not record["installer_path"]:
        raise UpdateSecurityError("Marker lacks a canonical payload path")
    if not isinstance(record.get("asset_type"), str) or not isinstance(record.get("asset_name"), str):
        raise UpdateSecurityError("Invalid update asset type/name")
    path = staging_path(root, record.get("version"), record.get("asset_name"),
                        record.get("asset_type"), record["installer_path"])
    if not isinstance(record.get("sha256"), str) or not _SHA256.fullmatch(record["sha256"]):
        raise UpdateSecurityError("Marker lacks integrity metadata")
    if type(record.get("install_on_next_start")) is not bool:
        raise UpdateSecurityError("Invalid update arm flag")
    if require_file and not path.is_file():
        raise UpdateSecurityError("Update payload is missing")
    return path


def verify_payload(path, metadata):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            size += len(chunk)
            digest.update(chunk)
    if size != metadata["size"] or not hmac.compare_digest(digest.hexdigest(), metadata["sha256"]):
        raise UpdateSecurityError("Update SHA256/size mismatch; download it again")


def verify_for_apply(root, record):
    path = validate_record(root, record)
    official = resolve_release(record["version"], record["asset_name"], record["asset_type"])
    for key in ("release_tag", "asset_id", "size", "download_url", "sha256", "integrity_source"):
        if record.get(key) != official[key]:
            raise UpdateSecurityError("Pending update does not match current official release metadata")
    # Re-check containment after the network round trip, then hash the exact file.
    path = validate_record(root, record)
    verify_payload(path, official)
    return path, official


def atomic_json(path, record):
    atomic_text(path, json.dumps(record, indent=2))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        with open(part, "x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()


def is_elevated():
    """Unknown privilege state fails closed; no writable helper runs elevated."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True


def download_payload(root, metadata, progress=None):
    path = staging_path(root, metadata["version"], metadata["asset_name"], metadata["asset_type"])
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        downloaded = 0
        with open_https(metadata["download_url"]) as response, open(part, "xb") as stream:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > metadata["size"]:
                    raise UpdateSecurityError("Update exceeds official asset size")
                stream.write(chunk)
                if progress:
                    progress(min(100, downloaded * 100 // metadata["size"]))
            stream.flush()
            os.fsync(stream.fileno())
        verify_payload(part, metadata)
        staging_path(root, metadata["version"], metadata["asset_name"], metadata["asset_type"])
        os.replace(part, path)
        return path
    finally:
        if part.exists():
            part.unlink()
