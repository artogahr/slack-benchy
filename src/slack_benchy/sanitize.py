"""User-text hardening for free-text modal fields."""

from __future__ import annotations

import re
import unicodedata

MAX_FILAMENT_NAME = 64
MAX_INVENTORY = 50

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_SLACK_MENTION_RE = re.compile(r"<[!@#][^>]*>")
_COMBINING_LIMIT = 2


class SanitizeError(ValueError):
    """Raised when input cannot be safely accepted."""


def sanitize_filament_name(raw: str) -> str:
    """Return a clean, display-safe filament name or raise SanitizeError.

    Steps: normalize, strip control chars, collapse whitespace, neutralize Slack
    markup, cap consecutive combining marks, cap length.
    """

    if not isinstance(raw, str):
        raise SanitizeError("Name must be text.")

    s = unicodedata.normalize("NFC", raw)
    s = _CONTROL_RE.sub("", s)
    s = _SLACK_MENTION_RE.sub("", s)
    # Replace Slack auto-link/format markers we know about
    s = s.replace("<", "‹").replace(">", "›")
    s = _WHITESPACE_RUN_RE.sub(" ", s).strip()

    if not s:
        raise SanitizeError("Name cannot be empty.")

    s = _limit_combining_marks(s, _COMBINING_LIMIT)

    if len(s) > MAX_FILAMENT_NAME:
        s = s[:MAX_FILAMENT_NAME].rstrip()

    if not s:
        raise SanitizeError("Name is invalid.")

    return s


def _limit_combining_marks(s: str, limit: int) -> str:
    """Drop runs of combining marks longer than ``limit`` to defang zalgo."""

    out: list[str] = []
    run = 0
    for ch in s:
        if unicodedata.category(ch) == "Mn":
            run += 1
            if run > limit:
                continue
        else:
            run = 0
        out.append(ch)
    return "".join(out)


def normalized_key(name: str) -> str:
    """Case-folded, whitespace-collapsed key used for dedupe checks."""

    s = unicodedata.normalize("NFC", name).casefold()
    s = _WHITESPACE_RUN_RE.sub(" ", s).strip()
    return s
