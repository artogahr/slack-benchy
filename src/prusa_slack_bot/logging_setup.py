"""Logging configuration with a redaction filter that scrubs known secrets."""

from __future__ import annotations

import logging
import re
import sys

_SECRET_PATTERNS = [
    re.compile(r"xoxb-[A-Za-z0-9-]+"),
    re.compile(r"xapp-[A-Za-z0-9-]+"),
    re.compile(r"(?i)(api[_-]?key=)[^\s&\"']+"),
    re.compile(r"(?i)(authorization:\s*)\S+"),
]


class RedactionFilter(logging.Filter):
    def __init__(self, extra_secrets: list[str] | None = None):
        super().__init__()
        self._extras = [re.escape(s) for s in (extra_secrets or []) if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _SECRET_PATTERNS:
            msg = pat.sub(lambda m: m.group(1) + "***" if m.groups() else "***", msg)
        for extra in self._extras:
            msg = re.sub(extra, "***", msg)
        record.msg = msg
        record.args = ()
        return True


def configure(level: str = "INFO", extra_secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactionFilter(extra_secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Quiet the noisier libs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("slack_bolt").setLevel(logging.INFO)
