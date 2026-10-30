"""Offline updater security regressions. No Qt, registry, AppData or installers.

All filesystem fixtures live in a uniquely named directory inside this checkout.
No NamedTemporaryFile / mode-0600 sandbox-sensitive temporary files are used.
"""

import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request
import uuid

from src import update_security as security


WORKSPACE = Path(__file__).resolve().parents[1]
VERSION = "1.5.8"
NAME = f"SSHDriveMgr-Setup-{VERSION}.exe"
PAYLOAD = b"offline official release fixture"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.headers = {"Content-Length": str(len(value))}


def release_fixture(digest="sha256:" + DIGEST):
    asset = {"id": 123, "name": NAME, "size": len(PAYLOAD),
             "browser_download_url": security.release_asset_url("v" + VERSION, NAME)}
    if digest is not None:
        asset["digest"] = digest
    return {"tag_name": "v" + VERSION, "draft": False, "assets": [asset]}


def checksum_asset():
    return {"id": 124, "name": "sha256sums.txt", "size": 200,
            "browser_download_url": security.release_asset_url("v" + VERSION, "sha256sums.txt")}


def official_fixture():
    with mock.patch.object(security, "fetch_release", return_value=release_fixture()):
        return security.resolve_release(VERSION, NAME, "installer")


def updater_namespace(root, installed=True):
    """Compile updater without Qt/logger; default fixtures model a Setup installation.

    Pass installed=None when testing real read-only installation detection.
    """
    source = (WORKSPACE / "src" / "updater.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    tree.body = [node for node in tree.body
                 if not isinstance(node, (ast.Import, ast.ImportFrom))]

    class Signal:
        def __init__(self, *args):
            self.emit = mock.Mock()

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    process = SimpleNamespace(STARTUPINFO=lambda: SimpleNamespace(dwFlags=0),
                              STARTF_USESHOWWINDOW=1, DETACHED_PROCESS=8,
                              CREATE_NEW_PROCESS_GROUP=512, Popen=mock.Mock())
    namespace = {"security": security, "Path": Path, "json": json, "os": os,
                 "sys": SimpleNamespace(frozen=True, executable=str(root / "app.exe")),
                 "logger": mock.Mock(), "subprocess": process,
                 "threading": SimpleNamespace(Thread=ImmediateThread),
                 "QObject": object, "pyqtSignal": Signal,
                 "version": SimpleNamespace(parse=lambda value: tuple(map(int, value.split(".")))),
                 "time": SimpleNamespace(time=lambda: 123456, strftime=time.strftime,
                                         localtime=time.localtime)}
    exec(compile(tree, "updater-security-test", "exec"), namespace)
    namespace["updates_dir"] = lambda: root
    if installed is not None:
        namespace["is_installed_copy"] = lambda: installed
    return namespace


class UpdateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = WORKSPACE / (".update-security-test-" + uuid.uuid4().hex)
        self.temp.mkdir(mode=0o777)
        self.root = self.temp / "staging"
        self.root.mkdir(mode=0o777)
        self.official = official_fixture()

    def tearDown(self):
        resolved = self.temp.resolve()
        if resolved.parent != WORKSPACE or not resolved.name.startswith(".update-security-test-"):
            raise AssertionError("Refusing cleanup outside the test workspace")
        shutil.rmtree(resolved)

    def payload_record(self):
        path = security.staging_path(self.root, VERSION, NAME, "installer")
        path.write_bytes(PAYLOAD)
        return dict(self.official, schema=1, installer_path=str(path),
                    install_on_next_start=True, changelog="")

    def test_atomic_text_and_json_roundtrip(self):
        target = self.root / "roundtrip.txt"
        security.atomic_text(target, "plain text\nnot JSON")
        self.assertEqual(target.read_text(encoding="utf-8"), "plain text\nnot JSON")
        security.atomic_json(target, {"sha256": DIGEST, "armed": True})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"sha256": DIGEST, "armed": True})
        self.assertFalse(list(self.root.glob("*.part")))

    def test_symlink_and_redirected_staging_rejected_without_platform_privileges(self):
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(security.UpdateSecurityError):
                security.staging_path(self.root, VERSION, NAME, "installer")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == self.root:
                return self.temp / "redirected-outside-staging"
            return original_resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            with self.assertRaises(security.UpdateSecurityError):
                security.staging_path(self.root, VERSION, NAME, "installer")

    def test_unknown_elevation_fails_closed(self):
        admin = mock.Mock(side_effect=OSError("token lookup failed"))
        ctypes = SimpleNamespace(windll=SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=admin)))
        with mock.patch.object(security, "os", SimpleNamespace(name="nt")), mock.patch.dict(
                "sys.modules", {"ctypes": ctypes}):
            self.assertTrue(security.is_elevated())
        admin.assert_called_once_with()

    def test_elevated_startup_preserves_armed_update_and_exposes_reason(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        updater["os"] = SimpleNamespace(name="nt")
        updater["get_pending_update"] = mock.Mock(return_value=record)
        updater["set_install_on_next_start"] = mock.Mock()
        with mock.patch.object(security, "is_elevated", return_value=True):
            self.assertFalse(updater["maybe_install_pending_update"]("1.0.0"))
        updater["set_install_on_next_start"].assert_not_called()
        self.assertTrue(record["install_on_next_start"])
        self.assertIn("non-elevated", updater["get_update_handover_error"]())
        updater["subprocess"].Popen.assert_not_called()

    def test_marker_save_failure_reports_download_failure(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        manager = updater["UpdaterManager"]("1.0.0")
        manager.latest_version, manager._asset_name = VERSION, NAME
        with mock.patch.object(security, "resolve_release", return_value=self.official), mock.patch.object(
                security, "download_payload", return_value=Path(record["installer_path"])), mock.patch.object(
                security, "atomic_json", side_effect=OSError("marker write failed")):
            manager.download_update_async(self.official["download_url"])
        manager.download_finished.emit.assert_called_once_with(False, "marker write failed")
        self.assertFalse(manager._is_downloading)

    def test_api_digest_is_required_valid_and_normalized(self):
        with mock.patch.object(security, "fetch_release", return_value=release_fixture("sha256:" + DIGEST.upper())):
            actual = security.resolve_release(VERSION, NAME, "installer")
        self.assertEqual(actual["sha256"], DIGEST)
        self.assertEqual(actual["integrity_source"], "github-api-digest")

    def test_missing_digest_and_checksum_fail_closed(self):
        with mock.patch.object(security, "fetch_release", return_value=release_fixture(None)):
            with self.assertRaises(security.UpdateSecurityError):
                security.resolve_release(VERSION, NAME, "installer")

    def test_malformed_digest_never_falls_back(self):
        for digest in ("", "md5:" + DIGEST, "sha256:xyz", 123):
            with self.subTest(digest=digest), mock.patch.object(security, "fetch_release", return_value=release_fixture(digest)):
                with self.assertRaises(security.UpdateSecurityError):
                    security.resolve_release(VERSION, NAME, "installer")

    def test_official_checksum_fallback(self):
        release = release_fixture(None)
        release["assets"].append(checksum_asset())
        with mock.patch.object(security, "fetch_release", return_value=release), mock.patch.object(
                security, "open_https", return_value=Response((DIGEST + " *" + NAME + "\n").encode())) as opened:
            result = security.resolve_release(VERSION, NAME, "installer")
        self.assertEqual(result["sha256"], DIGEST)
        self.assertEqual(result["integrity_source"], "github-release-checksum")
        opened.assert_called_once_with(checksum_asset()["browser_download_url"])

    def test_checksum_missing_entry_duplicate_malformed_or_network_failure_rejected(self):
        release = release_fixture(None)
        release["assets"].append(checksum_asset())
        for text in (DIGEST + " other.exe", "bad " + NAME,
                     (DIGEST + " " + NAME + "\n") * 2):
            with self.subTest(text=text), mock.patch.object(security, "fetch_release", return_value=release), mock.patch.object(
                    security, "open_https", return_value=Response(text.encode())):
                with self.assertRaises(security.UpdateSecurityError):
                    security.resolve_release(VERSION, NAME, "installer")
        with mock.patch.object(security, "fetch_release", return_value=release), mock.patch.object(
                security, "open_https", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                security.resolve_release(VERSION, NAME, "installer")

    def test_asset_and_checksum_urls_must_be_official_exact_release(self):
        for which in ("payload", "checksum"):
            for url in ("http://github.com/JacobCodeShow/SSHDriveMgr/releases/download/v1/a.exe",
                        "https://evil.example/a.exe", "https://github.com/attacker/repo/releases/download/v1/a.exe"):
                release = release_fixture(None if which == "checksum" else "sha256:" + DIGEST)
                if which == "checksum":
                    release["assets"].append(checksum_asset())
                release["assets"][-1]["browser_download_url"] = url
                with self.subTest(which=which, url=url), mock.patch.object(security, "fetch_release", return_value=release):
                    with self.assertRaises(security.UpdateSecurityError):
                        security.resolve_release(VERSION, NAME, "installer")

    def test_wrong_version_duplicate_asset_and_wrong_name_rejected(self):
        release = release_fixture()
        release["tag_name"] = "v0.0.1"
        with mock.patch.object(security, "fetch_release", return_value=release):
            with self.assertRaises(security.UpdateSecurityError):
                security.resolve_release(VERSION, NAME, "installer")
        release = release_fixture()
        release["assets"].append(copy.deepcopy(release["assets"][0]))
        with mock.patch.object(security, "fetch_release", return_value=release):
            with self.assertRaises(security.UpdateSecurityError):
                security.resolve_release(VERSION, NAME, "installer")
        for name, kind in (("cmd.exe", "installer"), ("../SSHDriveMgr.exe", "portable_exe"),
                           (NAME, "script"), ("SSHDriveMgr-Setup-0.0.1.exe", "installer")):
            with self.subTest(name=name, kind=kind), self.assertRaises(security.UpdateSecurityError):
                security.resolve_release(VERSION, name, kind)

    def test_exact_tag_lookup_fallback_only_on_404(self):
        not_found = urllib.error.HTTPError("url", 404, "missing", {}, None)
        with mock.patch.object(security, "fetch_release", side_effect=[not_found, release_fixture()]) as fetched:
            security.resolve_release(VERSION, NAME, "installer")
        self.assertEqual(fetched.call_args_list[0].args[0], security.API_ROOT + "/tags/v" + VERSION)
        self.assertEqual(fetched.call_args_list[1].args[0], security.API_ROOT + "/tags/" + VERSION)
        with mock.patch.object(security, "fetch_release", side_effect=OSError("offline")) as fetched:
            with self.assertRaises(OSError):
                security.resolve_release(VERSION, NAME, "installer")
        self.assertEqual(fetched.call_count, 1)

    def test_https_redirect_rejects_downgrade_foreign_host_and_foreign_repository(self):
        handler = security._HTTPSRedirects()
        req = urllib.request.Request(self.official["download_url"])
        for url in ("http://release-assets.githubusercontent.com/payload", "https://evil.example/payload",
                    "https://github.com/attacker/repo/releases/download/v1/payload",
                    "https://api.github.com/repos/attacker/repo/releases/latest",
                    "https://github.com:444/JacobCodeShow/SSHDriveMgr/releases/download/v1/x",
                    "https://user:pass@github.com/JacobCodeShow/SSHDriveMgr/releases/download/v1/x"):
            with self.subTest(url=url), self.assertRaises(security.UpdateSecurityError):
                handler.redirect_request(req, None, 302, "Found", {}, url)
        redirected = handler.redirect_request(req, None, 302, "Found", {},
                                             "https://release-assets.githubusercontent.com/asset?token=abc")
        self.assertEqual(redirected.full_url, "https://release-assets.githubusercontent.com/asset?token=abc")

    def test_urlopen_is_never_used_for_arbitrary_or_http_url(self):
        with mock.patch("urllib.request.build_opener") as opener:
            for url in ("http://github.com/a", "https://evil.example", "file:///C:/bad.exe"):
                with self.assertRaises(security.UpdateSecurityError):
                    security.open_https(url)
            opener.assert_not_called()

    def test_marker_arbitrary_relative_wrong_type_and_legacy_paths_rejected(self):
        good = self.payload_record()
        for changes in ({"installer_path": str(self.temp / "outside.exe")},
                        {"installer_path": NAME}, {"installer_path": None},
                        {"asset_type": "script"}, {"asset_type": []},
                        {"asset_name": "cmd.exe"}, {"schema": 0}, {"sha256": ""},
                        {"install_on_next_start": "true"}):
            with self.subTest(changes=changes), self.assertRaises(security.UpdateSecurityError):
                security.validate_record(self.root, dict(good, **changes))

    def test_payload_symlink_escape_is_rejected(self):
        outside = self.temp / "outside.exe"
        outside.write_bytes(PAYLOAD)
        path = security.staging_path(self.root, VERSION, NAME, "installer")
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks requires platform privileges")
        with self.assertRaises(security.UpdateSecurityError):
            security.staging_path(self.root, VERSION, NAME, "installer", str(path))

    def test_apply_refreshes_metadata_and_rehashes_exact_payload(self):
        record = self.payload_record()
        with mock.patch.object(security, "resolve_release", return_value=self.official) as resolve:
            path, official = security.verify_for_apply(self.root, record)
        resolve.assert_called_once_with(VERSION, NAME, "installer")
        self.assertEqual(path, Path(record["installer_path"]))
        self.assertEqual(official, self.official)
        path.write_bytes(b"tampered")
        with mock.patch.object(security, "resolve_release", return_value=self.official):
            with self.assertRaises(security.UpdateSecurityError):
                security.verify_for_apply(self.root, record)

    def test_marker_digest_or_asset_identity_cannot_authorize_payload(self):
        record = self.payload_record()
        for changes in ({"sha256": "0" * 64}, {"asset_id": 987}, {"release_tag": "v0.0.1"},
                        {"download_url": "https://evil.example/a.exe"}, {"size": 1}):
            with self.subTest(changes=changes), mock.patch.object(security, "resolve_release", return_value=self.official):
                with self.assertRaises(security.UpdateSecurityError):
                    security.verify_for_apply(self.root, dict(record, **changes))

    def test_apply_offline_fails_closed(self):
        record = self.payload_record()
        with mock.patch.object(security, "resolve_release", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                security.verify_for_apply(self.root, record)

    def test_download_writes_only_part_then_atomically_replaces(self):
        destination = security.staging_path(self.root, VERSION, NAME, "installer")
        destination.write_bytes(b"old")
        real_replace = os.replace
        replacements = []

        def replace(source, target):
            self.assertTrue(str(source).endswith(".part"))
            self.assertEqual(Path(source).read_bytes(), PAYLOAD)
            self.assertEqual(destination.read_bytes(), b"old")
            replacements.append((source, target))
            real_replace(source, target)

        with mock.patch.object(security, "open_https", return_value=Response(PAYLOAD)), mock.patch.object(
                security.os, "replace", side_effect=replace):
            path = security.download_payload(self.root, self.official)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertEqual(len(replacements), 1)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_bad_or_interrupted_download_preserves_destination_and_removes_part(self):
        destination = security.staging_path(self.root, VERSION, NAME, "installer")
        destination.write_bytes(b"old")
        for response in (Response(b"bad"), Response(PAYLOAD + b"too large")):
            with mock.patch.object(security, "open_https", return_value=response):
                with self.assertRaises(security.UpdateSecurityError):
                    security.download_payload(self.root, self.official)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(list(self.root.glob("*.part")))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = [PAYLOAD[:4], OSError("interrupted")]
        with mock.patch.object(security, "open_https", return_value=response):
            with self.assertRaises(OSError):
                security.download_payload(self.root, self.official)
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_marker_saved_atomically_with_integrity_metadata(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        updater["save_pending_update"](VERSION, record["installer_path"], NAME, integrity=self.official)
        marker = self.root / "pending_update.json"
        saved = json.loads(marker.read_text(encoding="utf-8"))
        for key in ("sha256", "release_tag", "asset_id", "integrity_source", "download_url", "size"):
            self.assertEqual(saved[key], self.official[key])
        self.assertEqual(saved["schema"], 1)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_marker_atomic_write_failure_keeps_old_marker(self):
        marker = self.root / "pending_update.json"
        marker.write_text("old", encoding="utf-8")
        with mock.patch.object(security.os, "replace", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                security.atomic_json(marker, {"new": True})
        self.assertEqual(marker.read_text(encoding="utf-8"), "old")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_clear_pending_update_never_deletes_arbitrary_marker_path(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        outside = self.temp / "must-survive.exe"
        outside.write_bytes(b"not an update")
        record["installer_path"] = str(outside)
        marker = self.root / "pending_update.json"
        marker.write_text(json.dumps(record), encoding="utf-8")
        updater["clear_pending_update"]()
        self.assertEqual(outside.read_bytes(), b"not an update")
        self.assertFalse(marker.exists())

    def test_elevated_handover_never_reads_or_launches_writable_helper(self):
        updater = updater_namespace(self.root)
        with mock.patch.object(security, "is_elevated", return_value=True), mock.patch.object(
                security, "verify_for_apply") as verify:
            self.assertFalse(updater["launch_pending_installer"]({"installer_path": "C:/evil.exe"}))
        verify.assert_not_called()
        updater["subprocess"].Popen.assert_not_called()
        self.assertFalse((self.root / "run_update.cmd").exists())

    def test_non_elevated_bad_metadata_never_launches(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
                security, "resolve_release", side_effect=OSError("offline")):
            self.assertFalse(updater["launch_pending_installer"](record))
        updater["subprocess"].Popen.assert_not_called()

    def test_payload_tampered_during_helper_creation_is_rehashed_before_launch(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        original_atomic_text = security.atomic_text

        def write_then_tamper(path, text):
            original_atomic_text(path, text)
            Path(record["installer_path"]).write_bytes(b"tampered after initial validation")

        with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
                security, "resolve_release", return_value=self.official), mock.patch.object(
                security, "atomic_text", side_effect=write_then_tamper):
            self.assertFalse(updater["launch_pending_installer"](record))
        updater["subprocess"].Popen.assert_not_called()
        self.assertIn("SHA256", updater["get_update_handover_error"]())

    def test_non_elevated_helper_has_apply_time_hash_guard_before_execution(self):
        updater = updater_namespace(self.root)
        record = self.payload_record()
        with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
                security, "resolve_release", return_value=self.official):
            self.assertTrue(updater["launch_pending_installer"](record))
        script = (self.root / "run_update.cmd").read_text(encoding="utf-8")
        self.assertIn(DIGEST, script)
        self.assertLess(script.index("Get-FileHash"), script.index('start "" /wait'))
        self.assertIn("if errorlevel 1 goto restart", script)
        self.assertIn("-ErrorAction Stop", script)
        updater["subprocess"].Popen.assert_called_once()

    def test_download_error_is_reported_instead_of_success(self):
        updater = updater_namespace(self.root)
        manager = updater["UpdaterManager"]("1.0.0")
        manager.latest_version, manager._asset_name = VERSION, NAME
        with mock.patch.object(security, "resolve_release", side_effect=OSError("digest unavailable")):
            manager.download_update_async(self.official["download_url"])
        manager.download_finished.emit.assert_called_once_with(False, "digest unavailable")
        self.assertFalse(manager._is_downloading)
        self.assertFalse((self.root / "pending_update.json").exists())

    def test_download_rejects_caller_supplied_noncanonical_url(self):
        updater = updater_namespace(self.root)
        manager = updater["UpdaterManager"]("1.0.0")
        manager.latest_version, manager._asset_name = VERSION, NAME
        with mock.patch.object(security, "resolve_release", return_value=self.official), mock.patch.object(
                security, "download_payload") as download:
            manager.download_update_async("https://evil.example/installer.exe")
        download.assert_not_called()
        self.assertFalse(manager.download_finished.emit.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
