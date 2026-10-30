"""Offline isolation regressions; real ACLs touch only a fresh private dummy root."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import pytest

from tests import conftest as isolation


class FakeACL:
    user_sid = "current-user"
    token_owner_sid = "token-owner"

    def __init__(self):
        self.owners = {}
        self.grants = []

    def owner(self, path):
        return self.owners.get(str(path), self.user_sid)

    def grant_delete(self, path, allowed_owners, validate):
        validate(path)
        assert self.owner(path) in allowed_owners
        self.grants.append(Path(path))


@pytest.fixture
def private_state(tmp_path):
    state = isolation.SessionIsolation.create(temp_base=tmp_path, acl=FakeACL())
    yield state
    state.restore()
    assert state.cleanup()


def test_import_has_no_environment_or_tempfile_side_effects():
    before = dict(os.environ), tempfile.tempdir
    name = "_isolation_import_probe"
    spec = importlib.util.spec_from_file_location(name, isolation.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    assert (dict(os.environ), tempfile.tempdir) == before


def test_configured_before_collection(test_isolation):
    root = test_isolation.root
    repository = Path(__file__).resolve().parents[1]
    assert root.parent == isolation.canonical_os_temp()
    assert not root.is_relative_to(repository)
    assert os.environ["APPDATA"] == str(root / "appdata")
    assert tempfile.tempdir == str((root / "tmp").resolve())
    assert tempfile.gettempdir() == tempfile.tempdir


@pytest.mark.parametrize("original", [None, "original-profile-never-delete"])
@pytest.mark.parametrize("cached", [None, "original-temp-cache"])
def test_restore_exact_environment_and_tempdir(tmp_path, monkeypatch, original, cached):
    if original is None:
        monkeypatch.delenv("APPDATA", raising=False)
    else:
        monkeypatch.setenv("APPDATA", original)
    monkeypatch.setattr(tempfile, "tempdir", cached)
    state = isolation.SessionIsolation.create(temp_base=tmp_path, acl=FakeACL())
    try:
        state.restore()
        assert os.environ.get("APPDATA") == original
        assert tempfile.tempdir == cached
        state.restore()  # Idempotent.
    finally:
        assert state.cleanup()


def test_original_profile_is_not_cleaned(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    sentinel = original / "dummy.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(original))
    state = isolation.SessionIsolation.create(temp_base=tmp_path, acl=FakeACL())
    state.restore()
    assert state.cleanup()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rejects_build_temp_before_creation(tmp_path):
    repository = tmp_path / "repo"
    build = repository / "build"
    build.mkdir(parents=True)
    before = dict(os.environ), tempfile.tempdir
    with pytest.raises(ValueError, match="outside the repository"):
        isolation.SessionIsolation.create(temp_base=build, repository=repository, acl=FakeACL())
    assert list(build.iterdir()) == []
    assert (dict(os.environ), tempfile.tempdir) == before


def test_cleanup_retries_only_failing_owned_path(private_state):
    state = private_state
    path = state.root / "tmp" / "dummy"
    path.write_text("dummy", encoding="utf-8")
    retried = []

    def fake_rmtree(root, *, onexc):
        assert root == state.root
        onexc(lambda target: retried.append(Path(target)), path, PermissionError("protected"))

    with mock.patch.object(isolation.shutil, "rmtree", side_effect=fake_rmtree):
        assert state.cleanup()
    assert state.acl.grants == [path]
    assert retried == [path]


def test_refuses_outside_and_foreign_owner(private_state, tmp_path):
    state = private_state
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(PermissionError, match="outside"):
        state.repair_delete(outside)
    inside = state.root / "tmp" / "dummy"
    inside.write_text("dummy", encoding="utf-8")
    state.acl.owners[str(inside)] = "foreign-owner"
    with pytest.raises(PermissionError, match="foreign owner"):
        state.repair_delete(inside)
    assert state.acl.grants == []
    assert outside.read_text(encoding="utf-8") == "keep"


def test_refuses_symlink_and_preserves_target(private_state, tmp_path):
    state = private_state
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "dummy"
    sentinel.write_text("keep", encoding="utf-8")
    link = state.root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlink privilege unavailable: {error}")
    try:
        with pytest.raises(PermissionError, match="outside|reparse/link"):
            state.repair_delete(link / "dummy")
        reports = []
        assert not state.cleanup(warn=reports.append)
        assert str(state.root) in reports[0]
        assert state.acl.grants == []
        assert sentinel.read_text(encoding="utf-8") == "keep"
    finally:
        link.unlink()


def test_reparse_attribute_rejected_without_real_junction(private_state):
    state = private_state
    real_lstat = Path.lstat

    def fake_lstat(path, *args, **kwargs):
        actual = real_lstat(path, *args, **kwargs)
        if path == state.root / "tmp":
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        return actual

    with mock.patch.object(Path, "lstat", fake_lstat):
        with pytest.raises(PermissionError, match="reparse/link"):
            state.repair_delete(state.root / "tmp")
    assert state.acl.grants == []


def test_cleanup_failure_warns_exact_root(private_state):
    state = private_state
    with mock.patch.object(isolation.shutil, "rmtree", side_effect=PermissionError("locked")):
        with pytest.warns(pytest.PytestWarning, match="Isolated pytest root retained") as warning:
            assert not state.cleanup()
    assert str(state.root) in str(warning[0].message)
    assert "locked" in state.cleanup_error
    assert state.root.exists()
    assert state.acl.grants == []


def test_hook_restores_even_when_cleanup_fails(private_state):
    state = private_state
    config = SimpleNamespace(_test_isolation=state)
    with mock.patch.object(state, "cleanup", return_value=False) as cleanup:
        isolation.pytest_unconfigure(config)
    assert os.environ.get("APPDATA") == state.original_appdata
    assert tempfile.tempdir == state.original_tempdir
    cleanup.assert_called_once_with()
    assert not hasattr(config, "_test_isolation")


def test_windows_backend_appends_only_current_user_delete():
    dacl = mock.Mock()
    sd = mock.Mock()
    sd.GetSecurityDescriptorOwner.return_value = "owner-object"
    sd.GetSecurityDescriptorDacl.return_value = dacl
    security = SimpleNamespace(
        OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4, ACL_REVISION=2,
        GetFileSecurity=mock.Mock(return_value=sd),
        ConvertSidToStringSid=mock.Mock(return_value="owner-sid"),
        SetFileSecurity=mock.Mock())
    backend = object.__new__(isolation.WindowsDeleteAccess)
    backend.security = security
    backend.user = "token-user-object"
    validate = mock.Mock()
    backend.grant_delete(Path("dummy"), {"owner-sid"}, validate)
    dacl.AddAccessAllowedAceEx.assert_called_once_with(2, 0, 0x10000, "token-user-object")
    sd.SetSecurityDescriptorDacl.assert_called_once_with(1, dacl, 0)
    security.SetFileSecurity.assert_called_once_with("dummy", 4, sd)
    validate.assert_called_once_with(Path("dummy"))
    assert not dacl.DeleteAce.called
    assert not sd.SetSecurityDescriptorOwner.called


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL integration")
def test_actual_protected_dummy_file_and_directory(tmp_path):
    import ntsecuritycon
    import win32security

    state = isolation.SessionIsolation.create(temp_base=tmp_path)
    directory = state.root / "tmp" / "protected-dummy"
    directory.mkdir()
    dummy = directory / "dummy.txt"
    dummy.write_text("not a database", encoding="utf-8")
    try:
        for path in (dummy, directory):
            sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
            acl = win32security.ACL()
            acl.AddAccessAllowedAce(win32security.ACL_REVISION,
                                    ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
                                    state.acl.user)
            sd.SetSecurityDescriptorDacl(1, acl, 0)
            win32security.SetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION |
                win32security.PROTECTED_DACL_SECURITY_INFORMATION, sd)
        with mock.patch.object(state.acl, "grant_delete", wraps=state.acl.grant_delete) as grant:
            assert state.cleanup()
            assert any(call.args[0] == dummy for call in grant.call_args_list)
        assert not state.root.exists()
    finally:
        state.restore()
        assert state.cleanup()
