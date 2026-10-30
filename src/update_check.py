"""Cached update discovery; never an authority for downloading/applying payloads.

Only authoritative API metadata in update_security.resolve_release may authorize
an automatic update. The public website fallback supplies a version/link only.
"""
import copy
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from src import update_security as security

LATEST_WEB_URL = f"https://github.com/{security.REPOSITORY}/releases/latest"
_TAG_PREFIX = f"/{security.REPOSITORY}/releases/tag/"


def checked_release_page(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment):
        raise security.UpdateSecurityError("Untrusted release discovery URL")
    if parsed.path == f"/{security.REPOSITORY}/releases/latest":
        return None
    if not parsed.path.startswith(_TAG_PREFIX):
        raise security.UpdateSecurityError("Release discovery leaves the official repository")
    tag = urllib.parse.unquote(parsed.path[len(_TAG_PREFIX):])
    security.checked_version(tag.removeprefix("v"))
    return tag


class _ReleasePageRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_release_page(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_latest_release():
    """Use GitHub's official latest redirect, without scraping or trusting HTML."""
    request = urllib.request.Request(LATEST_WEB_URL, method="HEAD", headers={
        "User-Agent": "SSHDriveMgr-Updater", "Accept": "text/html",
    })
    with urllib.request.build_opener(_ReleasePageRedirects()).open(request, timeout=15) as response:
        tag = checked_release_page(response.geturl())
    if tag is None:
        raise security.UpdateSecurityError("Official latest page did not identify a release")
    return {"tag_name": tag, "body": "", "assets": [], "draft": False,
            "_discovery_only": True}


class ReleaseChecker:
    """Serialize discovery and reuse results for five minutes across UI managers."""
    def __init__(self, *, api_fetch=None, web_fetch=None, clock=None, cache_seconds=300):
        self._api_fetch = api_fetch or security.fetch_release
        self._web_fetch = web_fetch or public_latest_release
        self._clock = clock or time.time
        self._ttl = cache_seconds
        self._lock = threading.Lock()
        self._api_data = None
        self._api_until = 0
        self._etag = None
        self._web_data = None
        self._web_until = 0
        self._web_failure = None
        self._web_failure_until = 0

    @staticmethod
    def _checked(data):
        if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
            raise security.UpdateSecurityError("Invalid latest release metadata")
        tag = data.get("tag_name")
        if not isinstance(tag, str):
            raise security.UpdateSecurityError("Latest release has no version")
        security.checked_version(tag.removeprefix("v"))
        return copy.deepcopy(data)

    def latest(self):
        with self._lock:
            now = self._clock()
            if self._api_data is not None and now < self._api_until:
                return copy.deepcopy(self._api_data)
            if self._web_data is not None and now < self._web_until:
                return copy.deepcopy(self._web_data)
            if self._web_failure is not None and now < self._web_failure_until:
                # Store scalar retry state, not an exception traceback retaining UI workers.
                raise security.UpdateRateLimitError(*self._web_failure)
            try:
                if self._etag and self._api_data is not None:
                    data = self._api_fetch(etag=self._etag)
                else:
                    data = self._api_fetch()
            except urllib.error.HTTPError as error:
                if error.code != 304 or self._api_data is None or not self._etag:
                    raise
                error.close()
                self._api_until = self._clock() + self._ttl
                return copy.deepcopy(self._api_data)
            except security.UpdateRateLimitError as error:
                # The API transport respects its Retry-After/reset deadline.
                # A public redirect may discover a newer version, but it cannot
                # supply trusted asset identity/digests or arm an installation.
                try:
                    data = self._checked(self._web_fetch())
                except Exception as web_error:
                    self._web_failure = (error.retry_at, error.code)
                    now = self._clock()
                    self._web_failure_until = min(now + 60, max(now + 1, error.retry_at))
                    raise error from web_error
                self._web_failure = None
                self._web_failure_until = 0
                data["assets"] = []
                data["_discovery_only"] = True
                data["_rate_limit_retry_at"] = error.retry_at
                data.pop("_response_etag", None)
                self._web_data = data
                now = self._clock()
                self._web_until = min(now + self._ttl, max(now + 1, error.retry_at))
                return copy.deepcopy(data)
            data = self._checked(data)
            data.pop("_discovery_only", None)
            data.pop("_rate_limit_retry_at", None)
            self._api_data = data
            self._etag = data.get("_response_etag")
            self._api_until = self._clock() + self._ttl
            self._web_data = None
            self._web_until = 0
            self._web_failure = None
            self._web_failure_until = 0
            return copy.deepcopy(data)


_checker = ReleaseChecker()


def fetch_latest_release():
    return _checker.latest()
