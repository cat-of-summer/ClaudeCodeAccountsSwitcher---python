"""`claude -t telegram ...`: a session that lives in a chat.

The wrapper strips ccas's own flags off the command line and lands here with
the slot already chosen. What is left to decide is which bot and chat the
session belongs to (the flags win over config.json), which directory it runs
in, and whether it polls the bot itself or lets the daemon feed it.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.transport.routing import LaunchOptions, TRANSPORTS
from core import telegram
from core.store import Config
from ui.i18n import t


class TransportError(Exception):
    pass


def resolve_target(config: Config, options: LaunchOptions) -> "ChatTarget":
    from app.transport.session import ChatTarget

    settings = config.telegram
    token = options.token or str(settings.get("token") or "")
    if not telegram.looks_like_token(token):
        raise TransportError(t("tg.no_token"))
    chat = options.chat or int(settings.get("chat") or 0)
    if not chat:
        raise TransportError(t("tg.no_chat"))
    thread = options.thread or int(settings.get("thread") or 0)
    users = [int(user) for user in (settings.get("users") or []) if str(user).strip().lstrip("-").isdigit()]
    return ChatTarget(
        bot=telegram.Bot(token),
        chat=chat,
        thread=thread,
        users=users,
        prefix=str(settings.get("prefix") or ""),
    )


def resolve_cwd(config: Config, options: LaunchOptions) -> Path:
    from app.transport.session import resolve_dir

    if options.cwd:
        target = resolve_dir(options.cwd, Path.cwd(), roots=config.telegram.get("roots") or [])
        if target is None:
            raise TransportError(t("tg.cd_bad", path=options.cwd))
        return target
    return Path.cwd()


def run(config: Config, slot: int, args: list[str], options: LaunchOptions) -> int:
    if options.transport not in TRANSPORTS:
        raise TransportError(t("tg.unknown_transport", transport=options.transport, known=", ".join(TRANSPORTS)))

    from app.transport.session import Session

    target = resolve_target(config, options)
    cwd = resolve_cwd(config, options)
    # The daemon, when it spawned us, says so: it polls, we only listen.
    fed_by_daemon = bool(os.environ.get("CCAS_TELEGRAM_FEED"))
    session = Session(
        config,
        slot=slot,
        target=target,
        cwd=cwd,
        args=args,
        alias=options.name,
        own_poller=not fed_by_daemon,
        tag=os.environ.get("CCAS_TELEGRAM_TAG") or "s",
    )
    return session.run()
