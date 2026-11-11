"""
Internationalization support for ArduScope.

Usage:
    from lib.i18n import t, set_locale

    set_locale("en")
    t("report.title")  # "ArduScope Flight Report"
    t("vibration.good_summary", val=12.3)  # "Vibration normal: max 12.3 m/s²"

Locale files live in locales/<lang>.json (monolithic) and optionally in
locales/<lang>/*.json (per-module).  Per-module files take precedence
over monolithic entries, allowing gradual migration.

Chinese (zh) is the fallback locale.

Thread-safe: each thread gets its own locale via threading.local().
"""

from __future__ import annotations

import glob
import json
import os
import threading

_LOCALE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "locales")
_local = threading.local()
_strings: dict[str, dict[str, str]] = {}
_strings_lock = threading.Lock()


def _load_locale(lang: str) -> dict[str, str]:
    """Load locale strings for *lang*.

    1. Load the monolithic ``locales/{lang}.json`` if it exists.
    2. Overlay all ``locales/{lang}/*.json`` per-module files on top
       (per-module keys take precedence).
    """
    result: dict[str, str] = {}

    mono_path = os.path.join(_LOCALE_DIR, f"{lang}.json")
    if os.path.exists(mono_path):
        with open(mono_path, encoding="utf-8") as f:
            result.update(json.load(f))

    module_dir = os.path.join(_LOCALE_DIR, lang)
    if os.path.isdir(module_dir):
        for module_file in sorted(glob.glob(os.path.join(module_dir, "*.json"))):
            with open(module_file, encoding="utf-8") as f:
                result.update(json.load(f))

    return result


def _ensure_loaded(lang: str) -> None:
    if lang not in _strings:
        with _strings_lock:
            if lang not in _strings:
                _strings[lang] = _load_locale(lang)


def set_locale(lang: str) -> None:
    _local.locale = lang
    _ensure_loaded(lang)


def get_locale() -> str:
    return getattr(_local, "locale", "zh")


def t(_key: str, **kwargs) -> str:
    locale = get_locale()
    _ensure_loaded(locale)
    _ensure_loaded("zh")

    template = (
        _strings.get(locale, {}).get(_key)
        or _strings.get("zh", {}).get(_key)
        or _key
    )
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def available_locales() -> list[str]:
    if not os.path.isdir(_LOCALE_DIR):
        return ["zh"]
    return sorted(
        f.removesuffix(".json")
        for f in os.listdir(_LOCALE_DIR)
        if f.endswith(".json")
    )
