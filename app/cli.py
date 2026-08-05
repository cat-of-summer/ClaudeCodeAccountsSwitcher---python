from __future__ import annotations

import argparse
import contextlib
import shlex
import shutil
import sys
from pathlib import Path

from app import installer
from core import claudecfg, detect
from core.store import (
    Accounts,
    Config,
    Slot,
    app_dir,
    bin_dir,
    creds_dir,
    creds_file,
    identity_file,
    is_installed,
    log_file,
    read_json,
    update_accounts,
)
from core.version import __version__
from system import secure
from ui import i18n, usage
from ui.i18n import t

OK = "  ok  "
WARN = " warn "
FAIL = " fail "


def _ask_yes_no(question: str, default: bool) -> bool:
    if not (sys.stdin and sys.stdin.isatty()):
        return default

    suffix = t(
        "common.yes_no_suffix_default_yes" if default else "common.yes_no_suffix_default_no"
    )
    positive = i18n.answers("common.yes_answers")
    negative = i18n.answers("common.no_answers")

    while True:
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not answer:
            return default
        if answer in positive:
            return True
        if answer in negative:
            return False


def _require_installed() -> tuple[Config, Accounts]:
    if not is_installed():
        raise SystemExit(t("error.not_installed"))
    return Config.load(), Accounts.load()


def _resolve_target(accounts: Accounts, token: str) -> Slot:
    probe = token.strip()

    if probe.isdigit():
        slot = accounts.get(int(probe))
        if slot is None:
            raise SystemExit(t("error.unknown_slot", slot=probe))
        return slot

    alias = probe[1:] if probe.startswith("@") else probe
    by_alias = accounts.by_alias(alias)
    if by_alias is not None:
        return by_alias

    matches = accounts.by_email(probe)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = ", ".join(f"{slot.number} ({slot.email})" for slot in matches)
        raise SystemExit(t("error.ambiguous_target", target=probe, matches=listed))

    raise SystemExit(t("error.no_such_account", target=probe))


def _refresh_usage(accounts: Accounts, slots: list[Slot]) -> tuple[int, int]:
    """Fetch limits for `slots`; returns (refreshed, failed)."""
    attempted = len(usage.refreshable(slots))
    if not attempted:
        return 0, 0

    fresh = usage.refresh_slots(slots)
    for number, payload in fresh.items():
        target = accounts.get(number)
        if target is not None:
            target.usage = payload

    if fresh:

        def _store(current: Accounts) -> None:
            for number, payload in fresh.items():
                current.ensure(number).usage = payload

        update_accounts(_store)

    return len(fresh), attempted - len(fresh)


def _autorefresh(accounts: Accounts, slots: list[Slot], args: argparse.Namespace) -> int:
    """Keep `ccas list` honest without making every call wait on the network.

    Limits used to move only when someone remembered `--refresh`, so the list
    showed either "unknown" or a frozen snapshot from hours ago.
    """
    if getattr(args, "cached", False):
        return 0

    if getattr(args, "refresh", False):
        stale = slots
    else:
        stale = [slot for slot in slots if usage.is_stale(slot.usage)]

    if not stale:
        return 0

    _, failed = _refresh_usage(accounts, stale)
    return failed


def cmd_install(args: argparse.Namespace) -> int:
    if args.skip_permissions:
        skip_permissions = True
    elif args.no_skip_permissions:
        skip_permissions = False
    else:
        skip_permissions = None

    ask = None if args.yes else _ask_yes_no

    try:
        installer.install(
            claude_path=args.claude_path,
            skip_permissions=skip_permissions,
            language=getattr(args, "lang", None),
            ask=ask,
        )
    except (installer.InstallError, detect.DetectionError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not is_installed() and not app_dir().exists():
        print(t("uninstall.not_installed"))
        return 0

    if args.purge and not args.yes:
        if not _ask_yes_no(t("uninstall.confirm_purge"), False):
            print(t("common.cancelled"))
            return 1

    installer.uninstall(purge=args.purge)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _, accounts = _require_installed()
    slots = accounts.ordered()
    if not slots:
        print(t("cli.no_slots_hint"))
        return 0

    failed = _autorefresh(accounts, slots, args)

    for slot in slots:
        marker = "*" if slot.number == accounts.active else " "
        state = claudecfg.token_state(creds_file(slot.number))
        label = slot.alias or slot.email or t("menu.slot_fallback_label", slot=slot.number)
        detail = usage.format_token_state(state) or usage.format_usage(slot.usage)
        email = f"  {slot.email}" if slot.alias and slot.email else ""
        print(f"{marker} [{slot.number}] {label}{email}   {detail}")

    if failed:
        print(t("usage.refresh_partial", count=failed))
    return 0


def cmd_menu(args: argparse.Namespace) -> int:
    del args
    config, accounts = _require_installed()
    if not accounts.slots:
        print(t("cli.no_slots_hint"))
        return 0

    from ui import menu

    chosen = menu.choose(config, accounts)
    if chosen is None:
        return 0

    target = accounts.get(chosen)
    if target is None or not target.has_credentials():
        # "+ add an account" picks a free number; making it active would leave a
        # bare `claude` pointing at a slot nobody has signed into.
        print(t("cli.free_slot", slot=chosen))
        print(t("cli.free_slot_hint", slot=chosen))
        return 0

    update_accounts(lambda current: setattr(current, "active", chosen))
    print(t("cli.active_slot", slot=chosen, label=target.label))
    print(t("cli.run_claude_hint"))
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    _, accounts = _require_installed()
    slot = _resolve_target(accounts, args.target)

    def _activate(current: Accounts) -> None:
        current.ensure(slot.number)
        current.active = slot.number

    update_accounts(_activate)
    print(t("cli.active_slot", slot=slot.number, label=slot.label))
    print(t("cli.run_claude_hint"))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    del args
    _, accounts = _require_installed()
    number = accounts.next_free_number()
    print(t("cli.free_slot", slot=number))
    print(t("cli.free_slot_hint", slot=number))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    _, accounts = _require_installed()
    slot = _resolve_target(accounts, args.target)

    if not args.yes:
        confirmed = _ask_yes_no(
            t("cli.confirm_remove", slot=slot.number, label=slot.label), False
        )
        if not confirmed:
            print(t("common.cancelled"))
            return 1

    shutil.rmtree(creds_dir(slot.number), ignore_errors=True)
    with contextlib.suppress(OSError):
        identity_file(slot.number).unlink()

    update_accounts(lambda current: current.remove(slot.number))
    print(t("cli.slot_removed", slot=slot.number))
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    _, accounts = _require_installed()
    slot = _resolve_target(accounts, args.target)
    alias = args.alias.strip()

    if alias.isdigit():
        raise SystemExit(t("error.alias_numeric"))
    if accounts.alias_taken(alias, excluding=slot.number):
        raise SystemExit(t("error.alias_taken", alias=alias))

    def _rename(current: Accounts) -> None:
        current.ensure(slot.number).alias = alias

    update_accounts(_rename)
    print(t("cli.slot_renamed", slot=slot.number, alias=alias))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    _, accounts = _require_installed()
    slots = accounts.ordered()
    if not slots:
        print(t("cli.no_slots"))
        return 0

    failed = 0
    if not args.cached:
        _, failed = _refresh_usage(accounts, slots)

    for slot in slots:
        marker = "*" if slot.number == accounts.active else " "
        print(f"{marker} [{slot.number}] {slot.label:<34} {usage.format_usage(slot.usage)}")

    if failed:
        print(t("usage.refresh_partial", count=failed))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    del args
    problems = 0

    def report(status: str, message: str) -> None:
        print(f"[{status}] {message}")

    if not is_installed():
        report(FAIL, t("error.not_installed"))
        return 1

    config = Config.load()
    accounts = Accounts.load()

    report(OK, t("doctor.version", installed=config.version, binary=__version__))
    if config.version != __version__:
        report(WARN, t("doctor.version_mismatch"))

    entry_present = installer.path_entry_present()
    if entry_present:
        report(OK, t("doctor.shim_dir_ok", path=bin_dir()))
    else:
        report(FAIL, t("doctor.shim_dir_missing"))
        problems += 1

    resolved = shutil.which("claude")
    if resolved and detect.is_our_shim(resolved):
        report(OK, t("doctor.intercept_ok", path=resolved))
    elif entry_present:
        state = (
            t("doctor.intercept_state_seen", path=resolved)
            if resolved
            else t("doctor.intercept_state_unseen")
        )
        report(WARN, t("doctor.intercept_pending", state=state))
    elif resolved:
        report(FAIL, t("doctor.intercept_bypassed", path=resolved))
        problems += 1
    else:
        report(FAIL, t("doctor.claude_missing_path"))
        problems += 1

    real = Path(config.real_claude_path) if config.real_claude_path else None
    if real and real.is_file():
        version = detect.claude_version(real) or t("doctor.claude_version_unknown")
        report(OK, t("doctor.real_claude_ok", path=real, version=version))
    else:
        report(FAIL, t("doctor.real_claude_missing", path=real))
        problems += 1
        if installer.refresh_claude_path(config):
            report(OK, t("doctor.real_claude_refreshed", path=config.real_claude_path))
            problems -= 1

    if config.cred_mode == detect.CRED_MODE_ENV:
        report(OK, t("doctor.cred_mode_env"))
    else:
        report(WARN, t("doctor.cred_mode_copy"))

    if not accounts.slots:
        report(WARN, t("doctor.no_slots"))

    for slot in accounts.ordered():
        path = creds_file(slot.number)
        state = claudecfg.token_state(path)
        if state == "missing":
            report(WARN, t("doctor.slot_no_creds", slot=slot.number))
        elif state == "stale":
            report(WARN, t("doctor.slot_stale", slot=slot.number, label=slot.label))
        else:
            report(OK, t("doctor.slot_ok", slot=slot.number, label=slot.label, state=state))
        if secure.is_world_readable(path):
            report(FAIL, t("doctor.slot_world_readable", slot=slot.number))
            problems += 1

    targets = claudecfg.existing_config_targets()
    accounts_seen: dict[str, list[str]] = {}
    for target in targets:
        raw = read_json(target)
        if not isinstance(raw, dict):
            continue
        oauth = raw.get("oauthAccount")
        email = oauth.get("emailAddress") if isinstance(oauth, dict) else None
        accounts_seen.setdefault(str(email), []).append(str(target))

    report(OK, t("doctor.configs_found", count=len(targets)))
    if len(accounts_seen) > 1:
        details = "; ".join(
            f"{email} -> {', '.join(paths)}" for email, paths in accounts_seen.items()
        )
        report(WARN, t("doctor.configs_drift", details=details))

    print()
    print(t("doctor.directory", path=app_dir()))
    print(t("doctor.log", path=log_file()))
    return 1 if problems else 0


def _add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-path", help=t("cli.help.claude_path"))
    parser.add_argument(
        "--skip-permissions", action="store_true", help=t("cli.help.skip_permissions")
    )
    parser.add_argument(
        "--no-skip-permissions",
        action="store_true",
        help=t("cli.help.no_skip_permissions"),
    )
    parser.add_argument("-y", "--yes", action="store_true", help=t("cli.help.yes"))
    parser.add_argument(
        "--lang",
        choices=i18n.available_languages(),
        help=t("cli.help.lang", languages="/".join(i18n.available_languages())),
    )
    parser.set_defaults(func=cmd_install)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccas", description=t("cli.description"))
    parser.add_argument("--version", action="version", version=f"ccas {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    _add_install_arguments(subparsers.add_parser("install", help=t("cli.help.install")))
    _add_install_arguments(
        subparsers.add_parser("reinstall", help=t("cli.help.reinstall"))
    )

    uninstall_parser = subparsers.add_parser("uninstall", help=t("cli.help.uninstall"))
    uninstall_parser.add_argument("--purge", action="store_true", help=t("cli.help.purge"))
    uninstall_parser.add_argument("-y", "--yes", action="store_true", help=t("cli.help.yes"))
    uninstall_parser.set_defaults(func=cmd_uninstall)

    list_parser = subparsers.add_parser("list", help=t("cli.help.list"))
    list_parser.add_argument("--refresh", action="store_true", help=t("cli.help.refresh"))
    list_parser.add_argument(
        "--cached", action="store_true", help=t("cli.help.cached_list")
    )
    list_parser.set_defaults(func=cmd_list)

    menu_parser = subparsers.add_parser("menu", help=t("cli.help.menu"))
    menu_parser.set_defaults(func=cmd_menu)

    switch_parser = subparsers.add_parser("switch", help=t("cli.help.switch"))
    switch_parser.add_argument("target", help=t("cli.help.target"))
    switch_parser.set_defaults(func=cmd_switch)

    add_parser = subparsers.add_parser("add", help=t("cli.help.add"))
    add_parser.set_defaults(func=cmd_add)

    remove_parser = subparsers.add_parser("remove", help=t("cli.help.remove"))
    remove_parser.add_argument("target", help=t("cli.help.target"))
    remove_parser.add_argument("-y", "--yes", action="store_true", help=t("cli.help.yes"))
    remove_parser.set_defaults(func=cmd_remove)

    rename_parser = subparsers.add_parser("rename", help=t("cli.help.rename"))
    rename_parser.add_argument("target", help=t("cli.help.target"))
    rename_parser.add_argument("alias")
    rename_parser.set_defaults(func=cmd_rename)

    usage_parser = subparsers.add_parser("usage", help=t("cli.help.usage"))
    usage_parser.add_argument("--cached", action="store_true", help=t("cli.help.cached"))
    usage_parser.set_defaults(func=cmd_usage)

    doctor_parser = subparsers.add_parser("doctor", help=t("cli.help.doctor"))
    doctor_parser.set_defaults(func=cmd_doctor)

    return parser


def _interactive() -> bool:
    return bool(
        sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()
    )


def _dispatch(parser: argparse.ArgumentParser, tokens: list[str]) -> None:
    try:
        args = parser.parse_args(tokens)
    except SystemExit:
        # argparse calls sys.exit() on bad input or on --help/--version;
        # keep the shell alive instead of letting it terminate the process.
        return

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return

    try:
        func(args)
    except SystemExit as exc:
        # Commands raise SystemExit(message) to report errors; surface the text.
        if isinstance(exc.code, str):
            print(exc.code)
    except KeyboardInterrupt:
        print()


def run_shell(parser: argparse.ArgumentParser) -> int:
    print(t("cli.shell.banner", version=__version__))
    if is_installed():
        cmd_list(argparse.Namespace(refresh=False, cached=False))
    else:
        print(t("cli.not_installed_bare"))
    print()
    print(t("cli.shell.hint"))

    while True:
        try:
            line = input("ccas> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line.lower() in {"exit", "quit", "q"}:
            return 0

        try:
            tokens = shlex.split(line)
        except ValueError:
            print(t("cli.shell.unknown"))
            continue

        _dispatch(parser, tokens)
        print()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    installer.apply_pending_upgrade()

    parser = build_parser()
    args = parser.parse_args(argv[1:])

    if not getattr(args, "func", None):
        if _interactive():
            return run_shell(parser)
        if not is_installed():
            print(t("cli.not_installed_bare") + "\n")
            parser.print_help()
            return 1
        return cmd_list(argparse.Namespace(refresh=False, cached=False))

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
