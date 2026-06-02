"""Integration tests for the poll loop using fake collaborators."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from slack_benchy.db import Database
from slack_benchy.poller import Poller
from slack_benchy.prusalink import (
    STATE_FINISHED,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_PRINTING,
    PrinterSnapshot,
    PrusaLinkAuthError,
    PrusaLinkUnreachable,
)
from slack_benchy.transitions import TransitionEvent, TransitionKind


def _snap(state: str, job_key: str | None = None, file_name: str | None = None, online: bool = True) -> PrinterSnapshot:
    return PrinterSnapshot(
        online=online,
        state=state,
        job_id=job_key.split("::")[-1] if job_key else None,
        job_key=job_key,
        file_name=file_name,
        progress_percent=10.0 if state == STATE_PRINTING else None,
        time_remaining_s=3600 if state == STATE_PRINTING else None,
        time_printing_s=600 if state == STATE_PRINTING else None,
        material=None,
        nozzle_temp_c=None,
        bed_temp_c=None,
        error_message=None,
    )


class FakePrusaLink:
    def __init__(self):
        self.script: list[Any] = []

    def queue(self, item: Any) -> None:
        self.script.append(item)

    async def get_snapshot(self) -> PrinterSnapshot:
        if not self.script:
            raise PrusaLinkUnreachable("nothing queued")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class _Update:
    snapshot: PrinterSnapshot
    age: float


class FakeBot:
    def __init__(self):
        self.updates: list[_Update] = []
        self.events: list[tuple[TransitionEvent, PrinterSnapshot]] = []

    async def update_status_message(self, snapshot: PrinterSnapshot, age_seconds: float) -> None:
        self.updates.append(_Update(snapshot, age_seconds))

    async def emit_events(self, events: list[TransitionEvent], snapshot: PrinterSnapshot) -> None:
        for e in events:
            self.events.append((e, snapshot))


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "poller.sqlite3")
    await d.open()
    try:
        yield d
    finally:
        await d.close()


async def _drive_one_tick(poller: Poller) -> None:
    """Run one cycle of the poller without the sleep loop."""

    await poller._tick()  # type: ignore[attr-defined]


async def test_single_failure_keeps_last_snapshot(db: Database):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 4, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(_snap(STATE_PRINTING, "f.gcode::1", "f.gcode"))
    await _drive_one_tick(poller)

    pl.queue(PrusaLinkUnreachable("router blip"))
    await _drive_one_tick(poller)

    # Still considered printing; never flipped to offline.
    assert poller.current_snapshot is not None
    assert poller.current_snapshot.state == STATE_PRINTING


async def test_flips_to_offline_after_threshold(db: Database):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 3, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(_snap(STATE_PRINTING, "f.gcode::1", "f.gcode"))
    await _drive_one_tick(poller)

    for _ in range(3):
        pl.queue(PrusaLinkUnreachable("still down"))
        await _drive_one_tick(poller)

    assert poller.current_snapshot is not None
    assert poller.current_snapshot.state == STATE_OFFLINE
    assert poller.current_snapshot.online is False


async def test_recovery_after_failures_returns_to_real_state(db: Database):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 2, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(_snap(STATE_PRINTING, "f.gcode::1", "f.gcode"))
    await _drive_one_tick(poller)

    pl.queue(PrusaLinkUnreachable("blip"))
    pl.queue(PrusaLinkUnreachable("blip"))
    await _drive_one_tick(poller)
    await _drive_one_tick(poller)
    assert poller.current_snapshot is not None
    assert poller.current_snapshot.state == STATE_OFFLINE

    pl.queue(_snap(STATE_IDLE))
    await _drive_one_tick(poller)
    assert poller.current_snapshot.state == STATE_IDLE


async def test_finished_transition_emits_event(db: Database):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 4, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(_snap(STATE_PRINTING, "f.gcode::1", "f.gcode"))
    await _drive_one_tick(poller)

    pl.queue(_snap(STATE_FINISHED, "f.gcode::1", "f.gcode"))
    await _drive_one_tick(poller)

    kinds = [e.kind for (e, _) in bot.events]
    assert TransitionKind.FINISHED in kinds


async def test_status_update_happens_on_first_tick(db: Database):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 4, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(_snap(STATE_IDLE))
    await _drive_one_tick(poller)
    assert len(bot.updates) == 1


async def test_auth_error_does_not_kill_loop(db: Database):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 4, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(PrusaLinkAuthError("bad key"))
    # Tick should not raise.
    await _drive_one_tick(poller)


async def test_snapshot_persisted_for_restart(db: Database, tmp_path: Path):
    pl = FakePrusaLink()
    bot = FakeBot()
    poller = Poller(30, 4, pl, bot, db)  # type: ignore[arg-type]

    pl.queue(_snap(STATE_PRINTING, "f.gcode::1", "f.gcode"))
    await _drive_one_tick(poller)

    # Simulate a restart by building a new poller against the same DB.
    pl2 = FakePrusaLink()
    bot2 = FakeBot()
    poller2 = Poller(30, 4, pl2, bot2, db)  # type: ignore[arg-type]
    await poller2._restore_last_snapshot()  # type: ignore[attr-defined]
    assert poller2.current_snapshot is not None
    assert poller2.current_snapshot.state == STATE_PRINTING
    assert poller2.current_snapshot.job_key == "f.gcode::1"
