from prusa_slack_bot.prusalink import (
    STATE_ERROR,
    STATE_FINISHED,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_PAUSED,
    STATE_PRINTING,
    STATE_STOPPED,
    PrinterSnapshot,
)
from prusa_slack_bot.transitions import TransitionKind, detect_transitions


def snap(state: str, job_key: str | None = None, file_name: str | None = None, online: bool = True, error: str | None = None) -> PrinterSnapshot:
    return PrinterSnapshot(
        online=online,
        state=state,
        job_id=job_key.split("::")[-1] if job_key and "::" in job_key else None,
        job_key=job_key,
        file_name=file_name,
        progress_percent=None,
        time_remaining_s=None,
        time_printing_s=None,
        material=None,
        nozzle_temp_c=None,
        bed_temp_c=None,
        error_message=error,
    )


def test_no_previous_emits_nothing():
    assert detect_transitions(None, snap(STATE_PRINTING, "f.gcode::1")) == []


def test_offline_current_emits_nothing():
    prev = snap(STATE_PRINTING, "f.gcode::1")
    cur = snap(STATE_OFFLINE, online=False)
    assert detect_transitions(prev, cur) == []


def test_printing_to_finished_emits_finished():
    prev = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    cur = snap(STATE_FINISHED, "f.gcode::1", "f.gcode")
    events = detect_transitions(prev, cur)
    assert len(events) == 1
    assert events[0].kind is TransitionKind.FINISHED
    assert events[0].job_key == "f.gcode::1"


def test_printing_to_paused_emits_paused():
    prev = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    cur = snap(STATE_PAUSED, "f.gcode::1", "f.gcode")
    events = detect_transitions(prev, cur)
    assert [e.kind for e in events] == [TransitionKind.PAUSED]


def test_paused_to_printing_emits_resumed():
    prev = snap(STATE_PAUSED, "f.gcode::1", "f.gcode")
    cur = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    assert [e.kind for e in detect_transitions(prev, cur)] == [TransitionKind.RESUMED]


def test_anything_to_error_emits_error_with_detail():
    prev = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    cur = snap(STATE_ERROR, "f.gcode::1", "f.gcode", error="Thermal runaway")
    events = detect_transitions(prev, cur)
    assert events[0].kind is TransitionKind.ERROR
    assert events[0].detail == "Thermal runaway"


def test_persistent_error_does_not_re_fire():
    prev = snap(STATE_ERROR, "f.gcode::1", "f.gcode")
    cur = snap(STATE_ERROR, "f.gcode::1", "f.gcode")
    assert detect_transitions(prev, cur) == []


def test_printing_to_stopped_emits_cancelled():
    prev = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    cur = snap(STATE_STOPPED, None, None)
    events = detect_transitions(prev, cur)
    assert [e.kind for e in events] == [TransitionKind.CANCELLED]
    assert events[0].job_key == "f.gcode::1"


def test_idle_to_printing_emits_started():
    prev = snap(STATE_IDLE, None, None)
    cur = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    assert [e.kind for e in detect_transitions(prev, cur)] == [TransitionKind.STARTED]


def test_idle_to_idle_silent():
    prev = snap(STATE_IDLE, None, None)
    cur = snap(STATE_IDLE, None, None)
    assert detect_transitions(prev, cur) == []


def test_printing_swap_emits_finish_then_start():
    prev = snap(STATE_PRINTING, "old.gcode::1", "old.gcode")
    cur = snap(STATE_PRINTING, "new.gcode::2", "new.gcode")
    events = detect_transitions(prev, cur)
    assert [e.kind for e in events] == [TransitionKind.FINISHED, TransitionKind.STARTED]
    assert events[0].job_key == "old.gcode::1"
    assert events[1].job_key == "new.gcode::2"


def test_first_online_snapshot_after_offline_silent():
    prev = snap(STATE_OFFLINE, online=False)
    cur = snap(STATE_PRINTING, "f.gcode::1", "f.gcode")
    assert detect_transitions(prev, cur) == []
