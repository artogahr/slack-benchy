from prusa_slack_bot.db import Filament
from prusa_slack_bot.prusalink import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_PAUSED,
    STATE_PRINTING,
    PrinterSnapshot,
)
from prusa_slack_bot.status_message import render_status, should_update


def snap(**overrides) -> PrinterSnapshot:
    defaults = dict(
        online=True,
        state=STATE_IDLE,
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
    defaults.update(overrides)
    return PrinterSnapshot(**defaults)


def _all_text(blocks):
    out: list[str] = []
    for b in blocks:
        if "text" in b and isinstance(b["text"], dict):
            out.append(b["text"].get("text", ""))
        if "fields" in b:
            for f in b["fields"]:
                out.append(f.get("text", ""))
        if "elements" in b:
            for el in b["elements"]:
                if isinstance(el, dict) and isinstance(el.get("text"), dict):
                    out.append(el["text"].get("text", ""))
                elif isinstance(el, dict) and "text" in el:
                    out.append(str(el["text"]))
    return "\n".join(out)


def test_idle_message_minimal():
    view = render_status(snap(), loaded=None, age_seconds=2.0)
    text = _all_text(view.blocks)
    assert "Idle" in text
    assert "updated 2s ago" in text


def test_printing_shows_progress_and_eta():
    view = render_status(
        snap(
            state=STATE_PRINTING,
            job_key="m.gcode::1",
            file_name="m.gcode",
            progress_percent=42.0,
            time_remaining_s=3600,
            time_printing_s=1800,
        ),
        loaded=Filament(id=1, name="PLA Galaxy Black", is_loaded=True),
        age_seconds=10.0,
    )
    text = _all_text(view.blocks)
    assert "m.gcode" in text
    assert "42%" in text
    assert "1h 00m left" in text


def test_offline_shows_degradation_banner():
    view = render_status(snap(online=False, state=STATE_OFFLINE), loaded=None, age_seconds=420.0)
    text = _all_text(view.blocks)
    assert "Offline" in text or "Haven't heard" in text


def test_stale_footer_warns():
    view = render_status(snap(), loaded=None, age_seconds=120.0)
    text = _all_text(view.blocks)
    assert "stale" in text.lower()


def test_degraded_footer_kicks_in_after_5_min():
    view = render_status(snap(), loaded=None, age_seconds=400.0)
    text = _all_text(view.blocks)
    assert "haven't heard" in text.lower()


def test_material_mismatch_surfaced():
    view = render_status(
        snap(state=STATE_PRINTING, file_name="m.gcode", job_key="m.gcode::1", material="PETG"),
        loaded=Filament(id=1, name="PLA Galaxy Black", is_loaded=True),
        age_seconds=5.0,
    )
    text = _all_text(view.blocks)
    assert "Double-check" in text or "PETG" in text


def test_no_mismatch_when_materials_align():
    view = render_status(
        snap(state=STATE_PRINTING, file_name="m.gcode", job_key="m.gcode::1", material="PLA"),
        loaded=Filament(id=1, name="PLA Galaxy Black", is_loaded=True),
        age_seconds=5.0,
    )
    text = _all_text(view.blocks)
    assert "Double-check" not in text


def test_buttons_include_track_when_printing():
    view = render_status(
        snap(state=STATE_PRINTING, file_name="m.gcode", job_key="m.gcode::1"),
        loaded=None,
        age_seconds=1.0,
    )
    action_ids: list[str] = []
    for b in view.blocks:
        if b.get("type") == "actions":
            for el in b["elements"]:
                action_ids.append(el["action_id"])
    assert "toggle_track" in action_ids
    assert "job_pause" in action_ids
    assert "job_cancel_confirm" in action_ids


def test_buttons_swap_inventory_always():
    view = render_status(snap(state=STATE_IDLE), loaded=None, age_seconds=1.0)
    action_ids: list[str] = []
    for b in view.blocks:
        if b.get("type") == "actions":
            for el in b["elements"]:
                action_ids.append(el["action_id"])
    assert "open_swap_filament" in action_ids
    assert "open_manage_inventory" in action_ids
    assert "job_pause" not in action_ids


def test_pause_button_only_when_printing():
    view = render_status(snap(state=STATE_PAUSED, file_name="m.gcode", job_key="m.gcode::1"), loaded=None, age_seconds=1.0)
    action_ids = [
        el["action_id"]
        for b in view.blocks
        if b.get("type") == "actions"
        for el in b["elements"]
    ]
    assert "job_resume" in action_ids
    assert "job_pause" not in action_ids


def test_file_name_with_slack_mention_is_neutered():
    view = render_status(
        snap(state=STATE_PRINTING, file_name="<!channel> evil.gcode", job_key="x::1"),
        loaded=None,
        age_seconds=1.0,
    )
    text = _all_text(view.blocks)
    assert "<!channel>" not in text


# should_update tests

def test_should_update_no_previous_yes():
    assert should_update(None, snap(), None, None, 0.0) is True


def test_should_update_state_change_yes():
    prev = snap(state=STATE_IDLE)
    cur = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode")
    assert should_update(prev, cur, None, None, 1.0) is True


def test_should_update_small_progress_no():
    prev = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode", progress_percent=10.0)
    cur = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode", progress_percent=11.0)
    assert should_update(prev, cur, 10.0, None, 5.0) is False


def test_should_update_progress_delta_threshold_yes():
    prev = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode", progress_percent=10.0)
    cur = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode", progress_percent=16.0)
    assert should_update(prev, cur, 10.0, None, 5.0) is True


def test_should_update_eta_delta_threshold_yes():
    prev = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode", progress_percent=10.0, time_remaining_s=3600)
    cur = snap(state=STATE_PRINTING, job_key="x::1", file_name="x.gcode", progress_percent=11.0, time_remaining_s=2400)
    assert should_update(prev, cur, 10.0, 3600, 5.0) is True


def test_should_update_periodic_refresh():
    prev = snap()
    cur = snap()
    assert should_update(prev, cur, None, None, 45.0) is True


def test_should_update_error_message_change_yes():
    prev = snap(state=STATE_ERROR, error_message="hot")
    cur = snap(state=STATE_ERROR, error_message="cold")
    assert should_update(prev, cur, None, None, 1.0) is True
