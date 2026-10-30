"""Qt-free updater policy. GitHub/TLS is the trust root, not a publisher signature.

AppData is untrusted. Records only identify an official release to re-resolve;
they never authorize a path, executable type, URL or checksum by themselves.
"""

import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REPOSITORY = "JacobCodeShow/SSHDriveMgr"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}/releases"
LATEST_URL = API_ROOT + "/latest"
CHECKSUM_NAMES = ("sha256sums.txt", "checksums.txt", "SHA256SUMS")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class UpdateSecurityError(ValueError):
    pass


class UpdateRateLimitError(UpdateSecurityError):
    """GitHub API throttling, including the shared in-process retry deadline."""

    def __init__(self, retry_at, code=403):
        self.retry_at = float(retry_at)
        self.code = code
        super().__init__(
            f"GitHub API rate limit exceeded (HTTP {code}). "
            "Please wait before checking for updates again; "
            "official release downloads are not affected.")


_api_rate_limit_lock = threading.Lock()
_api_retry_at = 0.0
_api_retry_code = 403
_RATE_LIMIT_BODY_BYTES = 64 * 1024
_RELEASE_BYTES = 4 * 1024 * 1024
_DEFAULT_RETRY_SECONDS = 60.0
_MAX_RETRY_SECONDS = 24 * 60 * 60.0
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"\Z')


def _response_header(headers, name):
    if headers is None:
        return None
    value = headers.get(name)
    if value is None:
        value = next((value for key, value in headers.items()
                      if key.lower() == name.lower()), None)
    return value if isinstance(value, str) else None


def _safe_header_value(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and bool(value.strip())
            and all(32 <= ord(char) <= 126 for char in value))


def _retry_after_seconds(value, now):
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _rate_limit_deadline(headers, now):
    seconds = _retry_after_seconds(_response_header(headers, "Retry-After"), now)
    if seconds is None:
        try:
            reset = float(_response_header(headers, "X-RateLimit-Reset"))
            seconds = reset - now if math.isfinite(reset) and reset > now else None
        except (TypeError, ValueError, OverflowError):
            seconds = None
    if seconds is None:
        seconds = _DEFAULT_RETRY_SECONDS
    return now + min(seconds, _MAX_RETRY_SECONDS)


def _is_rate_limit(error, now):
    if error.code == 429:
        return True
    if error.code != 403:
        return False
    remaining = _response_header(error.headers, "X-RateLimit-Remaining")
    if remaining is not None and remaining.strip() == "0":
        return True
    if _retry_after_seconds(_response_header(error.headers, "Retry-After"), now) is not None:
        return True
    # GitHub's secondary limit may have remaining quota and no Retry-After.
    # Never download an unbounded error page or interpret every 403 as throttling.
    try:
        body = error.read(_RATE_LIMIT_BODY_BYTES).decode("utf-8", errors="replace").lower()
    except (OSError, ValueError):
        return False
    return any(message in body for message in (
        "api rate limit", "rate limit exceeded", "secondary rate limit",
        "exceeded a rate limit", "abuse detection"))


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


def open_https(url, timeout=15, *, headers=None):
    global _api_retry_at, _api_retry_code
    if not url.startswith((API_ROOT + "/", f"https://github.com/{REPOSITORY}/releases/download/")):
        raise UpdateSecurityError("Untrusted update URL")
    request_headers = {"User-Agent": "SSHDriveMgr-Updater"}
    if headers is not None:
        if not isinstance(headers, dict):
            raise UpdateSecurityError("Invalid update request headers")
        for name, value in headers.items():
            # Conditional public metadata only; never credentials or UA overrides.
            if (not isinstance(name, str) or name.lower() != "if-none-match"
                    or not _safe_header_value(value)):
                raise UpdateSecurityError("Invalid update conditional request header")
            request_headers["If-None-Match"] = value
    req = urllib.request.Request(url, headers=request_headers)
    if not url.startswith(API_ROOT + "/"):
        return urllib.request.build_opener(_HTTPSRedirects()).open(req, timeout=timeout)
    # Serialize API request admission and response-header handling so a newly
    # observed limit cannot race another request past an active deadline.
    with _api_rate_limit_lock:
        now = time.time()
        if now < _api_retry_at:
            raise UpdateRateLimitError(_api_retry_at, _api_retry_code)
        try:
            return urllib.request.build_opener(_HTTPSRedirects()).open(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            now = time.time()
            if not _is_rate_limit(error, now):
                raise
            _api_retry_at = _rate_limit_deadline(error.headers, now)
            _api_retry_code = error.code
            error.close()
            raise UpdateRateLimitError(_api_retry_at, _api_retry_code) from error


def fetch_release(url=LATEST_URL, *, etag=None):
    # Keep the no-header call compatible with security callers and simple mocks.
    response = (open_https(url) if etag is None else
                open_https(url, headers={"If-None-Match": etag}))
    with response as stream:
        raw = stream.read(_RELEASE_BYTES + 1)
        if len(raw) > _RELEASE_BYTES:
            raise UpdateSecurityError("GitHub release metadata is too large")
        data = json.loads(raw.decode("utf-8"))
        response_etag = _response_header(getattr(stream, "headers", None), "ETag")
    if not isinstance(data, dict) or data.get("draft"):
        raise UpdateSecurityError("Invalid or draft GitHub release")
    # This private transport metadata is never trusted when supplied in JSON.
    data.pop("_response_etag", None)
    if _safe_header_value(response_etag) and _ETAG.fullmatch(response_etag):
        data["_response_etag"] = response_etag
    return data


def fetch_latest_release():
    """Discovery-only checker; apply always uses authoritative fetch_release."""
    from src.update_check import fetch_latest_release as check_latest_release
    return check_latest_release()


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
