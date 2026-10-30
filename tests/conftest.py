"""Session-only filesystem isolation; importing this module changes nothing.

Public helpers: canonical_os_temp(), SessionIsolation.create(), .restore(),
.cleanup(), .validate_path() and .repair_delete(). The pytest session state is
available as config._test_isolation. Production ACLs remain untouched during
collection/tests; DELETE repair is confined to teardown of this new root.
"""
from dataclasses import dataclass
import gc
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import warnings

import pytest


def canonical_os_temp():
    """Read OS temp, ignoring tempfile's possibly build-local cache."""
    if sys.platform == "win32":
        import win32api
        location = win32api.GetTempPath()
    else:
        cached = tempfile.tempdir
        try:
            tempfile.tempdir = None
            location = tempfile.gettempdir()
        finally:
            tempfile.tempdir = cached
    # resolve expands Windows 8.3 aliases as well as symlinks.
    return Path(location).resolve(strict=True)


class WindowsDeleteAccess:
    """Current-token-only DELETE grant; never changes owners or removes ACEs."""

    def __init__(self):
        import win32api
        import win32con
        import win32security
        self.security = win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            self.user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            owner = win32security.GetTokenInformation(token, win32security.TokenOwner)
            self.user_sid = win32security.ConvertSidToStringSid(self.user)
            self.token_owner_sid = win32security.ConvertSidToStringSid(owner)
        finally:
            token.Close()

    def owner(self, path):
        security = self.security
        sd = security.GetFileSecurity(str(path), security.OWNER_SECURITY_INFORMATION)
        return security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())

    def grant_delete(self, path, allowed_owners, validate):
        security = self.security
        sd = security.GetFileSecurity(
            str(path), security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        owner = security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
        if owner not in allowed_owners:
            raise PermissionError(f"Refusing ACL repair for foreign owner: {path}")
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            raise PermissionError(f"Refusing to replace a NULL DACL: {path}")
        # Preserve every existing allow/deny ACE, flags, and owner. No inheritance.
        dacl.AddAccessAllowedAceEx(security.ACL_REVISION, 0, 0x00010000, self.user)
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        validate(path)
        security.SetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION, sd)


@dataclass
class SessionIsolation:
    root: Path
    original_appdata: str | None
    original_tempdir: object
    root_identity: tuple
    acl: object = None
    creator_sid: str | None = None
    restored: bool = False
    cleanup_error: str | None = None

    @classmethod
    def create(cls, *, temp_base=None, repository=None, acl=None):
        """Create a fresh root, then install APPDATA/tempfile overrides.

        temp_base/acl are injectable for offline tests. No existing directory is
        adopted, repaired or deleted. A temp location within the checkout fails
        closed instead of falling back to the real user's APPDATA.
        """
        base = (canonical_os_temp() if temp_base is None else Path(temp_base).resolve(strict=True))
        repository = (Path(__file__).resolve().parents[1] if repository is None
                      else Path(repository).resolve())
        if base == repository or base.is_relative_to(repository):
            raise ValueError(f"OS temp must be outside the repository: {base}")
        if acl is None and sys.platform == "win32":
            acl = WindowsDeleteAccess()
        root = Path(tempfile.mkdtemp(prefix="neossh-pytest-", dir=str(base))).resolve(strict=True)
        info = root.lstat()
        state = cls(root, os.environ.get("APPDATA"), tempfile.tempdir,
                    (info.st_dev, info.st_ino), acl)
        try:
            if acl is not None:
                state.creator_sid = acl.owner(root)
                if state.creator_sid not in {acl.user_sid, acl.token_owner_sid}:
                    raise PermissionError(f"New test root has unexpected owner: {root}")
            (root / "appdata").mkdir(mode=0o700)
            (root / "tmp").mkdir(mode=0o700)
            os.environ["APPDATA"] = str(root / "appdata")
            tempfile.tempdir = str((root / "tmp").resolve(strict=True))
            return state
        except BaseException:
            state.restore()
            state.cleanup()
            raise

    def restore(self):
        """Restore even an originally absent APPDATA or uncached tempfile.tempdir."""
        if not self.restored:
            if self.original_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = self.original_appdata
            tempfile.tempdir = self.original_tempdir
            self.restored = True

    def validate_path(self, path):
        """Reject outside paths, replaced roots, links/junctions and hardlinks."""
        path = Path(os.path.abspath(os.fspath(path)))
        if path != self.root and not path.is_relative_to(self.root):
            raise PermissionError(f"Refusing path outside test root: {path}")
        root_info = self.root.lstat()
        if (root_info.st_dev, root_info.st_ino) != self.root_identity:
            raise PermissionError(f"Test root identity changed: {self.root}")
        current = self.root
        parts = path.relative_to(self.root).parts
        for index in range(len(parts) + 1):
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError(f"Refusing reparse/link path: {current}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise PermissionError(f"Refusing hardlinked file: {current}")
            if index < len(parts):
                current = current / parts[index]
        if path.resolve(strict=True) != path:
            raise PermissionError(f"Refusing noncanonical test path: {path}")
        return path

    def repair_delete(self, path):
        """Permit only this token's DELETE, and only on a validated owned path."""
        path = self.validate_path(path)
        if self.acl is None:
            raise PermissionError(f"No safe Windows DELETE repair available: {path}")
        allowed = {self.acl.user_sid, self.acl.token_owner_sid, self.creator_sid}
        if self.acl.owner(path) not in allowed:
            raise PermissionError(f"Refusing ACL repair for foreign owner: {path}")
        self.acl.grant_delete(path, allowed, self.validate_path)

    def cleanup(self, *, warn=None):
        """Remove only this session root; report and retain unsafe/locked roots."""
        if warn is None:
            warn = lambda message: warnings.warn(message, pytest.PytestWarning, stacklevel=2)
        try:
            gc.collect()  # sqlite context managers commit but need not close handles.
            if not os.path.lexists(self.root):
                return True
            self.validate_path(self.root)
            # Refuse the entire tree before any deletion if a link was injected.
            for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=_raise):
                for name in dirs + files:
                    self.validate_path(Path(directory) / name)

            def onexc(function, path, error):
                if not isinstance(error, PermissionError):
                    raise error
                self.repair_delete(path)
                function(path)

            shutil.rmtree(self.root, onexc=onexc)
            self.cleanup_error = None
            return True
        except Exception as error:
            self.cleanup_error = f"Isolated pytest root retained at {self.root}: {error}"
            warn(self.cleanup_error)
            return False


def _raise(error):
    raise error


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if not hasattr(config, "_test_isolation"):
        config._test_isolation = SessionIsolation.create()


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    state = getattr(config, "_test_isolation", None)
    if state is not None:
        try:
            state.restore()
        finally:
            state.cleanup()
        del config._test_isolation


def pytest_report_header(config):
    state = getattr(config, "_test_isolation", None)
    if state is not None:
        return f"isolated test profile: {state.root}"
    return None


@pytest.fixture(scope="session")
def test_isolation(pytestconfig):
    """Inspection only: do not clean this root until pytest_unconfigure."""
    return pytestconfig._test_isolation


test_isolation.__test__ = False
SessionIsolation.__test__ = False
WindowsDeleteAccess.__test__ = False

__all__ = ["SessionIsolation", "WindowsDeleteAccess", "canonical_os_temp"]
