from pathlib import Path

import pytest

from prusa_slack_bot.single_instance import AlreadyRunning, SingleInstanceLock


def test_lock_blocks_second_acquirer(tmp_path: Path):
    a = SingleInstanceLock(tmp_path / "x.lock")
    b = SingleInstanceLock(tmp_path / "x.lock")
    a.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            b.acquire()
    finally:
        a.release()


def test_lock_releases_for_next_acquirer(tmp_path: Path):
    a = SingleInstanceLock(tmp_path / "x.lock")
    a.acquire()
    a.release()
    b = SingleInstanceLock(tmp_path / "x.lock")
    b.acquire()
    b.release()
