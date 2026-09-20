"""`claude -t telegram ...`: a profile, running in a chat.

The wrapper strips ccas's own flags off the command line and lands here with
the slot already chosen. What is left to decide is which profile this is
(`-n name`, else `default`), where it runs and which chats it serves -- the
profile answers all three, and the flags override it for this one launch.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.transport import profiles as profiles_module
from app.transport.profiles import DEFAULT_PROFILE, Profile
from app.transport.routing import TRANSPORTS, LaunchOptions
from core import telegram
from core.store import Config
from ui.i18n import t


class TransportError(Exception):
    pass


def resolve_profile(config: Config, options: LaunchOptions) -> Profile:
    known = profiles_module.load(config)
    name = options.name or os.environ.get("CCAS_TELEGRAM_PROFILE") or DEFAULT_PROFILE
    profile = known.get(name)
    if profile is None:
        raise TransportError(
            t("tg.no_profile", name=name, names=", ".join(sorted(known)))
        )
    return profile


def resolve_bot(config: Config, options: LaunchOptions) -> telegram.Bot:
    token = options.token or str(config.telegram.get("token") or "")
    if not telegram.looks_like_token(token):
        raise TransportError(t("tg.no_token"))
    return telegram.Bot(token)


def resolve_cwd(config: Config, profile: Profile, options: LaunchOptions) -> Path:
    from app.transport.conversation import resolve_dir

    if options.cwd:
        target = resolve_dir(options.cwd, Path.cwd(), roots=config.telegram.get("roots") or [])
        if target is None:
            raise TransportError(t("tg.cd_bad", path=options.cwd))
        return target
    return profile.resolve_cwd(config)


def run(
    config: Config,
    slot: int,
    args: list[str],
    options: LaunchOptions,
    *,
    slot_explicit: bool = False,
) -> int:
    if options.transport not in TRANSPORTS:
        raise TransportError(
            t("tg.unknown_transport", transport=options.transport, known=", ".join(TRANSPORTS))
        )

    from app.transport.transport import Transport

    profile = resolve_profile(config, options)
    bot = resolve_bot(config, options)
    cwd = resolve_cwd(config, profile, options)
    if options.chat and not profile.open_to(options.chat, options.thread):
        raise TransportError(
            t(
                "daemon.profile_elsewhere",
                name=profile.name,
                chats=", ".join(profiles_module.format_chat(ref) for ref in profile.chats),
            )
        )

    # A slot typed on the command line beats the profile; the profile beats
    # whatever the wrapper would have resumed by default.
    chosen = slot if slot_explicit else (profile.slot or slot)

    transport = Transport(
        config,
        profile,
        bot=bot,
        slot=chosen,
        cwd=cwd,
        args=args,
        chat=options.chat,
        thread=options.thread,
        tag=os.environ.get("CCAS_TELEGRAM_TAG") or "s",
    )
    return transport.run()
