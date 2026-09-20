from __future__ import annotations

import argparse
import contextlib
import shlex
import shutil
import sys
from pathlib import Path

from app import installer
from core import claudecfg, detect, hookbus, settings
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
    migrate_config,
    read_json,
    refresh_resume_prompt,
    update_accounts,
)
from core.version import __version__
from system import console, secure
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


def _name_known_slots(accounts: Accounts) -> None:
    """Fill in names the store lost but the identity files still hold."""
    if accounts.backfill_identity():
        update_accounts(lambda current: current.backfill_identity())


def _start_claude(config: Config, slot: Slot, args: list[str]) -> int:
    """Hand the terminal to claude on `slot`, in this very process.

    Not `os.execv`: the wrapper's supervision -- the transcript watch, the
    switch on a spent quota -- lives in `run_slot`, and running it here means a
    session started from the menu behaves exactly like one started by typing
    `claude N`.
    """
    from app import wrapper

    update_accounts(lambda current: setattr(current, "active", slot.number))
    print(t("cli.active_slot", slot=slot.number, label=slot.label))
    try:
        return wrapper.run_slot(config, slot.number, args)
    except wrapper.WrapperError as exc:
        raise SystemExit(str(exc)) from exc


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

    fresh = usage.refresh_slots(
        slots, refresh_timeout=usage.INTERACTIVE_REFRESH_TIMEOUT
    )
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
        stale = [
            slot
            for slot in slots
            if usage.needs_network(slot, active=slot.number == accounts.active)
        ]

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
            ask_defaults=getattr(args, "reinstall", False),
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
    _name_known_slots(accounts)
    slots = accounts.ordered()
    if not slots:
        print(t("cli.no_slots_hint"))
        return 0

    failed = _autorefresh(accounts, slots, args)

    for slot in slots:
        marker = "*" if slot.number == accounts.active else " "
        label = slot.alias or slot.email or t("menu.slot_fallback_label", slot=slot.number)
        email = f"  {slot.email}" if slot.alias and slot.email else ""
        print(f"{marker} [{slot.number}] {label}{email}   {usage.describe(slot)}")

    if failed:
        print(t("usage.refresh_partial", count=failed))
    return 0


def cmd_menu(args: argparse.Namespace) -> int:
    del args
    config, accounts = _require_installed()
    if not accounts.slots:
        print(t("cli.no_slots_hint"))
        return 0

    _name_known_slots(accounts)
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

    return _start_claude(config, target, [])


def cmd_run(args: argparse.Namespace) -> int:
    config, accounts = _require_installed()
    slot = _resolve_target(accounts, args.target)
    if not slot.has_credentials():
        print(t("cli.free_slot", slot=slot.number))
        print(t("cli.free_slot_hint", slot=slot.number))
        return 0
    return _start_claude(config, slot, list(getattr(args, "claude_args", None) or []))


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
    _name_known_slots(accounts)
    slots = accounts.ordered()
    if not slots:
        print(t("cli.no_slots"))
        return 0

    failed = 0
    if not args.cached:
        _, failed = _refresh_usage(accounts, slots)

    for slot in slots:
        marker = "*" if slot.number == accounts.active else " "
        print(f"{marker} [{slot.number}] {slot.label:<34} {usage.describe(slot)}")

    if failed:
        print(t("usage.refresh_partial", count=failed))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    _require_installed()

    if args.key is None:
        if args.list or not _interactive():
            _print_settings()
            return 0

        from ui import settings_screen

        settings_screen.edit()
        _print_settings()
        return 0

    try:
        setting = settings.find(args.key)
        if args.value is None:
            print(f"{setting.key} = {setting.display(Config.load())}")
            return 0

        config = settings.apply(setting, settings.parse(setting, args.value))
    except settings.SettingError as exc:
        raise SystemExit(str(exc)) from exc

    print(t("config.saved", key=setting.key, value=setting.display(config)))
    return 0


def _print_settings() -> None:
    config = Config.load()
    width = max(len(setting.key) for setting in settings.SETTINGS)
    for setting in settings.SETTINGS:
        print(f"{setting.key:<{width}}  {setting.display(config)}")
    print()
    print(t("config.hint"))


def cmd_hooks(args: argparse.Namespace) -> int:
    """External handlers for claude's hook events, kept in config.json."""
    _require_installed()
    action = getattr(args, "hooks_action", None) or "list"

    if action == "add":
        if args.event != "*" and not all(
            name in hookbus.HOOK_EVENTS for name in args.event.split("|")
        ):
            raise SystemExit(
                t("hooks.unknown_event", event=args.event, events=", ".join(hookbus.HOOK_EVENTS))
            )
        handler = hookbus.Handler(
            event=args.event,
            command=args.command,
            matcher=args.matcher or "",
            sync=bool(args.sync),
            timeout=float(args.timeout) if args.timeout else hookbus.HANDLER_TIMEOUT_SECONDS,
        )
        config = Config.load()
        config.hooks = [*config.hooks, handler.to_dict()]
        config.save()
        print(t("hooks.added", index=len(config.hooks), event=handler.event))
        return 0

    if action == "remove":
        config = Config.load()
        index = int(args.index)
        if not 1 <= index <= len(config.hooks):
            raise SystemExit(t("hooks.no_such", index=index))
        removed = config.hooks.pop(index - 1)
        config.save()
        print(t("hooks.removed", index=index, event=removed.get("event", "?")))
        return 0

    config = Config.load()
    handlers = hookbus.parse_handlers(config.hooks)
    if not handlers:
        print(t("hooks.none"))
        return 0
    for index, handler in enumerate(handlers, start=1):
        mode = t("hooks.mode_sync") if handler.sync else t("hooks.mode_async")
        matcher = f" [{handler.matcher}]" if handler.matcher else ""
        print(f"{index:>2}. {handler.event}{matcher}  {mode}  {handler.command}")
    print()
    print(t("hooks.bus_state", state=t("config.value_on") if config.hooks_bus else t("config.value_off")))
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    _require_installed()
    from app import daemon

    action = getattr(args, "daemon_action", None) or "status"
    if action == "run":
        return daemon.Daemon(Config.load()).run(hidden=bool(getattr(args, "hidden", False)))
    if action == "start":
        if daemon.read_daemon() is not None:
            print(t("daemon.status_up_short"))
            return 0
        if not daemon.configured(Config.load()):
            raise SystemExit(t("daemon.not_configured"))
        if not daemon.spawn_detached():
            raise SystemExit(t("daemon.start_failed"))
        print(t("daemon.started"))
        return 0
    if action == "stop":
        if daemon.read_daemon() is None:
            print(t("daemon.status_down"))
            return 0
        print(t("daemon.stopped") if daemon.request_stop() else t("daemon.stop_failed"))
        return 0
    for line in daemon.status_text(Config.load()):
        print(line)
    return 0


def _profile_flag(raw: str | None, current: bool, key: str) -> bool:
    """An on/off flag that keeps its value when the option is not given."""
    if raw is None:
        return current
    return settings.to_bool(raw, key=key)


def cmd_profile(args: argparse.Namespace) -> int:
    """Profiles: what the transport listens to, where it runs, who may talk.

    `set` edits as well as creates, so it is also how the permanent
    `default` profile is configured.
    """
    _require_installed()
    from app import daemon
    from app.transport import profiles as profiles_module
    from app.transport.routing import tokenize, valid_alias

    action = getattr(args, "profile_action", None) or "list"
    config = Config.load()
    known = profiles_module.load(config)

    if action == "set":
        name = args.name.strip()
        if name != profiles_module.DEFAULT_PROFILE and not valid_alias(name):
            raise SystemExit(t("profile.bad_name", name=name))

        current = known.get(name) or profiles_module.Profile(name=name)
        cwd = current.cwd
        if args.cwd is not None:
            candidate = Path(args.cwd).expanduser()
            if not candidate.is_dir():
                raise SystemExit(t("config.bad_dir", path=candidate))
            cwd = str(candidate)

        chats = current.chats
        if args.chat is not None:
            parsed = [profiles_module.parse_chat(entry) for entry in args.chat]
            bad = [entry for entry, ref in zip(args.chat, parsed) if ref is None]
            if bad:
                raise SystemExit(t("profile.bad_chat", chat=", ".join(bad)))
            chats = tuple(ref for ref in parsed if ref is not None)

        mode = current.mode
        if args.mode is not None:
            from app.autoswitch import PERMISSION_MODES

            mode = args.mode.strip()
            if mode and mode not in PERMISSION_MODES:
                raise SystemExit(
                    t("profile.bad_mode", mode=mode, modes=", ".join(sorted(PERMISSION_MODES)))
                )

        users = current.users
        if args.users is not None:
            if not all(part.strip().lstrip("-").isdigit() for part in args.users):
                raise SystemExit(t("profile.bad_user", user=", ".join(args.users)))
            users = tuple(int(part) for part in args.users)

        try:
            profile = profiles_module.Profile(
                name=name,
                chats=chats,
                cwd=cwd,
                slot=int(args.slot) if args.slot is not None else current.slot,
                daemon=_profile_flag(args.daemon, current.daemon, "daemon"),
                multi=_profile_flag(args.multi, current.multi, "multi"),
                tech=_profile_flag(args.tech, current.tech, "tech"),
                expanded=_profile_flag(args.expanded, current.expanded, "expanded"),
                mode=mode,
                users=users,
                args=tuple(tokenize(args.claude_args)) if args.claude_args is not None else current.args,
            )
        except settings.SettingError as exc:
            raise SystemExit(str(exc)) from exc
        profiles_module.save(profile)
        daemon.reconcile_autostart(Config.load())
        print(t("profile.saved", name=name))
        print(f"  {profiles_module.describe(profile)}")
        return 0

    if action == "remove":
        name = args.name.strip()
        if name not in known:
            raise SystemExit(t("profile.no_such", name=name, names=", ".join(sorted(known))))
        profiles_module.remove(name)
        daemon.reconcile_autostart(Config.load())
        print(
            t("profile.reset", name=name)
            if name == profiles_module.DEFAULT_PROFILE
            else t("profile.removed", name=name)
        )
        return 0

    if action == "show":
        name = args.name.strip()
        profile = known.get(name)
        if profile is None:
            raise SystemExit(t("profile.no_such", name=name, names=", ".join(sorted(known))))
        print(f"{name}  {profiles_module.describe(profile)}")
        print(t("profile.runs_in", path=profile.resolve_cwd(config)))
        return 0

    width = max(len(name) for name in known)
    for name, profile in sorted(known.items()):
        print(f"{name:<{width}}  {profiles_module.describe(profile)}")
    print()
    print(t("profile.hint"))
    return 0


def cmd_telegram(args: argparse.Namespace) -> int:
    """`ccas telegram test`: prove the token and the chat before going live."""
    _require_installed()
    from core import telegram

    config = Config.load()
    token = str(config.telegram.get("token") or "")
    chat = int(config.telegram.get("chat") or 0)
    if not telegram.looks_like_token(token):
        raise SystemExit(t("tg.no_token"))
    bot = telegram.Bot(token)
    try:
        me = bot.get_me()
        print(t("telegram.test_bot", username=me.get("username") or "?", id=me.get("id")))
        if chat:
            bot.send_message(chat, t("telegram.test_message"), thread_id=int(config.telegram.get("thread") or 0))
            print(t("telegram.test_sent", chat=chat))
        else:
            print(t("tg.no_chat"))
    except (telegram.TelegramError, telegram.Unreachable) as exc:
        raise SystemExit(t("telegram.test_failed", error=exc)) from exc
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

    waiting = installer.pending_upgrade()
    if waiting:
        report(WARN, t("doctor.upgrade_pending", names=", ".join(path.name for path in waiting)))
        problems += 1

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

    if config.auto_switch.get("enabled"):
        report(
            OK,
            t(
                "doctor.auto_switch_on",
                strategy=config.auto_switch.get("strategy"),
                threshold=config.auto_switch.get("threshold"),
            ),
        )
    else:
        report(OK, t("doctor.auto_switch_off"))

    report(
        OK if config.hooks_bus else WARN,
        t("doctor.hooks_bus_on", handlers=len(config.hooks)) if config.hooks_bus else t("doctor.hooks_bus_off"),
    )

    from app import daemon
    from app.transport import profiles as profiles_module
    from core import telegram
    from system import autostart

    if daemon.configured(config):
        token = str(config.telegram.get("token") or "")
        known = profiles_module.load(config)
        watched = sorted(name for name, profile in known.items() if profile.daemon)
        report(OK, t("doctor.telegram_configured", bot=telegram.bot_id(token), profiles=len(known)))
        for name in sorted(known):
            report(OK, f"  {name}  {profiles_module.describe(known[name])}")

        record = daemon.read_daemon()
        if watched:
            if record is None:
                report(WARN, t("doctor.daemon_not_running", profiles=", ".join(watched)))
                problems += 1
            else:
                report(OK, t("doctor.daemon_running", pid=record.get("pid"), profiles=", ".join(watched)))
            if not autostart.is_registered():
                report(WARN, t("doctor.autostart_missing"))
        elif record is not None:
            report(OK, t("doctor.daemon_running", pid=record.get("pid"), profiles="—"))
        else:
            report(OK, t("doctor.daemon_off"))
    else:
        report(OK, t("doctor.telegram_unset"))

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


def _add_install_arguments(
    parser: argparse.ArgumentParser, *, reinstall: bool = False
) -> None:
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
    parser.set_defaults(func=cmd_install, reinstall=reinstall)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccas", description=t("cli.description"))
    parser.add_argument("--version", action="version", version=f"ccas {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    _add_install_arguments(subparsers.add_parser("install", help=t("cli.help.install")))
    _add_install_arguments(
        subparsers.add_parser("reinstall", help=t("cli.help.reinstall")), reinstall=True
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

    run_parser = subparsers.add_parser("run", help=t("cli.help.run"))
    run_parser.add_argument("target", help=t("cli.help.target"))
    run_parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=cmd_run)

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

    config_parser = subparsers.add_parser("config", help=t("cli.help.config"))
    config_parser.add_argument("key", nargs="?", help=t("cli.help.config_key"))
    config_parser.add_argument("value", nargs="?", help=t("cli.help.config_value"))
    config_parser.add_argument(
        "--list", action="store_true", help=t("cli.help.config_list")
    )
    config_parser.set_defaults(func=cmd_config)

    doctor_parser = subparsers.add_parser("doctor", help=t("cli.help.doctor"))
    doctor_parser.set_defaults(func=cmd_doctor)

    hooks_parser = subparsers.add_parser("hooks", help=t("cli.help.hooks"))
    hooks_parser.set_defaults(func=cmd_hooks)
    hooks_sub = hooks_parser.add_subparsers(dest="hooks_action")
    hooks_sub.add_parser("list", help=t("cli.help.hooks_list"))
    hooks_add = hooks_sub.add_parser("add", help=t("cli.help.hooks_add"))
    hooks_add.add_argument("event", help=t("cli.help.hooks_event"))
    hooks_add.add_argument("command", help=t("cli.help.hooks_command"))
    hooks_add.add_argument("--matcher", help=t("cli.help.hooks_matcher"))
    hooks_add.add_argument("--sync", action="store_true", help=t("cli.help.hooks_sync"))
    hooks_add.add_argument("--timeout", type=float, help=t("cli.help.hooks_timeout"))
    hooks_remove = hooks_sub.add_parser("remove", help=t("cli.help.hooks_remove"))
    hooks_remove.add_argument("index", type=int)

    daemon_parser = subparsers.add_parser("daemon", help=t("cli.help.daemon"))
    daemon_parser.set_defaults(func=cmd_daemon)
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_action")
    daemon_sub.add_parser("status", help=t("cli.help.daemon_status"))
    daemon_sub.add_parser("start", help=t("cli.help.daemon_start"))
    daemon_sub.add_parser("stop", help=t("cli.help.daemon_stop"))
    daemon_run = daemon_sub.add_parser("run", help=t("cli.help.daemon_run"))
    daemon_run.add_argument("--hidden", action="store_true", help=t("cli.help.daemon_hidden"))

    profile_parser = subparsers.add_parser("profile", help=t("cli.help.profile"))
    profile_parser.set_defaults(func=cmd_profile)
    profile_sub = profile_parser.add_subparsers(dest="profile_action")
    profile_sub.add_parser("list", help=t("cli.help.profile_list"))
    profile_show = profile_sub.add_parser("show", help=t("cli.help.profile_show"))
    profile_show.add_argument("name")
    profile_set = profile_sub.add_parser("set", help=t("cli.help.profile_set"))
    profile_set.add_argument("name")
    profile_set.add_argument("--cwd", help=t("cli.help.profile_cwd"))
    profile_set.add_argument("--chat", action="append", help=t("cli.help.profile_chat"))
    profile_set.add_argument("--slot", type=int, help=t("cli.help.profile_slot"))
    profile_set.add_argument("--daemon", help=t("cli.help.profile_daemon"))
    profile_set.add_argument("--multi", help=t("cli.help.profile_multi"))
    profile_set.add_argument("--tech", help=t("cli.help.profile_tech"))
    profile_set.add_argument("--expanded", help=t("cli.help.profile_expanded"))
    profile_set.add_argument("--mode", help=t("cli.help.profile_mode"))
    profile_set.add_argument("--users", nargs="*", help=t("cli.help.profile_users"))
    profile_set.add_argument("--args", dest="claude_args", help=t("cli.help.profile_args"))
    profile_remove = profile_sub.add_parser("remove", help=t("cli.help.profile_remove"))
    profile_remove.add_argument("name")

    telegram_parser = subparsers.add_parser("telegram", help=t("cli.help.telegram"))
    telegram_parser.set_defaults(func=cmd_telegram)
    telegram_sub = telegram_parser.add_subparsers(dest="telegram_action")
    telegram_sub.add_parser("test", help=t("cli.help.telegram_test"))

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
        # From the store, instantly: the network is asked on `r`, not before
        # the prompt has even appeared.
        cmd_list(argparse.Namespace(refresh=False, cached=True))
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

        # One-letter aliases for the two things you come back to this prompt
        # for. `l --refresh` works too, but nobody types it twice.
        shortcut = line.lower()
        if shortcut in {"exit", "quit", "q"}:
            return 0
        if shortcut in {"r", "refresh"}:
            _dispatch(parser, ["list", "--refresh"])
            print()
            continue
        if shortcut in {"l", "ls"}:
            _dispatch(parser, ["list"])
            print()
            continue
        if shortcut in {"s", "settings"}:
            _dispatch(parser, ["config"])
            print()
            continue
        if shortcut.isdigit() or shortcut.startswith("go "):
            target = shortcut[3:].strip() if shortcut.startswith("go ") else shortcut
            _dispatch(parser, ["run", target])
            print()
            continue

        try:
            tokens = shlex.split(line)
        except ValueError:
            print(t("cli.shell.unknown"))
            continue

        _dispatch(parser, tokens)
        print()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # A claude killed in this window earlier leaves the console in raw mode:
    # the menu would paint and then take no input.
    console.repair()
    installer.apply_pending_upgrade()
    migrate_config()
    if is_installed():
        refresh_resume_prompt()

    parser = build_parser()
    args = parser.parse_args(argv[1:])

    if is_installed() and getattr(args, "command", None) != "daemon":
        config = Config.load()
        if config.telegram.get("daemon"):
            from app import daemon

            daemon.ensure_running(config)

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
