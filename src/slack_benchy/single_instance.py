"""POSIX file lock used as a single-instance guard.

Two copies of the bot updating the same status message will flap, so we
fail fast and loudly if a second instance starts.
"""

from __future__ import annotations

import fcntl
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(
                f"Another slack-benchy instance holds {self._path}. "
                "Refusing to start a second copy — it would flap the status message."
            ) from exc
        self._fh.write(f"{__import__('os').getpid()}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
