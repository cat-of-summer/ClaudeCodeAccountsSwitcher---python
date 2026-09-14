from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.store import AUTO_SWITCH_STRATEGIES, Config
from ui import i18n
from ui.i18n import t

SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

BOOL_TRUE = {"on", "true", "1", "yes", "enabled", "enable"}
BOOL_FALSE = {"off", "false", "0", "no", "disabled", "disable"}


class SettingError(Exception):
    """Carries an already-translated message for the caller to print."""


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str  # "bool" | "int" | "choice" | "text" | "path"
    get: Callable[[Config], Any]
    set: Callable[[Config, Any], None]
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    editable_in_screen: bool = True
    choice_labels: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return t(f"config.label_{self.key}")

    @property
    def help(self) -> str:
        return t(f"config.help_{self.key}")

    def display(self, config: Config) -> str:
        value = self.get(config)
        if self.kind == "bool":
            return t("config.value_on") if value else t("config.value_off")
        if self.kind == "choice" and value in self.choice_labels:
            return t(self.choice_labels[value])
        text = str(value or "")
        return text or t("config.value_empty")


def _auto(key: str) -> Callable[[Config], Any]:
    return lambda config: config.auto_switch.get(key)


def _set_auto(key: str) -> Callable[[Config, Any], None]:
    def _apply(config: Config, value: Any) -> None:
        merged = dict(config.auto_switch)
        merged[key] = value
        config.auto_switch = merged

    return _apply


def _get_max_wait(config: Config) -> int:
    """Shown in minutes: nobody thinks about a wall in seconds."""
    value = config.auto_switch.get("maxWaitSeconds")
    return int(value // 60) if isinstance(value, (int, float)) else 0


def _set_max_wait(config: Config, value: Any) -> None:
    _set_auto("maxWaitSeconds")(config, int(value) * 60)


def _get_skip(config: Config) -> bool:
    return SKIP_PERMISSIONS_FLAG in config.default_args


def _set_skip(config: Config, value: Any) -> None:
    """Treat the flag as a view over default_args, not as a field of its own.

    Anyone who hand-added another default to config.json keeps it: rebuilding
    the list from scratch would quietly drop their edit.
    """
    args = [arg for arg in config.default_args if arg != SKIP_PERMISSIONS_FLAG]
    if value:
        args.insert(0, SKIP_PERMISSIONS_FLAG)
    config.default_args = args


def _set_language(config: Config, value: Any) -> None:
    config.language = str(value)
    i18n.set_language(str(value))


def _set_claude_path(config: Config, value: Any) -> None:
    candidate = Path(str(value)).expanduser()
    if not candidate.is_file():
        raise SettingError(t("error.claude_path_invalid", path=candidate))
    config.real_claude_path = str(candidate)


SETTINGS: tuple[Setting, ...] = (
    Setting("skip-permissions", "bool", _get_skip, _set_skip),
    Setting("auto-switch", "bool", _auto("enabled"), _set_auto("enabled")),
    Setting(
        "auto-switch-strategy",
        "choice",
        _auto("strategy"),
        _set_auto("strategy"),
        choices=AUTO_SWITCH_STRATEGIES,
        choice_labels={name: f"config.strategy_{name}" for name in AUTO_SWITCH_STRATEGIES},
    ),
    Setting(
        "auto-switch-threshold",
        "int",
        _auto("threshold"),
        _set_auto("threshold"),
        minimum=50,
        maximum=100,
    ),
    Setting(
        "auto-switch-max",
        "int",
        _auto("maxSwitches"),
        _set_auto("maxSwitches"),
        minimum=1,
        maximum=10,
    ),
    Setting(
        "auto-switch-max-wait",
        "int",
        _get_max_wait,
        _set_max_wait,
        minimum=0,
        maximum=1440,
    ),
    Setting(
        "auto-switch-confirm",
        "bool",
        _auto("confirmWithApi"),
        _set_auto("confirmWithApi"),
    ),
    Setting(
        "auto-switch-restore-mode",
        "bool",
        _auto("restoreMode"),
        _set_auto("restoreMode"),
    ),
    Setting(
        "auto-switch-prompt",
        "text",
        _auto("resumePrompt"),
        _set_auto("resumePrompt"),
        editable_in_screen=False,
    ),
    Setting(
        "language",
        "choice",
        lambda config: config.language or i18n.current_language(),
        _set_language,
        choices=i18n.available_languages(),
    ),
    Setting(
        "claude-path",
        "path",
        lambda config: config.real_claude_path,
        _set_claude_path,
        editable_in_screen=False,
    ),
)

BY_KEY = {setting.key: setting for setting in SETTINGS}


def find(key: str) -> Setting:
    setting = BY_KEY.get(key.strip().lower())
    if setting is None:
        raise SettingError(
            t("config.bad_key", key=key, keys=", ".join(BY_KEY)),
        )
    return setting


def parse_bool(setting: Setting, raw: str) -> bool:
    probe = raw.strip().lower()
    if probe in BOOL_TRUE or probe in i18n.answers("common.yes_answers"):
        return True
    if probe in BOOL_FALSE or probe in i18n.answers("common.no_answers"):
        return False
    raise SettingError(t("config.bad_value", key=setting.key, value=raw))


def parse(setting: Setting, raw: str) -> Any:
    if setting.kind == "bool":
        return parse_bool(setting, raw)

    if setting.kind == "int":
        try:
            number = int(raw.strip())
        except ValueError:
            raise SettingError(
                t("config.bad_value", key=setting.key, value=raw)
            ) from None
        if not setting.minimum <= number <= setting.maximum:
            raise SettingError(
                t(
                    "config.out_of_range",
                    key=setting.key,
                    minimum=setting.minimum,
                    maximum=setting.maximum,
                )
            )
        return number

    if setting.kind == "choice":
        probe = raw.strip().lower()
        if probe not in setting.choices:
            raise SettingError(
                t("config.bad_choice", key=setting.key, choices=", ".join(setting.choices))
            )
        return probe

    return raw


def apply(setting: Setting, value: Any) -> Config:
    """Read, change and save in one go, so screens never hold a stale Config."""
    config = Config.load()
    setting.set(config, value)
    config.save()
    return config


def cycle_choice(setting: Setting, config: Config, step: int) -> Any:
    current = setting.get(config)
    options = list(setting.choices)
    if not options:
        return current
    try:
        index = options.index(current)
    except ValueError:
        index = 0
    return options[(index + step) % len(options)]


def step_int(setting: Setting, config: Config, step: int) -> int:
    current = setting.get(config)
    number = int(current) if isinstance(current, (int, float)) else setting.minimum
    return max(setting.minimum, min(setting.maximum, number + step))


def as_lines(config: Config) -> list[tuple[str, str]]:
    return [(setting.key, setting.display(config)) for setting in SETTINGS]


__all__ = [
    "SETTINGS",
    "Setting",
    "SettingError",
    "apply",
    "as_lines",
    "cycle_choice",
    "find",
    "parse",
    "step_int",
]
