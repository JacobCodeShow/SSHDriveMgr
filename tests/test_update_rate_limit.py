"""Purely mocked GitHub transport regressions; no API, disk cache or credentials."""

import io
import json
import sys
import threading
from email.utils import formatdate
from types import SimpleNamespace
from unittest import mock
import urllib.error

import pytest

from src import update_security as security


NOW = 1_700_000_000.0


class Response(io.BytesIO):
    def __init__(self, value=b'{"draft": false}', headers=None):
        super().__init__(value)
        self.headers = headers or {}


def http_error(code=403, headers=None, body=b'Forbidden'):
    error = urllib.error.HTTPError(security.LATEST_URL, code, "fixture", headers or {},
                                   io.BytesIO(body))
    error.read = mock.Mock(wraps=error.read)
    return error


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    # Restore every shared attribute after each test, including any pre-test
    # deadline, so test order cannot poison other updater/security suites.
    monkeypatch.setattr(security, "_api_rate_limit_lock", threading.Lock())
    monkeypatch.setattr(security, "_api_retry_at", 0.0)
    monkeypatch.setattr(security, "_api_retry_code", 403)
    clock = mock.Mock(return_value=NOW)
    monkeypatch.setattr(security, "time", SimpleNamespace(time=clock))
    opener = mock.Mock()
    opener.open.return_value = Response()
    build = mock.Mock(return_value=opener)
    monkeypatch.setattr(security.urllib.request, "build_opener", build)
    return SimpleNamespace(clock=clock, opener=opener, build=build)


def test_primary_limit_sets_structured_deadline(transport):
    transport.opener.open.side_effect = http_error(
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 3600)})
    with pytest.raises(security.UpdateRateLimitError) as caught:
        security.open_https(security.LATEST_URL)
    error = caught.value
    assert isinstance(error, security.UpdateSecurityError)
    assert error.retry_at == NOW + 3600
    assert type(error.retry_at) is float
    assert error.code == 403
    assert "GitHub" in str(error) and "rate limit" in str(error) and "wait" in str(error)


@pytest.mark.parametrize("body", [
    b'{"message":"API rate limit exceeded for fixture"}',
    b'{"message":"You have exceeded a secondary rate limit."}',
    b'{"message":"You have triggered an abuse detection mechanism."}',
])
def test_primary_and_secondary_body_limits_have_default_minute(transport, body):
    error = http_error(headers={"X-RateLimit-Remaining": "37"}, body=body)
    transport.opener.open.side_effect = error
    with pytest.raises(security.UpdateRateLimitError) as caught:
        security.open_https(security.LATEST_URL)
    assert caught.value.retry_at == NOW + 60
    error.read.assert_called_once_with(64 * 1024)


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("retry_after,expected", [
    ("120", 120),
    ("10", 10),
    (formatdate(NOW + 180, usegmt=True), 180),
])
def test_retry_after_seconds_and_http_date(transport, status, retry_after, expected):
    transport.opener.open.side_effect = http_error(
        status, {"retry-after": retry_after, "X-RateLimit-Reset": str(NOW + 9999)})
    with pytest.raises(security.UpdateRateLimitError) as caught:
        security.open_https(security.LATEST_URL)
    assert caught.value.retry_at == NOW + expected
    assert caught.value.code == status


def test_429_without_other_rate_limit_evidence_uses_default(transport):
    transport.opener.open.side_effect = http_error(429)
    with pytest.raises(security.UpdateRateLimitError) as caught:
        security.open_https(security.LATEST_URL)
    assert caught.value.retry_at == NOW + 60
    assert caught.value.code == 429


@pytest.mark.parametrize("headers", [
    {},
    {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": str(NOW + 400)},
    {"Retry-After": "invalid", "X-RateLimit-Reset": str(NOW + 400)},
])
def test_generic_403_is_not_a_rate_limit(transport, headers):
    original = http_error(headers=headers, body=b'{"message":"Resource not accessible"}')
    transport.opener.open.side_effect = original
    with pytest.raises(urllib.error.HTTPError) as caught:
        security.open_https(security.LATEST_URL)
    assert caught.value is original
    assert security._api_retry_at == 0


def test_error_body_inspection_is_bounded(transport):
    error = http_error(body=b"x" * (64 * 1024) + b"API rate limit exceeded")
    transport.opener.open.side_effect = error
    with pytest.raises(urllib.error.HTTPError) as caught:
        security.open_https(security.LATEST_URL)
    assert caught.value is error
    error.read.assert_called_once_with(64 * 1024)
    assert security._api_retry_at == 0


@pytest.mark.parametrize("headers,expected", [
    ({"Retry-After": "nan"}, 60),
    ({"Retry-After": "inf"}, 60),
    ({"Retry-After": "-10"}, 60),
    ({"Retry-After": "0"}, 60),
    ({"Retry-After": "invalid", "X-RateLimit-Reset": str(NOW + 240)}, 240),
    ({"X-RateLimit-Reset": "nan"}, 60),
    ({"X-RateLimit-Reset": "inf"}, 60),
    ({"X-RateLimit-Reset": str(NOW - 1)}, 60),
    ({"Retry-After": "999999999999999999999999999999"}, 86400),
    ({"X-RateLimit-Reset": str(NOW + 1_000_000_000)}, 86400),
])
def test_untrusted_deadlines_are_finite_and_at_most_one_day(transport, headers, expected):
    transport.opener.open.side_effect = http_error(429, headers)
    with pytest.raises(security.UpdateRateLimitError) as caught:
        security.open_https(security.LATEST_URL)
    assert caught.value.retry_at == NOW + expected


def test_shared_backoff_prevents_requests_and_recovers_at_deadline(transport):
    response = Response()
    transport.opener.open.side_effect = [http_error(429, {"Retry-After": "90"}), response]
    with pytest.raises(security.UpdateRateLimitError):
        security.open_https(security.LATEST_URL)
    transport.clock.return_value = NOW + 89
    with pytest.raises(security.UpdateRateLimitError) as caught:
        security.open_https(security.API_ROOT + "/tags/v1.4.2")
    assert caught.value.retry_at == NOW + 90 and caught.value.code == 429
    assert transport.opener.open.call_count == 1
    assert transport.build.call_count == 1
    transport.clock.return_value = NOW + 90
    assert security.open_https(security.LATEST_URL) is response
    assert transport.opener.open.call_count == 2


def test_api_network_admission_and_limit_observation_hold_shared_lock(transport):
    def limited_request(*args, **kwargs):
        assert security._api_rate_limit_lock.locked()
        raise http_error(429)

    transport.opener.open.side_effect = limited_request
    with pytest.raises(security.UpdateRateLimitError):
        security.open_https(security.LATEST_URL)
    assert not security._api_rate_limit_lock.locked()


def test_asset_requests_ignore_api_backoff_and_do_not_create_it(transport):
    asset_url = security.release_asset_url("v1.4.2", "SSHDriveMgr.exe")
    security._api_retry_at = NOW + 100
    response = Response(b"asset fixture")
    transport.opener.open.return_value = response
    assert security.open_https(asset_url) is response
    assert security._api_retry_at == NOW + 100
    security._api_retry_at = 0
    original = http_error(429, {"Retry-After": "500"})
    transport.opener.open.side_effect = original
    with pytest.raises(urllib.error.HTTPError) as caught:
        security.open_https(asset_url)
    assert caught.value is original
    assert security._api_retry_at == 0


@pytest.mark.parametrize("headers", [
    {"If-None-Match": "a\r\nb: injected"},
    {"If-None-Match": "a\nInjected: value"},
    {"If-None-Match": "\x00"},
    {"If-None-Match": "\t"},
    {"If-None-Match": "\x7f"},
    {"If-None-Match": "\u2603"},
    {"If-None-Match": ""},
    {"If-None-Match": " "},
    {"If-None-Match": "x" * 1025},
    {"If-None-Match": 123},
    {"If-None-Match": None},
    {"If-None-Match\r\nAuthorization": "injected"},
    {"Authorization": "Bearer never-use-tokens"},
    {"User-Agent": "override"},
    {1: "invalid"},
    [("If-None-Match", '"abc"')],
])
def test_unsafe_or_unapproved_headers_are_rejected_before_http(transport, headers):
    with pytest.raises(security.UpdateSecurityError):
        security.open_https(security.LATEST_URL, headers=headers)
    transport.build.assert_not_called()


def test_conditional_headers_keep_fixed_user_agent_and_timeout(transport):
    security.open_https(security.LATEST_URL, timeout=7, headers={"if-none-match": 'W/"abc"'})
    args, kwargs = transport.opener.open.call_args
    assert args[0].full_url == security.LATEST_URL
    assert args[0].get_header("If-none-match") == 'W/"abc"'
    assert args[0].get_header("User-agent") == "SSHDriveMgr-Updater"
    assert kwargs == {"timeout": 7}
    assert isinstance(transport.build.call_args.args[0], security._HTTPSRedirects)


def test_fetch_release_without_etag_keeps_one_argument_mock_compatibility():
    def open_one_argument(url):
        assert url == security.LATEST_URL
        return Response()

    with mock.patch.object(security, "open_https", side_effect=open_one_argument) as opened:
        assert security.fetch_release() == {"draft": False}
    opened.assert_called_once_with(security.LATEST_URL)


@pytest.mark.parametrize("etag", ['"abc"', 'W/"abc"'])
def test_fetch_release_sends_conditional_etag_and_attaches_response_etag(etag):
    response = Response(headers={"etag": etag})
    with mock.patch.object(security, "open_https", return_value=response) as opened:
        result = security.fetch_release(etag=etag)
    opened.assert_called_once_with(security.LATEST_URL, headers={"If-None-Match": etag})
    assert result == {"draft": False, "_response_etag": etag}


@pytest.mark.parametrize("etag", [None, "", "unquoted", '"abc"\r\nInjected: value',
                                  '"a\x00b"', '"' + "x" * 1025 + '"'])
def test_missing_or_invalid_response_etag_never_adds_private_key(etag):
    # Release JSON cannot forge the transport-only metadata either.
    response = Response(b'{"draft":false,"_response_etag":"forged"}', {"ETag": etag})
    with mock.patch.object(security, "open_https", return_value=response):
        assert security.fetch_release() == {"draft": False}


def test_304_propagates_unchanged_without_setting_backoff(transport):
    original = http_error(304, {"ETag": '"abc"'})
    transport.opener.open.side_effect = original
    with pytest.raises(urllib.error.HTTPError) as caught:
        security.fetch_release(etag='"abc"')
    assert caught.value is original
    assert security._api_retry_at == 0
    original.read.assert_not_called()


def test_release_metadata_exact_limit_accepted_and_oversize_explicitly_rejected():
    minimal = b'{"draft":false}'
    maximum = minimal + b" " * (4 * 1024 * 1024 - len(minimal))
    with mock.patch.object(security, "open_https", return_value=Response(maximum)):
        assert security.fetch_release() == {"draft": False}
    response = Response(maximum + b" ")
    response.read = mock.Mock(wraps=response.read)
    with mock.patch.object(security, "open_https", return_value=response):
        with pytest.raises(security.UpdateSecurityError, match="too large"):
            security.fetch_release()
    response.read.assert_called_once_with(4 * 1024 * 1024 + 1)


@pytest.mark.parametrize("release", [[], None, {"draft": True}])
def test_invalid_or_draft_release_still_rejected(release):
    with mock.patch.object(security, "open_https", return_value=Response(json.dumps(release).encode())):
        with pytest.raises(security.UpdateSecurityError, match="Invalid or draft"):
            security.fetch_release()


def test_fetch_release_uses_context_managers_entered_response():
    context = mock.MagicMock()
    context.__enter__.return_value = Response(headers={"ETag": '"abc"'})
    with mock.patch.object(security, "open_https", return_value=context):
        assert security.fetch_release() == {"draft": False, "_response_etag": '"abc"'}
    context.__exit__.assert_called_once_with(None, None, None)


def test_authoritative_fetch_has_no_cache_or_fallback(transport):
    transport.opener.open.side_effect = [Response(b'{"tag_name":"v1.4.2"}'),
                                       Response(b'{"tag_name":"v1.4.3"}')]
    assert security.fetch_release()["tag_name"] == "v1.4.2"
    assert security.fetch_release()["tag_name"] == "v1.4.3"
    assert transport.opener.open.call_count == 2


def test_fetch_latest_release_lazily_delegates_to_discovery_checker(monkeypatch):
    checker = mock.Mock(return_value={"discovery": "fixture"})
    monkeypatch.setitem(sys.modules, "src.update_check", SimpleNamespace(fetch_latest_release=checker))
    assert security.fetch_latest_release() == {"discovery": "fixture"}
    checker.assert_called_once_with()
