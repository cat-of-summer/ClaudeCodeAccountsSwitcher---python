from __future__ import annotations

import sys

from core.store import Accounts, Config, Slot, update_accounts
from ui import usage
from ui.i18n import t
from ui.screen import BOLD, CYAN, DIM, GREEN, RESET, Surface, enable_ansi, read_key


def _row(slot: Slot, *, active: bool, selected: bool) -> str:
    marker = "*" if active else " "
    pointer = ">" if selected else " "
    label = slot.alias or slot.email or t("menu.slot_fallback_label", slot=slot.number)
    detail = usage.describe(slot, colour=True)

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


def refresh_into(accounts: Accounts, slots: list[Slot]) -> int:
    """Ask the API about `slots` and keep the answer, in memory and on disk."""
    fresh = usage.refresh_slots(slots, refresh_timeout=usage.INTERACTIVE_REFRESH_TIMEOUT)
    for number, payload in fresh.items():
        target = accounts.get(number)
        if target is not None:
            target.usage = payload

    if fresh:

        def _store(current: Accounts, fetched: dict = fresh) -> None:
            for number, payload in fetched.items():
                current.ensure(number).usage = payload

        update_accounts(_store)

    return len(fresh)


def choose(config: Config, accounts: Accounts) -> int | None:
    slots = accounts.ordered()
    if not slots:
        return accounts.next_free_number()

    enable_ansi()

    cursor = 0
    for index, slot in enumerate(slots):
        if slot.number == accounts.active:
            cursor = index
            break

    surface = Surface()
    note = ""
    try:
        while True:
            surface.paint(_lines(accounts, cursor, note))
            note = ""
            key = read_key()

            if key in ("q", "esc"):
                return None

            if key == "up":
                cursor = (cursor - 1) % (len(slots) + 1)
            elif key == "down":
                cursor = (cursor + 1) % (len(slots) + 1)
            elif key in ("r", "u"):
                # `u` is one request, `r` is one per slot. Shift-R could not be
                # the pair to `r`: read_key() lowercases everything.
                wanted = (
                    [slots[cursor]] if key == "u" and cursor < len(slots) else slots
                )
                surface.paint(_lines(accounts, cursor, t("menu.refreshing")))
                count = refresh_into(accounts, wanted)
                note = (
                    t("menu.refreshed", count=count) if count else t("menu.refresh_failed")
                )
            elif key == "s":
                from ui import settings_screen

                settings_screen.edit(config)
                surface.reset()
            elif key in ("enter", "a"):
                if key == "a" or cursor == len(slots):
                    return accounts.next_free_number()
                return slots[cursor].number
            elif key.isdigit():
                wanted_number = int(key)
                for index, slot in enumerate(slots):
                    if slot.number == wanted_number:
                        cursor = index
                        break
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return None
    finally:
        sys.stdout.flush()
