"""Edge-triggered state transition detection.

Given the previous and current snapshots, emit notification events. Notifications
fire on transitions, never on snapshot state alone, so a long-standing ERROR
does not page someone every poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .prusalink import (
    STATE_ERROR,
    STATE_FINISHED,
    STATE_PAUSED,
    STATE_PRINTING,
    STATE_STOPPED,
    PrinterSnapshot,
)


class TransitionKind(StrEnum):
    FINISHED = "FINISHED"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    STARTED = "STARTED"


@dataclass(frozen=True)
class TransitionEvent:
    kind: TransitionKind
    job_key: str
    file_name: str | None
    detail: str | None = None


def detect_transitions(
    previous: PrinterSnapshot | None,
    current: PrinterSnapshot,
) -> list[TransitionEvent]:
    """Compare two snapshots and return the list of events to fire.

    Pure function: makes no I/O, takes no time. Tested in isolation.
    """

    if not current.online:
        return []

    # First-ever snapshot: nothing to compare against, no events.
    if previous is None or not previous.online:
        return []

    events: list[TransitionEvent] = []

    prev_state = previous.state
    cur_state = current.state
    prev_key = previous.job_key
    cur_key = current.job_key

    if cur_state == STATE_ERROR and prev_state != STATE_ERROR:
        key = cur_key or prev_key or "unknown"
        events.append(
            TransitionEvent(
                kind=TransitionKind.ERROR,
                job_key=key,
                file_name=current.file_name or previous.file_name,
                detail=current.error_message,
            )
        )
        return events

    if cur_state == STATE_PAUSED and prev_state == STATE_PRINTING:
        events.append(
            TransitionEvent(
                kind=TransitionKind.PAUSED,
                job_key=cur_key or prev_key or "unknown",
                file_name=current.file_name,
            )
        )
        return events

    if cur_state == STATE_PRINTING and prev_state == STATE_PAUSED and prev_key == cur_key:
        events.append(
            TransitionEvent(
                kind=TransitionKind.RESUMED,
                job_key=cur_key or "unknown",
                file_name=current.file_name,
            )
        )
        return events

    if cur_state == STATE_FINISHED and prev_state in {STATE_PRINTING, STATE_PAUSED}:
        events.append(
            TransitionEvent(
                kind=TransitionKind.FINISHED,
                job_key=prev_key or cur_key or "unknown",
                file_name=previous.file_name or current.file_name,
            )
        )
        return events

    if cur_state == STATE_STOPPED and prev_state in {STATE_PRINTING, STATE_PAUSED}:
        events.append(
            TransitionEvent(
                kind=TransitionKind.CANCELLED,
                job_key=prev_key or cur_key or "unknown",
                file_name=previous.file_name or current.file_name,
            )
        )
        return events

    # Print started: previously idle/none, now actively printing a real job.
    if cur_state == STATE_PRINTING and prev_state not in {STATE_PRINTING, STATE_PAUSED} and cur_key:
        events.append(
            TransitionEvent(
                kind=TransitionKind.STARTED,
                job_key=cur_key,
                file_name=current.file_name,
            )
        )
        return events

    # Job swap mid-flight (file changed while still printing). Treat as
    # implicit finish-of-prev + start-of-new so trackers stop receiving updates
    # for the old print.
    if (
        cur_state == STATE_PRINTING
        and prev_state in {STATE_PRINTING, STATE_PAUSED}
        and prev_key
        and cur_key
        and prev_key != cur_key
    ):
        events.append(
            TransitionEvent(
                kind=TransitionKind.FINISHED,
                job_key=prev_key,
                file_name=previous.file_name,
                detail="superseded by a new print",
            )
        )
        events.append(
            TransitionEvent(
                kind=TransitionKind.STARTED,
                job_key=cur_key,
                file_name=current.file_name,
            )
        )

    return events
