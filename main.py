from __future__ import annotations

import sys
from pathlib import Path

WRAPPER_NAMES = {"claude"}


def _init_language() -> None:
    from ui import i18n

    language = ""
    try:
        from core.store import Config, is_installed

        if is_installed():
            language = Config.load().language
    except (OSError, ValueError):
        language = ""

    i18n.set_language(language or None)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _init_language()

    invoked_as = Path(argv[0]).stem.lower() if argv and argv[0] else ""

    if invoked_as in WRAPPER_NAMES:
        from app.wrapper import main as wrapper_main

        return wrapper_main(argv)

    from app.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
