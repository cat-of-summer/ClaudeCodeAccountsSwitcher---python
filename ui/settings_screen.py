from __future__ import annotations

import sys

from core import settings
from core.settings import Setting, SettingError
from core.store import Config
from ui.i18n import t
from ui.screen import BOLD, CYAN, DIM, GREEN, RESET, YELLOW, Surface, enable_ansi, read_key

INT_STEP = 5
LABEL_WIDTH = 26


def _row(setting: Setting, config: Config, *, selected: bool) -> str:
    pointer = ">" if selected else " "
    value = setting.display(config)
    painted = value if setting.editable_in_screen else f"{DIM}{value}{RESET}"
    body = f"{pointer} {setting.label:<{LABEL_WIDTH}} {CYAN}{painted}{RESET}"
    return f"{BOLD}{body}{RESET}" if selected else body


def _lines(config: Config, cursor: int, note: str) -> list[str]:
    lines = [f"{CYAN}{t('config.title')}{RESET}"]
    for index, setting in enumerate(settings.SETTINGS):
        lines.append(_row(setting, config, selected=index == cursor))

    current = settings.SETTINGS[cursor]
    lines.append("")
    lines.append(f"{DIM}{current.help}{RESET}")
    lines.append(f"{DIM}{t('config.keys')}{RESET}")
    if note:
        lines.append(f"{YELLOW}{note}{RESET}" if note.startswith("!") else f"{GREEN}{note}{RESET}")
    return lines


def _change(setting: Setting, config: Config, step: int) -> Config:
    if setting.kind == "bool":
        return settings.apply(setting, not setting.get(config))
    if setting.kind == "choice":
        return settings.apply(setting, settings.cycle_choice(setting, config, step or 1))
    if setting.kind == "int":
        return settings.apply(setting, settings.step_int(setting, config, step * INT_STEP))
    return config


def edit(config: Config | None = None) -> bool:
    """Toggle settings in place. Returns True if anything was written.

    Every change is saved the moment it is made rather than on exit: this is a
    raw-key screen with no confirmation step, and Ctrl-C out of it must not
    silently discard what the user just set.
    """
    current = Config.load() if config is None else config
    enable_ansi()

    surface = Surface()
    cursor = 0
    note = ""
    changed = False

    try:
        while True:
            surface.paint(_lines(current, cursor, note))
            note = ""
            key = read_key()

            if key in ("q", "esc"):
                return changed

            if key == "up":
                cursor = (cursor - 1) % len(settings.SETTINGS)
                continue
            if key == "down":
                cursor = (cursor + 1) % len(settings.SETTINGS)
                continue

            setting = settings.SETTINGS[cursor]
            step = -1 if key == "left" else 1

            if key in ("enter", "space", "left", "right", "+", "-", "="):
                if not setting.editable_in_screen:
                    # read_key() reads one character and cannot edit a line, so
                    # free-text values are shown but changed through the CLI.
                    note = f"!{t('config.edit_via_cli', key=setting.key)}"
                    continue
                if key in ("+", "="):
                    step = 1
                elif key == "-":
                    step = -1
                try:
                    current = _change(setting, current, step)
                except SettingError as exc:
                    note = f"!{exc}"
                    continue
                changed = True
                note = t("config.saved", key=setting.key, value=setting.display(current))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return changed
    finally:
        sys.stdout.flush()
