"""Windows-user startup preferences, independent of Qt, login and the database.

Call apply_startup_preferences BEFORE saving the matching SQLite settings. Keep
its returned rollback callable and invoke it if the database save fails. Failure
raises StartupPreferencesError; callers must not display a successful settings save.
The bootstrap file contains only a version and require_admin, never credentials.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    import winreg
except ImportError:  # Allows stdlib tests (and preference reads) off Windows.
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
RUN_VALUE = "SSHDriveMgr"


class StartupPreferencesError(RuntimeError):
    """The requested OS/bootstrap preferences could not be fully applied."""


def _is_windows():
    return os.name == "nt"


def bootstrap_preferences_path():
    """The same per-Windows-user config directory used by src.config."""
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "SSHDriveMgr" / "startup_preferences.json"


def read_bootstrap_preferences():
    """Read pre-login elevation preference, defaulting safely to non-admin.

    Legacy config.json is a fallback ONLY while the new file is absent. Once a
    new preference is saved, even an invalid/unreadable new file cannot resurrect
    a stale legacy require_admin=True. This function never opens the database.
    """
    path = bootstrap_preferences_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        value = data.get("require_admin") if isinstance(data, dict) else None
        return {"require_admin": value if type(value) is bool else False}
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return {"require_admin": False}

    try:
        with (path.parent / "config.json").open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        settings = data.get("settings", {}) if isinstance(data, dict) else {}
        value = settings.get("require_admin") if isinstance(settings, dict) else None
        return {"require_admin": value if type(value) is bool else False}
    except (OSError, ValueError):
        return {"require_admin": False}


def _atomic_write_bootstrap(path, payload):
    """Replace only the captured bootstrap path, preserving bytes on rollback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _save_bootstrap_preferences(require_admin, path=None):
    if path is None:
        path = bootstrap_preferences_path()
    payload = json.dumps({"version": 1, "require_admin": require_admin}).encode("utf-8")
    _atomic_write_bootstrap(path, payload)


def _snapshot_rollback(registry_values, bootstrap_path, bootstrap_bytes):
    """Return a caller-owned rollback token; no live registry handles retained."""
    restored = False

    def rollback():
        nonlocal restored
        if restored:
            return
        errors = []
        for key_path, name, previous in reversed(registry_values):
            key = None
            try:
                key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                         winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
                _write_value(key, name, previous)
            except Exception as error:
                errors.append(f"{key_path} [{name}]: {error}")
            finally:
                if key is not None:
                    try:
                        winreg.CloseKey(key)
                    except Exception as error:
                        errors.append(f"closing {key_path}: {error}")
        try:
            if bootstrap_bytes is None:
                # This is the absolute bootstrap target captured before apply,
                # not a recomputed path or a directory. Restore its absence only.
                try:
                    bootstrap_path.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_write_bootstrap(bootstrap_path, bootstrap_bytes)
        except Exception as error:
            errors.append(f"bootstrap preference restoration: {error}")
        if errors:
            raise StartupPreferencesError("Startup preference rollback failed: " + "; ".join(errors))
        restored = True

    return rollback


def _absolute_launch_path(value):
    path = str(Path(value).resolve())
    if not value or any(char in path for char in ('"', '\x00', '\r', '\n')):
        raise ValueError("Invalid application launch path")
    return path


def application_launch_command():
    """Exact executable/arguments for HKCU Run (never cmd.exe or a shell)."""
    executable = _absolute_launch_path(sys.executable)
    command = '"' + executable + '"'
    if not getattr(sys, "frozen", False):
        script = _absolute_launch_path(Path(__file__).resolve().parent.parent / "main.py")
        command += " " + subprocess.list2cmdline([script])
    return command


def elevation_launch_arguments(arguments):
    """ShellExecute parameters, including main.py for a source Python launch.

    The child gets --no-elevate as a second guard if its admin-token probe fails.
    Windows command-line quoting handles embedded quotes and trailing slashes.
    """
    args = [arg for arg in arguments if arg != "--no-elevate"]
    if not getattr(sys, "frozen", False):
        args.insert(0, _absolute_launch_path(Path(__file__).resolve().parent.parent / "main.py"))
    args.append("--no-elevate")
    return subprocess.list2cmdline(args)


def _layer_executable():
    if not getattr(sys, "frozen", False):
        return None  # Never put RUNASADMIN on the shared Python interpreter.
    path = _absolute_launch_path(sys.executable)
    if Path(path).suffix.lower() != ".exe" or Path(path).name.lower() in ("python.exe", "pythonw.exe"):
        return None
    return path


def _read_value(key, name):
    try:
        return winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None


def _write_value(key, name, value):
    if value is None:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    else:
        data, kind = value
        winreg.SetValueEx(key, name, 0, kind, data)


def _without_admin_layer(previous):
    if previous is not None and (previous[1] != winreg.REG_SZ or not isinstance(previous[0], str)):
        raise ValueError("AppCompat Layers value is not a REG_SZ string; refusing to overwrite it")
    flags = previous[0].split() if previous else []
    if not any(flag.upper() == "RUNASADMIN" for flag in flags):
        return previous
    flags = [flag for flag in flags if flag.upper() != "RUNASADMIN"]
    if not flags or flags == ["~"]:
        return None
    return (" ".join(flags), winreg.REG_SZ)


def apply_startup_preferences(start_with_windows, require_admin):
    """Apply HKCU Run, clean legacy RUNASADMIN and save pre-login preference.

    Return a zero-argument rollback callable for a subsequent SQLite save failure.
    It restores the exact Windows-user-wide registry values/types and bootstrap
    bytes/absence, NOT the previous app account's booleans. Discard it after a
    successful database save; invoke immediately on failure, before another save.
    A successful rollback is idempotent. Restoration failures raise a combined
    StartupPreferencesError and permit retrying the token.

    Registry values are also rolled back internally if applying either registry
    operation or the final atomic file save fails. Bootstrap snapshot errors fail
    closed before any registry writes. On non-Windows only the bootstrap file is
    saved. No administrator rights are needed for HKCU.
    """
    if type(start_with_windows) is not bool or type(require_admin) is not bool:
        raise TypeError("Startup preferences must be bool values")
    keys = []
    attempted = []
    registry_values = []
    stage = "snapshotting pre-login administrator preference"
    try:
        bootstrap_path = bootstrap_preferences_path().absolute()
        try:
            bootstrap_bytes = bootstrap_path.read_bytes()
        except FileNotFoundError:
            bootstrap_bytes = None
        if _is_windows():
            if winreg is None:
                raise RuntimeError("Windows registry API is unavailable")
            stage = "opening Windows startup registry keys"
            run = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                     winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
            keys.append(run)
            old_run = _read_value(run, RUN_VALUE)
            registry_values.append((RUN_KEY, RUN_VALUE, old_run))
            new_run = (application_launch_command(), winreg.REG_SZ) if start_with_windows else None
            changes = [(run, RUN_VALUE, old_run, new_run)]
            executable = _layer_executable()
            if executable:
                layers = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LAYERS_KEY, 0,
                                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
                keys.append(layers)
                old_layers = _read_value(layers, executable)
                registry_values.append((LAYERS_KEY, executable, old_layers))
                # Windows can suppress HKCU Run entries whose EXE requires
                # elevation. Bootstrap requests runas AFTER the normal launch;
                # remove legacy RUNASADMIN instead of marking the startup EXE.
                changes.append((layers, executable, old_layers, _without_admin_layer(old_layers)))
            for key, name, previous, desired in changes:
                if previous != desired:
                    stage = "updating " + ("Windows autostart" if name == RUN_VALUE else "application elevation flags")
                    attempted.append((key, name, previous))
                    _write_value(key, name, desired)
        stage = "saving pre-login administrator preference"
        _save_bootstrap_preferences(require_admin, bootstrap_path)
        return _snapshot_rollback(tuple(registry_values), bootstrap_path, bootstrap_bytes)
    except Exception as error:
        rollback_errors = []
        for key, name, previous in reversed(attempted):
            try:
                _write_value(key, name, previous)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        message = "Failed " + stage + ": " + str(error)
        if rollback_errors:
            message += "; registry rollback also failed: " + "; ".join(rollback_errors)
        raise StartupPreferencesError(message) from error
    finally:
        for key in reversed(keys):
            winreg.CloseKey(key)
