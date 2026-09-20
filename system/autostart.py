"""Start the Telegram daemon when the user logs in.

One entry per platform, all pointing at `ccas daemon run --hidden`: a Run key
on Windows, a systemd user unit on Linux (an XDG autostart file where there
is no systemd), a launchd agent on macOS. Registered and removed together
with the `telegram-daemon` setting, so the setting is the only switch the
user has to know about.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from core import log
from core.store import bin_dir, manager_name

ENTRY_NAME = "ccas-daemon"
LAUNCHD_LABEL = "dev.ccas.daemon"


def daemon_command() -> list[str]:
    return [str(bin_dir() / manager_name()), "daemon", "run", "--hidden"]


def register() -> bool:
    try:
        if os.name == "nt":
            return _register_windows()
        if sys.platform == "darwin":
            return _register_launchd()
        return _register_linux()
    except (OSError, subprocess.SubprocessError) as exc:
        log.write(f"autostart: register failed: {exc}")
        return False


def unregister() -> bool:
    try:
        if os.name == "nt":
            return _unregister_windows()
        if sys.platform == "darwin":
            return _unregister_launchd()
        return _unregister_linux()
    except (OSError, subprocess.SubprocessError) as exc:
        log.write(f"autostart: unregister failed: {exc}")
        return False


def is_registered() -> bool:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            return _read_windows() is not None
        if sys.platform == "darwin":
            return _launchd_plist().exists()
        return _systemd_unit().exists() or _xdg_desktop().exists()
    return False


# -- Windows ---------------------------------------------------------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _read_windows() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, ENTRY_NAME)
            return str(value)
    except FileNotFoundError:
        return None


def _register_windows() -> bool:
    import winreg

    executable, *rest = daemon_command()
    command = f'"{executable}" ' + " ".join(rest)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, command)
    return True


def _unregister_windows() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, ENTRY_NAME)
    except FileNotFoundError:
        return False
    return True


# -- Linux -----------------------------------------------------------------


def _config_home() -> Path:
    override = os.environ.get("XDG_CONFIG_HOME")
    return Path(override) if override else Path.home() / ".config"


def _systemd_unit() -> Path:
    return _config_home() / "systemd" / "user" / f"{ENTRY_NAME}.service"


def _xdg_desktop() -> Path:
    return _config_home() / "autostart" / f"{ENTRY_NAME}.desktop"


def _has_user_systemd() -> bool:
    if not shutil.which("systemctl"):
        return False
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"], capture_output=True, timeout=10
    )
    return probe.returncode in {0, 1}  # "running" / "degraded" both answer


def _register_linux() -> bool:
    command = " ".join(f'"{part}"' if " " in part else part for part in daemon_command())
    if _has_user_systemd():
        unit = _systemd_unit()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\n"
            "Description=ccas Telegram daemon\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={command}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n\n"
            "[Install]\n"
            "WantedBy=default.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{ENTRY_NAME}.service"],
            capture_output=True,
            timeout=30,
        )
        return True

    desktop = _xdg_desktop()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ccas Telegram daemon\n"
        f"Exec={command}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return True


def _unregister_linux() -> bool:
    removed = False
    unit = _systemd_unit()
    if unit.exists():
        if shutil.which("systemctl"):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{ENTRY_NAME}.service"],
                capture_output=True,
                timeout=30,
            )
        unit.unlink()
        removed = True
    desktop = _xdg_desktop()
    if desktop.exists():
        desktop.unlink()
        removed = True
    return removed


# -- macOS -----------------------------------------------------------------


def _launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _register_launchd() -> bool:
    plist = _launchd_plist()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as handle:
        plistlib.dump(
            {
                "Label": LAUNCHD_LABEL,
                "ProgramArguments": daemon_command(),
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
            },
            handle,
        )
    subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, timeout=30)
    return True


def _unregister_launchd() -> bool:
    plist = _launchd_plist()
    if not plist.exists():
        return False
    subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True, timeout=30)
    plist.unlink()
    return True
