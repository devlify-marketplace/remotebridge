"""
Phase 12 - localization for the desktop host/viewer.

Design: the English text IS the message key (gettext style), so code stays
readable - `t("Connection approved")` - and a missing translation degrades to
readable English instead of a raw key like "viewer.approved". Catalogs are JSON,
one per language, in desktop/locales/<code>.json:

    {"_meta": {"name": "Español"},
     "strings": {"Connection approved": "Conexión aprobada",
                 "Viewer '{viewer}' joined": "El visor '{viewer}' se unió"}}

Placeholders use str.format names ({viewer}), never positional, so a translation
can reorder them. A translation whose placeholders don't match the call is
ignored (English is used) rather than crashing a live session.

Language choice, first match wins:
    1. set_language("es")                      (e.g. from --lang)
    2. environment variable REMOTEBRIDGE_LANG
    3. the OS locale (LC_ALL / LC_MESSAGES / LANG)
    4. English
"Español de México" style regional codes fall back to the base language
(es_MX -> es) when there's no regional catalog.

The pseudo-language "en-XA" is built in, not a file: it accents every letter
and pads the string, so a screenshot or a scan of output shows at a glance
which strings still bypass t() (they stay plain ASCII) and whether a layout
survives ~30% longer text. Tests/test_i18n.py uses it to prove coverage.

Translations of this project's strings were written without a professional
review; have a native speaker check them before a public launch
(see docs/phases/PHASE_12_README.md).
"""

import json
import locale
import os
import string
import threading

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
DEFAULT_LANGUAGE = "en"
PSEUDO_LANGUAGE = "en-XA"

_lock = threading.Lock()
_language = None            # explicit choice, or None = auto-detect on first use
_catalogs: dict = {}        # code -> {english: translated}


_ACCENTS = str.maketrans(
    "AEIOUYaeiouycnszCNSZ",
    "ÁÉÍÓÚÝáéíóúýçñšžÇÑŠŽ")


def _placeholders(text: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def pseudo(text: str) -> str:
    """Accent letters (leaving {placeholders} intact) and pad ~30%."""
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        if depth:
            out.append(ch)
        else:
            out.append(ch.translate(_ACCENTS))
        if ch == "}":
            depth = max(0, depth - 1)
    body = "".join(out)
    pad = "·" * max(1, len(text) // 3)
    return f"[{body}{pad}]"


def available_languages(locales_dir: str = None) -> dict:
    """{code: native name} for every catalog on disk, plus English."""
    found = {DEFAULT_LANGUAGE: "English"}
    directory = locales_dir or LOCALES_DIR
    if os.path.isdir(directory):
        for fname in sorted(os.listdir(directory)):
            if fname.endswith(".json"):
                code = fname[:-5]
                try:
                    with open(os.path.join(directory, fname), encoding="utf-8") as f:
                        found[code] = json.load(f).get("_meta", {}).get("name", code)
                except (OSError, ValueError):
                    continue
    return found


def _load(code: str) -> dict:
    with _lock:
        if code in _catalogs:
            return _catalogs[code]
        path = os.path.join(LOCALES_DIR, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as f:
                _catalogs[code] = json.load(f).get("strings", {})
        except (OSError, ValueError):
            _catalogs[code] = {}
        return _catalogs[code]


def normalize(code: str) -> str:
    """'es_MX.UTF-8' / 'ES-mx' -> best available catalog code ('es'), else 'en'."""
    if not code:
        return DEFAULT_LANGUAGE
    code = code.split(".")[0].split("@")[0].replace("_", "-")
    if code.lower() == PSEUDO_LANGUAGE.lower():
        return PSEUDO_LANGUAGE
    avail = {c.lower(): c for c in available_languages()}
    for candidate in (code.lower(), code.lower().split("-")[0]):
        if candidate in avail:
            return avail[candidate]
    return DEFAULT_LANGUAGE


def detect_system_language() -> str:
    env = os.environ.get("REMOTEBRIDGE_LANG")
    if env:
        return normalize(env)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value and value not in ("C", "POSIX"):
            return normalize(value)
    try:
        loc = locale.getlocale()[0]
        if loc:
            return normalize(loc)
    except (ValueError, TypeError):
        pass
    return DEFAULT_LANGUAGE


def set_language(code) -> str:
    """Pins the language (None = go back to auto-detect). Returns the code in effect."""
    global _language
    _language = normalize(code) if code else None
    return current_language()


def current_language() -> str:
    return _language or detect_system_language()


def t(message: str, **values) -> str:
    """Translate `message` into the current language and fill placeholders.
    Never raises: a missing translation or mismatched placeholder yields the
    English text."""
    lang = current_language()
    translated = message
    if lang == PSEUDO_LANGUAGE:
        translated = pseudo(message)
    elif lang != DEFAULT_LANGUAGE:
        candidate = _load(lang).get(message)
        if candidate and _placeholders(candidate) == _placeholders(message):
            translated = candidate
    try:
        return translated.format(**values) if values or "{" in translated else translated
    except (KeyError, IndexError, ValueError):
        try:
            return message.format(**values)
        except (KeyError, IndexError, ValueError):
            return message
