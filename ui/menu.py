from __future__ import annotations

import contextlib
import os
import sys

from core import claudecfg
from core.store import Accounts, Config, Slot, creds_file, update_accounts
from ui import usage
from ui.i18n import t

IS_WINDOWS = os.name == "nt"

ESC = "\033["
RESET = f"{ESC}0m"
DIM = f"{ESC}2m"
BOLD = f"{ESC}1m"
GREEN = f"{ESC}32m"
YELLOW = f"{ESC}33m"
CYAN = f"{ESC}36m"

ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11


def enable_ansi() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
    except (AttributeError, OSError):
        pass


def _read_key_windows() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        second = msvcrt.getwch()
        return {"H": "up", "P": "down"}.get(second, "")
    if char == "\r":
        return "enter"
    if char == "\x1b":
        return "esc"
    if char == "\x03":
        raise KeyboardInterrupt
    return char.lower()


def _read_key_posix() -> str:
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        char = sys.stdin.read(1)
        if char == "\x1b":
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not ready:
                return "esc"
            if sys.stdin.read(1) != "[":
                return "esc"
            return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "")
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x03":
            raise KeyboardInterrupt
        return char.lower()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_key() -> str:
    return _read_key_windows() if IS_WINDOWS else _read_key_posix()


def _row(slot: Slot, *, active: bool, selected: bool) -> str:
    marker = "*" if active else " "
    pointer = ">" if selected else " "
    label = slot.alias or slot.email or t("menu.slot_fallback_label", slot=slot.number)

    state = claudecfg.token_state(creds_file(slot.number))
    if state == "missing":
        detail = f"{DIM}{t('menu.slot_no_login')}{RESET}"
    elif state == "stale":
        detail = f"{YELLOW}{t('menu.slot_relogin')}{RESET}"
    else:
        summary = usage.format_usage(slot.usage)
        detail = f"{DIM}{summary}{RESET}" if usage.is_unknown(summary) else summary
        if state == "expired":
            detail = f"{detail}  {DIM}{t('menu.token_self_refresh')}{RESET}"

    body = f"{pointer} {marker} {slot.number}  {label:<28} {detail}"
    return f"{BOLD}{body}{RESET}" if selected else body


def _lines(accounts: Accounts, cursor: int, note: str) -> list[str]:
    slots = accounts.ordered()
    lines = [f"{CYAN}{t('menu.title')}{RESET}"]

    for index, slot in enumerate(slots):
        lines.append(
            _row(slot, active=slot.number == accounts.active, selected=index == cursor)
        )

    add_selected = cursor == len(slots)
    pointer = ">" if add_selected else " "
    add_line = f"{pointer}   +  {t('menu.add_account')}"
    lines.append(f"{BOLD}{add_line}{RESET}" if add_selected else add_line)
    lines.append(f"{DIM}{t('menu.keys')}{RESET}")

    if note:
        lines.append(f"{GREEN}{note}{RESET}")
    return lines


def _draw(lines: list[str], previous_height: int) -> int:
    if previous_height:
        sys.stdout.write(f"{ESC}{previous_height}A")
    for line in lines:
        sys.stdout.write(f"\r{ESC}K{line}\n")
    sys.stdout.flush()
    return len(lines)


def choose(config: Config, accounts: Accounts) -> int | None:
    del config

    slots = accounts.ordered()
    if not slots:
        return accounts.next_free_number()

    enable_ansi()

    cursor = 0
    for index, slot in enumerate(slots):
        if slot.number == accounts.active:
            cursor = index
            break

    note = ""
    height = 0
    try:
        while True:
            height = _draw(_lines(accounts, cursor, note), height)
            note = ""
            key = read_key()

            if key in ("q", "esc"):
                return None

            if key == "up":
                cursor = (cursor - 1) % (len(slots) + 1)
            elif key == "down":
                cursor = (cursor + 1) % (len(slots) + 1)
            elif key == "r":
                height = _draw(_lines(accounts, cursor, t("menu.refreshing")), height)
                fresh = usage.refresh_slots(slots)
                for number, payload in fresh.items():
                    target = accounts.get(number)
                    if target is not None:
                        target.usage = payload
                if fresh:

                    def _store(current: Accounts, fetched=fresh) -> None:
                        for number, payload in fetched.items():
                            current.ensure(number).usage = payload

                    update_accounts(_store)
                note = (
                    t("menu.refreshed", count=len(fresh))
                    if fresh
                    else t("menu.refresh_failed")
                )
            elif key in ("enter", "a"):
                if key == "a" or cursor == len(slots):
                    return accounts.next_free_number()
                return slots[cursor].number
            elif key.isdigit():
                wanted = int(key)
                for index, slot in enumerate(slots):
                    if slot.number == wanted:
                        cursor = index
                        break
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return None
    finally:
        with contextlib.suppress(OSError):
            sys.stdout.flush()
