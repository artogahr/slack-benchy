"""Pure renderer for the live status message (Slack Block Kit)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .db import Filament
from .prusalink import (
    STATE_ATTENTION,
    STATE_BUSY,
    STATE_ERROR,
    STATE_FINISHED,
    STATE_IDLE,
    STATE_OFFLINE,
    STATE_PAUSED,
    STATE_PRINTING,
    STATE_STOPPED,
    PrinterSnapshot,
)

# Slack rate-limit-aware update thresholds.
PROGRESS_DELTA_FOR_UPDATE = 5.0      # percent
ETA_DELTA_SECONDS_FOR_UPDATE = 300   # 5 min
STALE_AFTER_SECONDS = 90             # warn footer
DEGRADED_AFTER_SECONDS = 5 * 60      # visible degradation banner


@dataclass(frozen=True)
class StatusView:
    text: str
    blocks: list[dict[str, Any]]


_STATE_ICON = {
    STATE_IDLE: ":zzz: Idle",
    STATE_PRINTING: ":hourglass_flowing_sand: Printing",
    STATE_PAUSED: ":double_vertical_bar: Paused",
    STATE_FINISHED: ":white_check_mark: Finished",
    STATE_ATTENTION: ":warning: Needs attention",
    STATE_ERROR: ":rotating_light: Error",
    STATE_STOPPED: ":octagonal_sign: Stopped",
    STATE_BUSY: ":gear: Busy",
    STATE_OFFLINE: ":satellite_antenna: Offline",
}


def render_status(
    snapshot: PrinterSnapshot,
    loaded: Filament | None,
    age_seconds: float | None,
    is_tracking_label: str | None = None,
) -> StatusView:
    """Build the Slack message representing the current snapshot."""

    icon = _STATE_ICON.get(snapshot.state, f":question: {snapshot.state.title()}")
    header_text = f"*Printer status*: {icon}"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}
    ]

    if not snapshot.online:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":warning: Haven't heard from the printer. "
                        "Check that it's powered on and reachable on the network.",
                    }
                ],
            }
        )
        blocks.append(_actions_block(snapshot, is_tracking_label))
        blocks.append(_freshness_block(age_seconds))
        return StatusView(text="Printer offline", blocks=blocks)

    # Active-print details
    if snapshot.file_name:
        fields: list[dict[str, str]] = [
            {"type": "mrkdwn", "text": f"*File*\n{_plain(snapshot.file_name)}"},
        ]
        if snapshot.progress_percent is not None:
            bar = _progress_bar(snapshot.progress_percent)
            fields.append(
                {
                    "type": "mrkdwn",
                    "text": f"*Progress*\n{bar} {snapshot.progress_percent:.0f}%",
                }
            )
        if snapshot.time_remaining_s is not None:
            fields.append(
                {
                    "type": "mrkdwn",
                    "text": f"*ETA*\n{_format_duration(snapshot.time_remaining_s)} left",
                }
            )
        if snapshot.time_printing_s is not None:
            fields.append(
                {
                    "type": "mrkdwn",
                    "text": f"*Elapsed*\n{_format_duration(snapshot.time_printing_s)}",
                }
            )
        blocks.append({"type": "section", "fields": fields})

    # Filament line + mismatch warning
    filament_lines: list[str] = []
    if loaded:
        filament_lines.append(f":thread: *Loaded*: {_plain(loaded.name)}")
    else:
        filament_lines.append(":thread: *Loaded*: _not set_ — hit *Swap filament* to record")
    mismatch = _material_mismatch(loaded, snapshot.material)
    if mismatch:
        filament_lines.append(f":warning: {mismatch}")
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(filament_lines)},
        }
    )

    # Error detail
    if snapshot.state == STATE_ERROR and snapshot.error_message:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":rotating_light: *Error*: {_plain(snapshot.error_message)}",
                },
            }
        )

    # Temperature line (compact)
    temps: list[str] = []
    if snapshot.nozzle_temp_c is not None:
        temps.append(f"nozzle {snapshot.nozzle_temp_c:.0f}°C")
    if snapshot.bed_temp_c is not None:
        temps.append(f"bed {snapshot.bed_temp_c:.0f}°C")
    if temps:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(temps)}],
            }
        )

    blocks.append(_actions_block(snapshot, is_tracking_label))
    blocks.append(_freshness_block(age_seconds))

    text = _fallback_text(snapshot, loaded)
    return StatusView(text=text, blocks=blocks)


def should_update(
    previous: PrinterSnapshot | None,
    current: PrinterSnapshot,
    last_progress_sent: float | None,
    last_eta_sent: int | None,
    seconds_since_last_update: float,
) -> bool:
    """Decide whether to call chat.update for this poll.

    Debounce: only push an update when something meaningful changed, so we
    don't burn rate limit on no-op edits. The freshness footer is itself a
    reason to update once per ~30s so the timestamp stays accurate.
    """

    if previous is None:
        return True
    if previous.state != current.state:
        return True
    if previous.online != current.online:
        return True
    if previous.job_key != current.job_key:
        return True
    if (previous.file_name or "") != (current.file_name or ""):
        return True
    if (previous.error_message or "") != (current.error_message or ""):
        return True

    if current.progress_percent is not None and last_progress_sent is not None:
        if abs(current.progress_percent - last_progress_sent) >= PROGRESS_DELTA_FOR_UPDATE:
            return True
    elif current.progress_percent is not None and last_progress_sent is None:
        return True

    if current.time_remaining_s is not None and last_eta_sent is not None:
        if abs(current.time_remaining_s - last_eta_sent) >= ETA_DELTA_SECONDS_FOR_UPDATE:
            return True

    # Periodic refresh so the "updated Xs ago" footer stays accurate.
    if seconds_since_last_update >= 30:
        return True

    return False


def _plain(text: str) -> str:
    """Make a user/printer-supplied string safe to drop into mrkdwn."""

    if text is None:
        return ""
    # Strip Slack mention/format markers so a malicious file name can't ping.
    sanitized = re.sub(r"<[!@#][^>]*>", "", str(text))
    sanitized = sanitized.replace("<", "‹").replace(">", "›")
    sanitized = sanitized.replace("*", "∗").replace("_", "ˍ").replace("`", "ˋ")
    return sanitized


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _progress_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "▓" * filled + "░" * (width - filled)


def _material_mismatch(loaded: Filament | None, reported: str | None) -> str | None:
    if not loaded or not reported:
        return None
    rep = reported.strip()
    if not rep:
        return None
    rep_token = rep.split()[0].casefold()
    name_norm = loaded.name.casefold()
    if rep_token and rep_token not in name_norm:
        return (
            f"Printer reports material *{_plain(reported)}*, but the loaded spool is "
            f"*{_plain(loaded.name)}*. Double-check before you wreck a print."
        )
    return None


def _fallback_text(snapshot: PrinterSnapshot, loaded: Filament | None) -> str:
    parts = [f"Printer: {snapshot.state.lower()}"]
    if snapshot.file_name:
        parts.append(f"file {snapshot.file_name}")
    if snapshot.progress_percent is not None:
        parts.append(f"{snapshot.progress_percent:.0f}%")
    if loaded:
        parts.append(f"loaded: {loaded.name}")
    return " · ".join(parts)


def _freshness_block(age_seconds: float | None) -> dict[str, Any]:
    if age_seconds is None:
        text = "updated just now"
    elif age_seconds < 60:
        text = f"updated {int(age_seconds)}s ago"
    elif age_seconds < 3600:
        text = f"updated {int(age_seconds // 60)}m ago"
    else:
        text = f"updated {int(age_seconds // 3600)}h ago"

    if age_seconds is not None and age_seconds >= DEGRADED_AFTER_SECONDS:
        text = f":warning: haven't heard from the printer in {int(age_seconds // 60)} min"
    elif age_seconds is not None and age_seconds >= STALE_AFTER_SECONDS:
        text = f":hourglass: {text} (stale)"

    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _actions_block(snapshot: PrinterSnapshot, is_tracking_label: str | None) -> dict[str, Any]:
    """Render the row of buttons that match the current state."""

    elements: list[dict[str, Any]] = []
    job_key = snapshot.job_key or ""

    if snapshot.has_active_job:
        label = is_tracking_label or "Track this print"
        elements.append(
            _button(
                action_id="toggle_track",
                text=label,
                value=job_key,
                style="primary" if not is_tracking_label else None,
            )
        )

    elements.append(
        _button(action_id="open_swap_filament", text="Swap filament", value="swap")
    )
    elements.append(
        _button(action_id="open_manage_inventory", text="Manage inventory", value="manage")
    )

    if snapshot.state == STATE_PRINTING:
        elements.append(_button(action_id="job_pause", text="Pause", value=job_key))
    elif snapshot.state == STATE_PAUSED:
        elements.append(_button(action_id="job_resume", text="Resume", value=job_key, style="primary"))

    if snapshot.state in {STATE_PRINTING, STATE_PAUSED}:
        elements.append(
            _button(
                action_id="job_cancel_confirm",
                text="Cancel",
                value=job_key,
                style="danger",
            )
        )

    return {"type": "actions", "elements": elements}


def _button(*, action_id: str, text: str, value: str, style: str | None = None) -> dict[str, Any]:
    btn: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "value": value or "_",
    }
    if style:
        btn["style"] = style
    return btn
