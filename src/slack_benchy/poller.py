"""The unkillable poll loop.

Polls PrusaLink on an interval. A single failed poll is transient: the loop
backs off and tries again. The bot only flips to OFFLINE after N consecutive
failures. Exceptions from the snapshot or from downstream handlers are caught
and logged: the loop must outlive any one bad cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .db import Database
from .prusalink import (
    STATE_OFFLINE,
    PrinterSnapshot,
    PrusaLinkAuthError,
    PrusaLinkClient,
)
from .slack_app import (
    KV_LAST_ETA_SENT,
    KV_LAST_PROGRESS_SENT,
    KV_LAST_SNAPSHOT,
    KV_LAST_UPDATE_AT,
    BotApp,
)
from .status_message import should_update
from .transitions import detect_transitions

logger = logging.getLogger(__name__)


@dataclass
class PollerState:
    last_snapshot: PrinterSnapshot | None = None
    consecutive_failures: int = 0
    last_successful_poll_at: float | None = None


class Poller:
    def __init__(
        self,
        config_poll_interval: int,
        config_offline_after: int,
        prusalink: PrusaLinkClient,
        bot: BotApp,
        db: Database,
    ):
        self._interval = config_poll_interval
        self._offline_after = config_offline_after
        self._prusalink = prusalink
        self._bot = bot
        self._db = db
        self._state = PollerState()
        self._stopped = asyncio.Event()

    @property
    def current_snapshot(self) -> PrinterSnapshot | None:
        return self._state.last_snapshot

    async def run(self) -> None:
        await self._restore_last_snapshot()

        while not self._stopped.is_set():
            cycle_started = time.monotonic()
            try:
                await self._tick()
            except Exception:
                logger.exception("Poll cycle raised unexpectedly. Continuing.")

            elapsed = time.monotonic() - cycle_started
            wait = max(1.0, self._interval - elapsed)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=wait)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stopped.set()

    async def _tick(self) -> None:
        try:
            snapshot = await self._prusalink.get_snapshot()
            self._state.consecutive_failures = 0
            self._state.last_successful_poll_at = time.time()
        except PrusaLinkAuthError as exc:
            # Auth errors are persistent. Log loudly but keep the loop alive
            # so a fixed config recovers without a restart.
            logger.error("PrusaLink auth error: %s", exc)
            self._state.consecutive_failures += 1
            snapshot = self._offline_snapshot()
        except Exception as exc:
            self._state.consecutive_failures += 1
            logger.warning(
                "Poll failed (%d/%d): %s",
                self._state.consecutive_failures,
                self._offline_after,
                exc,
            )
            if self._state.consecutive_failures < self._offline_after:
                # Treat as transient: keep last snapshot, do nothing else.
                age = self._age_seconds()
                if self._state.last_snapshot is not None:
                    await self._bot.update_status_message(self._state.last_snapshot, age)
                return
            snapshot = self._offline_snapshot()

        events = detect_transitions(self._state.last_snapshot, snapshot)
        if events:
            try:
                await self._bot.emit_events(events, snapshot)
            except Exception:
                logger.exception("emit_events failed; continuing.")

        await self._maybe_update_status(snapshot)
        self._state.last_snapshot = snapshot
        await self._db.kv_set_json(KV_LAST_SNAPSHOT, _snapshot_to_dict(snapshot))

    async def _maybe_update_status(self, snapshot: PrinterSnapshot) -> None:
        last_progress = await self._db.kv_get(KV_LAST_PROGRESS_SENT)
        last_eta = await self._db.kv_get(KV_LAST_ETA_SENT)
        last_update_at_raw = await self._db.kv_get(KV_LAST_UPDATE_AT)

        last_progress_f = float(last_progress) if last_progress else None
        last_eta_i = int(last_eta) if last_eta else None
        last_update_at = float(last_update_at_raw) if last_update_at_raw else 0.0
        seconds_since = time.time() - last_update_at if last_update_at else 1e9

        if not should_update(
            self._state.last_snapshot, snapshot, last_progress_f, last_eta_i, seconds_since
        ):
            return

        age = self._age_seconds()
        await self._bot.update_status_message(snapshot, age)
        await self._db.kv_set(KV_LAST_UPDATE_AT, str(time.time()))
        if snapshot.progress_percent is not None:
            await self._db.kv_set(KV_LAST_PROGRESS_SENT, str(snapshot.progress_percent))
        if snapshot.time_remaining_s is not None:
            await self._db.kv_set(KV_LAST_ETA_SENT, str(snapshot.time_remaining_s))

    async def _restore_last_snapshot(self) -> None:
        raw = await self._db.kv_get_json(KV_LAST_SNAPSHOT)
        if raw is None:
            return
        try:
            self._state.last_snapshot = _dict_to_snapshot(raw)
        except Exception:
            logger.debug("Could not restore last snapshot from DB; starting fresh.")

    def _age_seconds(self) -> float:
        if self._state.last_successful_poll_at is None:
            return float("inf")
        return max(0.0, time.time() - self._state.last_successful_poll_at)

    def _offline_snapshot(self) -> PrinterSnapshot:
        return PrinterSnapshot(
            online=False,
            state=STATE_OFFLINE,
            job_id=None,
            job_key=None,
            file_name=None,
            progress_percent=None,
            time_remaining_s=None,
            time_printing_s=None,
            material=None,
            nozzle_temp_c=None,
            bed_temp_c=None,
            error_message=None,
        )


def _snapshot_to_dict(s: PrinterSnapshot) -> dict:
    return {
        "online": s.online,
        "state": s.state,
        "job_id": s.job_id,
        "job_key": s.job_key,
        "file_name": s.file_name,
        "progress_percent": s.progress_percent,
        "time_remaining_s": s.time_remaining_s,
        "time_printing_s": s.time_printing_s,
        "material": s.material,
        "nozzle_temp_c": s.nozzle_temp_c,
        "bed_temp_c": s.bed_temp_c,
        "error_message": s.error_message,
    }


def _dict_to_snapshot(d: dict) -> PrinterSnapshot:
    return PrinterSnapshot(
        online=bool(d.get("online", False)),
        state=d.get("state", STATE_OFFLINE),
        job_id=d.get("job_id"),
        job_key=d.get("job_key"),
        file_name=d.get("file_name"),
        progress_percent=d.get("progress_percent"),
        time_remaining_s=d.get("time_remaining_s"),
        time_printing_s=d.get("time_printing_s"),
        material=d.get("material"),
        nozzle_temp_c=d.get("nozzle_temp_c"),
        bed_temp_c=d.get("bed_temp_c"),
        error_message=d.get("error_message"),
    )
