"""
Phase 12 - localization for the admin web console.

Same catalog format and English-text-as-key convention as desktop/i18n.py
(see that file for the rationale), but request-scoped instead of
process-global: a web server answers many people at once, each in their own
language, so nothing here keeps a "current language" in module state - the
language is chosen per request and passed in.

Language choice per request, first match wins:
    1. a `lang` cookie (set when someone picks a language from the footer picker)
    2. the browser's Accept-Language header
    3. English

Catalogs live in admin/locales/<code>.json:
    {"_meta": {"name": "Español"}, "strings": {"Dashboard": "Panel", ...}}
"en-XA" is a built-in pseudo-language (accented + padded) used by
tests/test_admin_i18n.py to prove every visible string goes through translation.
Translations were written without professional review - see PHASE_12_README.md.
"""

import json
import os
import string

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
DEFAULT_LANGUAGE = "en"
PSEUDO_LANGUAGE = "en-XA"

_catalogs: dict = {}
_ACCENTS = str.maketrans("AEIOUYaeiouycnszCNSZ", "ÁÉÍÓÚÝáéíóúýçñšžÇÑŠŽ")


def _placeholders(text: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def pseudo(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        out.append(ch if depth else ch.translate(_ACCENTS))
        if ch == "}":
            depth = max(0, depth - 1)
    return f"[{''.join(out)}{'·' * max(1, len(text) // 3)}]"


def available_languages() -> dict:
    """{code: native name}, English first."""
    found = {DEFAULT_LANGUAGE: "English"}
    if os.path.isdir(LOCALES_DIR):
        for fname in sorted(os.listdir(LOCALES_DIR)):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(LOCALES_DIR, fname), encoding="utf-8") as f:
                        found[fname[:-5]] = json.load(f).get("_meta", {}).get("name", fname[:-5])
                except (OSError, ValueError):
                    continue
    return found


def _catalog(code: str) -> dict:
    if code not in _catalogs:
        try:
            with open(os.path.join(LOCALES_DIR, f"{code}.json"), encoding="utf-8") as f:
                _catalogs[code] = json.load(f).get("strings", {})
        except (OSError, ValueError):
            _catalogs[code] = {}
    return _catalogs[code]


def normalize(code) -> str:
    if not code:
        return DEFAULT_LANGUAGE
    code = str(code).split(".")[0].replace("_", "-")
    if code.lower() == PSEUDO_LANGUAGE.lower():
        return PSEUDO_LANGUAGE
    avail = {c.lower(): c for c in available_languages()}
    for candidate in (code.lower(), code.lower().split("-")[0]):
        if candidate in avail:
            return avail[candidate]
    return DEFAULT_LANGUAGE


def negotiate(accept_language: str) -> str:
    """Best supported language for an Accept-Language header, honoring q-values."""
    ranked = []
    for part in (accept_language or "").split(","):
        piece, _, q = part.strip().partition(";q=")
        if not piece or piece == "*":
            continue
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        ranked.append((weight, piece))
    for _, piece in sorted(ranked, key=lambda x: -x[0]):
        lang = normalize(piece)
        if lang != DEFAULT_LANGUAGE or piece.lower().startswith("en"):
            return lang
    return DEFAULT_LANGUAGE


def lookup(message: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """The translated text with placeholders still unfilled ({name} stays {name}).
    Missing translation or mismatched placeholders -> the English message."""
    if lang == PSEUDO_LANGUAGE:
        return pseudo(message)
    if lang != DEFAULT_LANGUAGE:
        candidate = _catalog(lang).get(message)
        if candidate and _placeholders(candidate) == _placeholders(message):
            return candidate
    return message


def translate(message: str, lang: str = DEFAULT_LANGUAGE, **values) -> str:
    """Never raises. Missing translation or mismatched placeholders -> English."""
    text = lookup(message, lang)
    try:
        return text.format(**values) if values or "{" in text else text
    except (KeyError, IndexError, ValueError):
        try:
            return message.format(**values)
        except (KeyError, IndexError, ValueError):
            return message
