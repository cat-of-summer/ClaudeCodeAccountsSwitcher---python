from __future__ import annotations

import ctypes
import os
from pathlib import Path

from ui.i18n import t

try:
    import winreg
except ImportError:
    winreg = None

ENVIRONMENT_KEY = "Environment"
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


class PathError(Exception):
    pass


def _read_user_path() -> tuple[str, int]:
    assert winreg is not None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_READ
        ) as key:
            value, value_type = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return "", winreg.REG_EXPAND_SZ
    except OSError as exc:
        raise PathError(t("error.registry_read", error=exc)) from exc
    return str(value), int(value_type)


def _write_user_path(value: str, value_type: int) -> None:
    assert winreg is not None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, ENVIRONMENT_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, value_type, value)
    except OSError as exc:
        raise PathError(t("error.registry_write", error=exc)) from exc


def broadcast_change() -> None:
    try:
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.c_wchar_p(ENVIRONMENT_KEY),
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass


def _entries(raw: str) -> list[str]:
    return [part for part in raw.split(os.pathsep) if part.strip()]


def _same(left: str, right: Path) -> bool:
    expanded = os.path.expandvars(left).strip().rstrip("\\/")
    try:
        return Path(expanded).resolve() == right.resolve()
    except OSError:
        return expanded.lower() == str(right).lower().rstrip("\\/")


def is_on_path(directory: Path) -> bool:
    raw, _ = _read_user_path()
    return any(_same(entry, directory) for entry in _entries(raw))


def ensure_first(directory: Path) -> bool:
    raw, value_type = _read_user_path()
    entries = _entries(raw)
    remaining = [entry for entry in entries if not _same(entry, directory)]

    if entries and remaining and _same(entries[0], directory):
        return False

    _write_user_path(os.pathsep.join([str(directory), *remaining]), value_type)
    broadcast_change()
    return True


def remove(directory: Path) -> bool:
    raw, value_type = _read_user_path()
    entries = _entries(raw)
    remaining = [entry for entry in entries if not _same(entry, directory)]
    if len(remaining) == len(entries):
        return False
    _write_user_path(os.pathsep.join(remaining), value_type)
    broadcast_change()
    return True
