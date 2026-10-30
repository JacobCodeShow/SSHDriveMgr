"""Offline discovery cache/fallback tests; never download or install payloads."""
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock
import urllib.error
import urllib.request

import pytest

from src import update_security as security
from src.update_check import (ReleaseChecker, _ReleasePageRedirects,
                             checked_release_page, public_latest_release)
from tests.test_update_security import updater_namespace


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


def release(tag="v1.4.3"):
    return {"tag_name": tag, "body": "changes", "assets": [], "draft": False}


def test_five_minute_cache_returns_copies_and_revalidates_with_etag():
    clock = Clock()
    first = dict(release(), _response_etag='"one"')
    not_modified = urllib.error.HTTPError(security.LATEST_URL, 304, "Not Modified", {}, io.BytesIO())
    api = mock.Mock(side_effect=[first, not_modified, release("v1.4.4")])
    checker = ReleaseChecker(api_fetch=api, clock=clock)
    answer = checker.latest()
    answer["tag_name"] = "v9.9.9"
    answer["assets"].append({"name": "mutated"})
    assert checker.latest()["tag_name"] == "v1.4.3"
    assert not checker.latest()["assets"]
    assert api.call_count == 1
    clock.now += 300
    assert checker.latest()["tag_name"] == "v1.4.3"
    api.assert_called_with(etag='"one"')
    assert not_modified.closed
    clock.now += 300
    assert checker.latest()["tag_name"] == "v1.4.4"
    assert api.call_count == 3


def test_304_without_matching_cache_is_not_accepted():
    api = mock.Mock(side_effect=urllib.error.HTTPError("url", 304, "bad", {}, None))
    with pytest.raises(urllib.error.HTTPError):
        ReleaseChecker(api_fetch=api).latest()


def test_parallel_managers_share_one_inflight_fetch():
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def api():
        calls.append(True)
        entered.set()
        assert finish.wait(3)
        return release()

    checker = ReleaseChecker(api_fetch=api)
    answers, failures = [], []

    def check():
        try:
            answers.append(checker.latest())
        except Exception as error:
            failures.append(error)

    one, two = threading.Thread(target=check), threading.Thread(target=check)
    one.start()
    assert entered.wait(3)
    two.start()
    finish.set()
    one.join(3)
    two.join(3)
    assert not one.is_alive() and not two.is_alive() and not failures
    assert len(answers) == 2 and len(calls) == 1


def test_rate_limit_web_fallback_is_cached_manual_only_then_recovers():
    clock = Clock()
    error = security.UpdateRateLimitError(clock.now + 600)
    api = mock.Mock(side_effect=[error, release("v1.4.4")])
    # Even an accidental asset list from a web adapter cannot authorize a payload.
    web = mock.Mock(return_value=dict(release(), assets=[{"name": "SSHDriveMgr.exe"}],
                                     _response_etag='"untrusted"'))
    checker = ReleaseChecker(api_fetch=api, web_fetch=web, clock=clock)
    result = checker.latest()
    assert result["_discovery_only"] is True and result["assets"] == []
    assert "_response_etag" not in result
    assert checker.latest()["tag_name"] == "v1.4.3"
    assert api.call_count == web.call_count == 1
    clock.now += 600
    result = checker.latest()
    assert result["tag_name"] == "v1.4.4" and "_discovery_only" not in result


def test_failed_web_fallback_is_cooled_down_without_repeated_network():
    clock = Clock()
    limit = security.UpdateRateLimitError(clock.now + 600)
    api = mock.Mock(side_effect=limit)
    web = mock.Mock(side_effect=OSError("website unavailable"))
    checker = ReleaseChecker(api_fetch=api, web_fetch=web, clock=clock)
    for _ in range(3):
        with pytest.raises(security.UpdateRateLimitError) as failure:
            checker.latest()
        assert failure.value.retry_at == limit.retry_at
    assert api.call_count == web.call_count == 1
    clock.now += 60
    with pytest.raises(security.UpdateRateLimitError):
        checker.latest()
    assert api.call_count == web.call_count == 2


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("url", 403, "Forbidden", {}, io.BytesIO(b"permission denied")),
    urllib.error.HTTPError("url", 500, "Server error", {}, None),
    OSError("offline"), security.UpdateSecurityError("bad metadata"),
])
def test_non_rate_errors_never_use_web_fallback(error):
    web = mock.Mock()
    with pytest.raises(type(error)):
        ReleaseChecker(api_fetch=mock.Mock(side_effect=error), web_fetch=web).latest()
    web.assert_not_called()


@pytest.mark.parametrize("url", [
    "http://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3",
    "https://evil.example/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3",
    "https://github.com/attacker/repo/releases/tag/v1.4.3",
    "https://user:pass@github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3",
    "https://github.com:444/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3",
    "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3?token=x",
    "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3#fragment",
    "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3%2F..%2Fbad",
    "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3%252Fbad",
    "https://github.com/JacobCodeShow/SSHDriveMgr/releases/download/v1.4.3/evil.exe",
    "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/not-a-version",
])
def test_discovery_redirects_and_final_urls_are_confined(url):
    with pytest.raises(ValueError):
        checked_release_page(url)
    request = urllib.request.Request("https://github.com/JacobCodeShow/SSHDriveMgr/releases/latest")
    with pytest.raises(ValueError):
        _ReleasePageRedirects().redirect_request(request, None, 302, "Found", {}, url)


def test_public_latest_uses_official_redirect_and_never_reads_html_payload():
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.geturl.return_value = "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3"
    with mock.patch("urllib.request.build_opener") as opener:
        opener.return_value.open.return_value = response
        data = public_latest_release()
        assert opener.return_value.open.call_args.args[0].get_method() == "HEAD"
    assert data["tag_name"] == "v1.4.3" and data["_discovery_only"]
    assert data["assets"] == []
    response.read.assert_not_called()


def test_discovery_cache_never_satisfies_authoritative_apply_metadata():
    checker = ReleaseChecker(api_fetch=mock.Mock(return_value=release()))
    checker.latest()
    with mock.patch.object(security, "fetch_release", side_effect=OSError("must fetch fresh")) as fresh:
        with pytest.raises(OSError, match="must fetch fresh"):
            security.resolve_release("1.4.3", "SSHDriveMgr.exe", "portable_exe")
    fresh.assert_called_once_with(security.API_ROOT + "/tags/v1.4.3")


@pytest.mark.parametrize("kind", ["installer", "portable_exe", "onedir_zip", "source"])
def test_web_discovery_never_offers_automatic_payload_for_any_runtime(tmp_path, kind):
    namespace = updater_namespace(tmp_path)
    namespace["runtime_update_type"] = lambda: kind
    manager = namespace["UpdaterManager"]("1.4.2")
    data = dict(release(), _discovery_only=True)
    with mock.patch.object(security, "fetch_latest_release", return_value=data):
        manager.check_for_updates_async()
    args = manager.update_available.emit.call_args.args
    assert args[-1] == "rate_limited"
    assert args[-2] == "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3"
    assert manager._asset_name == "" and not manager._is_checking
    with mock.patch.object(security, "resolve_release") as apply_metadata:
        manager.download_update_async(args[-2])
    apply_metadata.assert_not_called()
    assert manager.download_finished.emit.call_args.args[0] is False
    assert not (tmp_path / "pending_update.json").exists()
    namespace["subprocess"].Popen.assert_not_called()


def test_current_version_can_be_confirmed_via_official_web_without_api_error(tmp_path):
    namespace = updater_namespace(tmp_path)
    manager = namespace["UpdaterManager"]("1.4.2")
    with mock.patch.object(security, "fetch_latest_release", return_value=dict(
            release("v1.4.2"), _discovery_only=True)):
        manager.check_for_updates_async()
    manager.no_update_available.emit.assert_called_once()
    manager.check_failed.emit.assert_not_called()


def test_same_manager_does_not_launch_duplicate_check_workers(tmp_path):
    namespace = updater_namespace(tmp_path)
    workers = []

    class HeldThread:
        def __init__(self, target, daemon):
            workers.append(target)

        def start(self):
            pass

    namespace["threading"] = SimpleNamespace(Thread=HeldThread)
    manager = namespace["UpdaterManager"]("1.4.2")
    manager.check_for_updates_async()
    manager.check_for_updates_async()
    assert len(workers) == 1 and manager._is_checking
    with mock.patch.object(security, "fetch_latest_release", side_effect=OSError("offline")):
        workers[0]()
    assert not manager._is_checking
    manager.check_failed.emit.assert_called_once_with("offline")


def test_rate_error_has_localized_retry_time_and_official_link(tmp_path):
    namespace = updater_namespace(tmp_path)
    manager = namespace["UpdaterManager"]("1.4.2")
    with mock.patch.object(security, "fetch_latest_release", side_effect=security.UpdateRateLimitError(2000000000)):
        manager.check_for_updates_async()
    message = manager.check_failed.emit.call_args.args[0]
    assert "GitHub" in message and "/JacobCodeShow/SSHDriveMgr/releases/latest" in message
    assert "HTTP Error 403" not in message
    assert not manager._is_checking


def test_rate_limited_dialog_only_opens_the_official_page():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from src.ui.dialogs.update_dialog import UpdateDialog
    app = QApplication.instance() or QApplication([])
    url = "https://github.com/JacobCodeShow/SSHDriveMgr/releases/tag/v1.4.3"
    dialog = UpdateDialog(version="1.4.3", download_url=url, obj_type="rate_limited",
                          already_downloaded=True)
    try:
        assert not dialog._downloaded
        assert dialog.install_btn.isHidden() and dialog._opt_row.isHidden()
        assert "API" in dialog.lead.text()
        dialog.on_download_finished(False, "Retry after the quota reset")
        assert dialog.lead.text() == "Retry after the quota reset"
        assert dialog.install_btn.isHidden()
        with mock.patch("webbrowser.open") as opened:
            dialog._open_browser()
        opened.assert_called_once_with(url)
    finally:
        dialog.hide()
        dialog.deleteLater()
        app.processEvents()


def test_settings_check_reset_does_not_touch_a_deleted_button():
    import ast
    from PyQt6 import sip
    from PyQt6.QtWidgets import QApplication, QPushButton
    app = QApplication.instance() or QApplication([])
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "src/ui/main_window.py").read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == "_sf_check_updates")
    reset = next(node for node in method.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_reset_btn")
    button = QPushButton()
    namespace = {"btn": button, "sip": sip, "tr": lambda key: key}
    exec(compile(ast.Module(body=[reset], type_ignores=[]), "check-reset", "exec"), namespace)
    sip.delete(button)
    namespace["_reset_btn"]()
    app.processEvents()


def test_all_locales_have_rate_limit_guidance():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "src/translations").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert {"settings.update.rate_limited", "update.rate_limited.lead", "update.rate_limited.details"} <= data.keys()
        assert "{retry_at}" in data["settings.update.rate_limited"]
        assert "{url}" in data["settings.update.rate_limited"]
