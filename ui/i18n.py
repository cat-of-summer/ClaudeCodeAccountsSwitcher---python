from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "ru")
ENV_OVERRIDE = "CCAS_LANG"

_LOCALE_VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
_WINDOWS_PRIMARY_RUSSIAN = 0x19
_WINDOWS_PRIMARY_MASK = 0x3FF

_catalogs: dict[str, dict[str, str]] = {}
_language = DEFAULT_LANGUAGE


def lang_dir() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "lang"
    return Path(__file__).resolve().parent.parent / "lang"


def _catalog(code: str) -> dict[str, str]:
    cached = _catalogs.get(code)
    if cached is not None:
        return cached

    data: dict[str, str] = {}
    path = lang_dir() / f"{code}.json"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = {k: v for k, v in loaded.items() if isinstance(v, str)}
    except (OSError, ValueError):
        data = {}

    _catalogs[code] = data
    return data


def available_languages() -> tuple[str, ...]:
    return SUPPORTED_LANGUAGES


def normalise(raw: str | None) -> str:
    if not raw:
        return ""
    code = raw.strip().replace("-", "_")
    code = code.split(":")[0].split(".")[0].split("_")[0].lower()
    return code if code in SUPPORTED_LANGUAGES else ""


def detect_system_language() -> str:
    for variable in (ENV_OVERRIDE, *_LOCALE_VARS):
        code = normalise(os.environ.get(variable))
        if code:
            return code

    if os.name == "nt":
        try:
            import ctypes

            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if (langid & _WINDOWS_PRIMARY_MASK) == _WINDOWS_PRIMARY_RUSSIAN:
                return "ru"
            return DEFAULT_LANGUAGE
        except (AttributeError, OSError, ValueError):
            pass

    return DEFAULT_LANGUAGE


def set_language(code: str | None = None) -> str:
    global _language

    override = normalise(os.environ.get(ENV_OVERRIDE))
    chosen = override or normalise(code) or detect_system_language()

    _language = chosen
    _catalog(chosen)
    if chosen != DEFAULT_LANGUAGE:
        _catalog(DEFAULT_LANGUAGE)
    return chosen


def current_language() -> str:
    return _language


def t(key: str, **params: object) -> str:
    template = _catalog(_language).get(key)
    if template is None and _language != DEFAULT_LANGUAGE:
        template = _catalog(DEFAULT_LANGUAGE).get(key)
    if template is None:
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


def answers(key: str) -> set[str]:
    return {part.strip().lower() for part in t(key).split(",") if part.strip()}
