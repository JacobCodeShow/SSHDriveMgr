"""
updater.py – Update check, installer download and deferred installation.

Security policy:
    Download the exact Setup/portable asset from the fixed GitHub repository.
    Require a GitHub API SHA256 digest or official release checksum, refresh
    that metadata before applying, and rehash the staged payload. Checksums
    provide integrity relative to GitHub/TLS, not a publisher signature.

    AppData markers and helper scripts are user-writable. Automatic handover
    therefore fails closed when elevated (or when elevation is unknown). Start
    the app without administrator privileges to apply a pending update. The
    deferred helper also rehashes after waiting, immediately before applying.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from packaging import version
from PyQt6.QtCore import QObject, pyqtSignal

from src.app_logger import logger
from src import update_security as security

GITHUB_API_URL = security.LATEST_URL

# Characters that would break out of a quoted path inside the generated .cmd
# helper (or let it run something else entirely).
_UNSAFE_PATH_CHARS = '"%&|<>^\r\n'

_MARKER_NAME = "pending_update.json"
_ATTEMPT_NAME = "update_attempt.json"
_HANDOVER_ERROR = ""


def get_update_handover_error() -> str:
    """User-facing reason for the last rejected handover (for dialog/startup UI)."""
    return _HANDOVER_ERROR


def _handover_failure(message: str) -> bool:
    global _HANDOVER_ERROR
    _HANDOVER_ERROR = message
    logger.error(message)
    return False


def is_installed_copy() -> bool:
    """Recognize an Inno installation, including per-user/custom directories.

    Directory names (including Program Files) are not installation evidence.
    Only this application's uninstall registration or a local Inno uninstaller
    and its matching data file qualify. Registry access is strictly read-only.
    """
    if not getattr(sys, "frozen", False):
        return False
    app_dir = Path(sys.executable).resolve().parent
    if any(path.with_suffix(".dat").is_file() for path in app_dir.glob("unins[0-9][0-9][0-9].exe")
           if path.is_file()):
        return True
    try:
        import winreg
        key_name = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                    r"\{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1")
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        location, _ = winreg.QueryValueEx(key, "InstallLocation")
                        if location and Path(location).resolve() == app_dir:
                            return True
                except (OSError, ValueError, TypeError):
                    continue
    except (ImportError, OSError):
        pass
    return False


def is_onedir_copy() -> bool:
    """Use PyInstaller's extraction location, not the executable's name.

    Onedir bundles use the executable directory (older PyInstaller) or a
    contents child (_internal by default); onefile uses a temporary _MEI
    directory elsewhere. Never replace a known onedir bundle with onefile.
    """
    if not getattr(sys, "frozen", False):
        return False
    bundle = getattr(sys, "_MEIPASS", None)
    if not bundle:
        return False
    app_dir = Path(sys.executable).resolve().parent
    bundle_dir = Path(bundle).resolve()
    return bundle_dir == app_dir or (
        bundle_dir.parent == app_dir and not bundle_dir.name.startswith("_MEI"))


def runtime_update_type() -> str:
    """Installation evidence takes precedence over the underlying bundle layout."""
    if not getattr(sys, "frozen", False):
        return "source"
    if is_installed_copy():
        return "installer"
    if is_onedir_copy():
        return "onedir_zip"
    return "portable_exe"


def matching_release_asset(data: dict, release_version: str) -> tuple[str, str, str]:
    """Pick only the matching official asset; ZIPs are explicitly manual updates.

    Never send a ZIP into the executable handover pipeline. Missing matching
    artifacts link to this exact release, rather than silently switching types.
    """
    security.checked_version(release_version)
    if data.get("tag_name") not in (release_version, "v" + release_version):
        raise security.UpdateSecurityError("Release tag does not match the update version")
    kind = runtime_update_type()
    name = {
        "installer": f"SSHDriveMgr-Setup-{release_version}.exe",
        "portable_exe": "SSHDriveMgr.exe",
        "onedir_zip": f"SSHDriveMgr-onedir-{release_version}.zip",
    }.get(kind)
    if name:
        matches = [asset for asset in data.get("assets", []) if asset.get("name") == name]
        if len(matches) > 1:
            raise security.UpdateSecurityError("Matching update asset is ambiguous")
        if matches:
            tag = data["tag_name"]
            canonical = security.release_asset_url(tag, name)
            if matches[0].get("browser_download_url") != canonical:
                raise security.UpdateSecurityError("Matching update asset URL is not canonical")
            return name, canonical, kind
    logger.warning("Release has no matching %s asset; manual release page required.", kind)
    tag = data.get("tag_name", "v" + release_version)
    return "", f"https://github.com/{security.REPOSITORY}/releases/tag/{tag}", "browser"


# ── Pending-update bookkeeping ──────────────────────────────────────────────

def updates_dir() -> Path:
    """Directory holding a downloaded installer and its marker file."""
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "SSHDriveMgr" / "updates"


def _marker_path() -> Path:
    return updates_dir() / _MARKER_NAME


def read_pending_update() -> dict | None:
    """
    Raw pending-update record, or None if there is none / the installer file
    is gone. Does not compare versions — see get_pending_update().
    """
    try:
        with open(_marker_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        security.validate_record(updates_dir(), data)
    except (ValueError, TypeError, OSError):
        return None
    return data


def get_pending_update(current_version: str) -> dict | None:
    """
    Pending update that is actually newer than the running version.
    A record for a version we are already running is stale (the update went
    through) and gets cleaned up. Records belonging to another distribution
    are ignored without being cleaned, disarmed or applied.
    """
    data = read_pending_update()
    if not data:
        return None
    # Several distribution types share AppData. Never consume, disarm or clean
    # another type's staged update, even if this copy is already newer.
    if data.get("asset_type") != runtime_update_type():
        return None
    try:
        if version.parse(str(data.get("version", "0"))) <= version.parse(str(current_version)):
            logger.info("Pending update is not newer than the running version — cleaning up.")
            clear_pending_update()
            return None
    except Exception:
        return None
    return data


def save_pending_update(version_str: str, installer_path: str, asset_name: str,
                        changelog: str = "", install_on_next_start: bool = True,
                        asset_type: str = "installer", integrity: dict | None = None) -> None:
    """Atomically remember a verified official asset; failures propagate to the UI."""
    official = integrity or security.resolve_release(version_str, asset_name, asset_type)
    if any(official.get(key) != value for key, value in (
            ("version", version_str), ("asset_name", asset_name), ("asset_type", asset_type))):
        raise security.UpdateSecurityError("Integrity metadata belongs to another update")
    path = security.staging_path(updates_dir(), version_str, asset_name, asset_type, installer_path)
    security.verify_payload(path, official)
    record = dict(official, schema=1, installer_path=str(path), changelog=changelog,
                  install_on_next_start=bool(install_on_next_start))
    security.validate_record(updates_dir(), record)
    security.atomic_json(_marker_path(), record)


def set_install_on_next_start(enabled: bool, record: dict | None = None) -> bool:
    """Persist arming state; handover can supply its already verified record.

    Never mutate the caller's record, and never report a successful disarm if
    the atomic marker write failed. The specific failure is retained for UI.
    """
    data = record if record is not None else read_pending_update()
    if not data:
        return _handover_failure("No verified pending update is available to change its schedule.")
    if data.get("asset_type") != runtime_update_type():
        return _handover_failure("The pending update belongs to another distribution; its schedule was not changed.")
    try:
        data = dict(data, install_on_next_start=bool(enabled))
        security.validate_record(updates_dir(), data)
        security.atomic_json(_marker_path(), data)
        logger.info(f"Update install-on-next-start set to {bool(enabled)}.")
        return True
    except Exception as e:
        return _handover_failure(f"Failed to update pending-update marker: {e}")


def clear_pending_update() -> None:
    """Drop the marker and any downloaded installer."""
    data = read_pending_update()
    if data:
        try:
            # Never delete a marker-supplied arbitrary path, even during cleanup.
            security.validate_record(updates_dir(), data).unlink()
        except Exception:
            pass
    try:
        os.remove(_marker_path())
    except Exception:
        pass


# ── Did the update actually happen? ─────────────────────────────────────────

def _attempt_path() -> Path:
    return updates_dir() / _ATTEMPT_NAME


def record_update_attempt(from_version: str, to_version: str) -> None:
    """
    Remember that we handed over to the installer.

    Whether the update really went through can only be told on the next start:
    if the app now runs the new version, it worked. The installer reports
    nothing back, and the pending-update record is gone by then — the helper
    script deletes the installer once it finished.
    """
    record = {
        "from_version": str(from_version),
        "to_version": str(to_version),
        "started_at": int(time.time()),
    }
    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
        with open(_attempt_path(), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write update-attempt marker: {e}")


def take_update_attempt() -> dict | None:
    """
    Read the attempt record and drop it — it is evaluated exactly once.

    A stale record (older than a week) is discarded without a verdict: the
    machine may have been off in between, and a late "installed" would land in
    the wrong day's statistics.
    """
    try:
        with open(_attempt_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    finally:
        try:
            os.remove(_attempt_path())
        except Exception:
            pass

    if not isinstance(data, dict) or not data.get("to_version"):
        return None
    if time.time() - int(data.get("started_at", 0)) > 7 * 86400:
        logger.info("Discarding stale update-attempt marker.")
        return None
    return data


# ── Handover to the installer ───────────────────────────────────────────────

def _is_safe_script_path(path: str) -> bool:
    """Path can be embedded in a quoted .cmd argument without escaping tricks."""
    return bool(path) and not any(c in path for c in _UNSAFE_PATH_CHARS)


def launch_pending_installer(record: dict, from_version: str = "") -> bool:
    """
    Start a detached helper script that waits for this process to exit, applies
    the update, and relaunches the app afterwards. The caller must quit right
    after this returns True.

    Two update types are supported:
      - installer   : run the Setup exe (for registered/marked Inno installations)
      - portable_exe: backup + overwrite the running EXE (for portable copies)

    `from_version` is only used for the statistics marker — pass the running
    version so the next start can tell whether the update took effect.
    """
    global _HANDOVER_ERROR
    _HANDOVER_ERROR = ""
    if not getattr(sys, "frozen", False):
        return _handover_failure("Updates can only be applied by the packaged application.")
    if security.is_elevated():
        return _handover_failure(
            "Automatic update blocked: restart without administrator privileges to apply the update. "
            "Writable AppData payloads/helpers must not run elevated.")
    try:
        payload, official = security.verify_for_apply(updates_dir(), record)
    except Exception as error:
        return _handover_failure(f"Update integrity validation failed; download again: {error}")
    update_file = str(payload)
    asset_type = official["asset_type"]
    expected_type = runtime_update_type()
    if asset_type not in ("installer", "portable_exe") or asset_type != expected_type:
        return _handover_failure(
            f"Update type {asset_type!r} does not match this {expected_type} copy. "
            "Use the matching Setup, portable EXE or complete onedir ZIP instead.")
    app_exe = sys.executable
    exe_name = os.path.basename(app_exe)
    script_path = str(updates_dir() / "run_update.cmd")

    for p in (update_file, app_exe, script_path):
        if not _is_safe_script_path(p):
            return _handover_failure(f"Updater: refusing to build helper script for unsafe path: {p}")

    # Wait for the app to be gone before touching its files: with a PyInstaller
    # onefile build the bootloader parent outlives the Python child briefly and
    # keeps the exe locked, so we wait on the image name, not on a PID.
    wait_block = f"""@echo off
chcp 65001 >nul
setlocal disabledelayedexpansion
set /a _tries=0

:waitloop
tasklist /fi "imagename eq {exe_name}" /nh 2>nul | find /i "{exe_name}" >nul
if errorlevel 1 goto apply
set /a _tries+=1
if %_tries% GEQ 60 goto abort
timeout /t 1 /nobreak >nul
goto waitloop

"""

    # Rehash at the actual apply boundary too, after waiting for the app. This
    # is integrity checking, not a publisher signature or a same-user sandbox.
    ps_path = update_file.replace("'", "''")
    hash_guard = (
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        '-NoProfile -NonInteractive -Command '
        f'"try {{ if ((Get-FileHash -LiteralPath \'{ps_path}\' -Algorithm SHA256 '
        f'-ErrorAction Stop).Hash -ne \'{official["sha256"]}\') {{ exit 1 }} }} '
        'catch { exit 1 }"\n'
        'if errorlevel 1 goto restart\n'
    )

    if asset_type == "portable_exe":
        # Backup old exe, overwrite with new one, rollback on failure.
        apply_block = f""":apply
{hash_guard}copy /y "{app_exe}" "{app_exe}.bak" >nul 2>&1
copy /y "{update_file}" "{app_exe}"
if errorlevel 1 (
  copy /y "{app_exe}.bak" "{app_exe}" >nul 2>&1
  goto restart
)
del /f /q "{app_exe}.bak" >nul 2>&1
del /f /q "{update_file}" >nul 2>&1
goto restart

"""
    elif asset_type == "installer":
        # Installer: run it, then discard on success.
        apply_block = f""":apply
{hash_guard}start "" /wait "{update_file}"
rem Exit code 0 means the installer finished; only then is it safe to discard.
if not errorlevel 1 del /f /q "{update_file}" >nul 2>&1
goto restart

"""

    restart_block = f""":restart
rem A restarted frozen app must not reuse the previous onefile extraction.
set PYINSTALLER_RESET_ENVIRONMENT=1
start "" "{app_exe}"
del /f /q "%~f0" >nul 2>&1
exit /b 0

:abort
rem The app is still alive: do not hash, copy, delete or restart anything.
exit /b 1
"""

    script = wait_block + apply_block + restart_block

    try:
        updates_dir().mkdir(parents=True, exist_ok=True)
        security.atomic_text(script_path, script)
    except Exception as e:
        return _handover_failure(f"Failed to write update helper script: {e}")

    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        # Last local check immediately before launching the helper. The helper
        # repeats the hash after its wait, at the eventual execution boundary.
        security.validate_record(updates_dir(), record)
        security.verify_payload(payload, official)
        # Both Install Now and next-start must persistently disarm before any
        # detached process can take over. Cancellation then cannot loop.
        if not set_install_on_next_start(False, record=record):
            return False  # preserve the marker write's precise failure reason
        helper_env = os.environ.copy()
        helper_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(
            [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
             "/d", "/c", script_path],
            startupinfo=startupinfo,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            env=helper_env,
        )
    except Exception as e:
        return _handover_failure(f"Failed to start update helper script: {e}")

    record_update_attempt(from_version, str(record.get("version", "")))
    logger.info(f"Update handover scheduled ({asset_type}): {update_file}")
    return True


def maybe_install_pending_update(current_version: str) -> bool:
    """
    Startup hook. Returns True if the installer was handed over and the caller
    must exit immediately (the helper restarts the app afterwards).

    Call this as early as possible — after the single-instance check, before
    the database and the main window are touched.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False

    try:
        record = get_pending_update(current_version)
    except Exception as e:
        logger.warning(f"Pending update check failed: {e}")
        return False

    if not record or not record.get("install_on_next_start"):
        return False
    if security.is_elevated():
        return _handover_failure(
            "Pending update requires a non-elevated app start. Restart without administrator "
            "privileges to apply it; the pending update has been left armed.")

    # The common handover path persistently disarms before launching; a failed
    # marker write aborts both this startup path and Install Now identically.
    return launch_pending_installer(record, from_version=current_version)


# ── Update check / download ─────────────────────────────────────────────────

class UpdaterManager(QObject):
    # version, changelog, URL, type: installer | portable_exe | onedir_zip | browser.
    # onedir_zip is a browser/manual download, never an executable handover.
    update_available = pyqtSignal(str, str, str, str)
    no_update_available = pyqtSignal()
    check_failed = pyqtSignal(str)
    download_progress = pyqtSignal(int)
    download_finished = pyqtSignal(bool, str)  # success, path or error message
    handover_finished = pyqtSignal(bool, str)
    supports_async_handover = True

    def __init__(self, current_version: str):
        super().__init__()
        self.current_version = current_version
        self.update_file_path = None
        self.latest_version = ""
        self._asset_name = ""
        self._asset_type = "installer"
        self._changelog = ""
        self._is_downloading = False
        self._is_handing_over = False

    def launch_pending_update_async(self):
        """Refresh remote metadata and hash in a worker, never on the GUI thread."""
        if self._is_handing_over:
            return
        self._is_handing_over = True

        def _worker():
            success, message = False, ""
            try:
                record = self.pending_update()
                if not record:
                    message = "No verified update is available. Download it again."
                else:
                    success = launch_pending_installer(record, from_version=self.current_version)
                    if not success:
                        message = get_update_handover_error()
            except Exception as error:
                message = f"Update handover failed: {error}"
            finally:
                self._is_handing_over = False
                self.handover_finished.emit(success, message)

        threading.Thread(target=_worker, daemon=True).start()

    def pending_update(self) -> dict | None:
        """Already downloaded installer for a newer version, if any."""
        return get_pending_update(self.current_version)

    def check_for_updates_async(self):
        """Checks in the background whether a new version is available."""
        def _worker():
            try:
                data = security.fetch_release()
                security.checked_version(data.get("tag_name", "").removeprefix("v"))

                latest_version_tag = data.get("tag_name", "").lstrip("v")
                if not latest_version_tag:
                    self.check_failed.emit("GitHub API returned no tag_name")
                    return

                # Compare versions using packaging.version for proper semantic versioning
                if version.parse(latest_version_tag) > version.parse(self.current_version):
                    changelog = data.get("body", "No changelog available.")
                    asset_name, download_url, obj_type = matching_release_asset(data, latest_version_tag)

                    self.latest_version = latest_version_tag
                    self._asset_name = asset_name
                    self._asset_type = obj_type
                    self._changelog = changelog
                    self.update_available.emit(latest_version_tag, changelog, download_url, obj_type)
                else:
                    self.no_update_available.emit()

            except Exception as e:
                logger.warning(f"Failed to check for updates: {e}")
                try:
                    self.check_failed.emit(str(e))
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def download_update_async(self, download_url: str):
        if (self._asset_type not in ("installer", "portable_exe")
                or self._asset_type != runtime_update_type()):
            self.download_finished.emit(False, "This update is not an automatic update for this distribution.")
            return
        if self._is_downloading:
            return
        self._is_downloading = True
        self.update_file_path = None

        def _worker():
            try:
                official = security.resolve_release(self.latest_version, self._asset_name, self._asset_type)
                if download_url != official["download_url"]:
                    raise security.UpdateSecurityError("Requested URL is not the canonical release asset")
                # Preserve the old pending update until the new payload verifies.
                path = security.download_payload(updates_dir(), official, self.download_progress.emit)
                self.update_file_path = str(path)
                save_pending_update(
                    self.latest_version, self.update_file_path, self._asset_name,
                    changelog=self._changelog, install_on_next_start=True,
                    asset_type=self._asset_type, integrity=official,
                )
                self.download_finished.emit(True, self.update_file_path)
            except Exception as e:
                logger.error(f"Download failed: {e}")
                try:
                    if self.update_file_path and os.path.exists(self.update_file_path):
                        security.staging_path(updates_dir(), self.latest_version, self._asset_name,
                                              self._asset_type, self.update_file_path).unlink()
                except Exception:
                    pass
                self.update_file_path = None
                self.download_finished.emit(False, str(e))
            finally:
                self._is_downloading = False

        threading.Thread(target=_worker, daemon=True).start()

    def _verify_checksum(self):
        """Compatibility hook: verification is mandatory; failures propagate."""
        official = security.resolve_release(self.latest_version, self._asset_name, self._asset_type)
        path = security.staging_path(updates_dir(), self.latest_version, self._asset_name,
                                     self._asset_type, self.update_file_path)
        security.verify_payload(path, official)
