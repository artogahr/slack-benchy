"""Slack Bolt app: Socket Mode handlers, status messenger, and notification sender."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from .config import Config
from .db import Database, DuplicateFilament, Filament, InventoryFull
from .prusalink import (
    STATE_PAUSED,
    STATE_PRINTING,
    PrinterSnapshot,
    PrusaLinkAuthError,
    PrusaLinkClient,
    PrusaLinkError,
    PrusaLinkUnreachable,
)
from .sanitize import MAX_INVENTORY, SanitizeError, sanitize_filament_name
from .status_message import render_status
from .transitions import TransitionEvent, TransitionKind

logger = logging.getLogger(__name__)


KV_STATUS_CHANNEL_ID = "status_channel_id"
KV_STATUS_MESSAGE_TS = "status_message_ts"
KV_LAST_SNAPSHOT = "last_snapshot_for_diff"
KV_LAST_PROGRESS_SENT = "last_progress_sent"
KV_LAST_ETA_SENT = "last_eta_sent"
KV_LAST_UPDATE_AT = "last_update_at"
KV_JOB_STARTER = "job_starter:"  # appended with job_key


class StatusMessenger:
    """Maintains the single live status message in the configured channel."""

    def __init__(self, app: AsyncApp, db: Database, channel_config: str):
        self._app = app
        self._db = db
        self._channel_config = channel_config
        self._channel_id: str | None = None
        self._ts: str | None = None
        self._lock = asyncio.Lock()

    @property
    def channel_id(self) -> str | None:
        return self._channel_id

    @property
    def message_ts(self) -> str | None:
        return self._ts

    async def initialize(self) -> None:
        """Resolve channel ID, find-or-create the status message, persist both."""

        self._channel_id = await self._resolve_channel(self._channel_config)
        if self._channel_id is None:
            raise RuntimeError(
                f"Couldn't find Slack channel {self._channel_config!r}. "
                "Invite the bot to the channel, or use a channel ID (Cxxxxxxxxxx)."
            )
        await self._db.kv_set(KV_STATUS_CHANNEL_ID, self._channel_id)

        stored = await self._db.kv_get(KV_STATUS_MESSAGE_TS)
        if stored:
            # Make sure the message still exists; if it was deleted manually,
            # we drop our reference and post a new one.
            ok = await self._verify_message_exists(self._channel_id, stored)
            if ok:
                self._ts = stored
                return

        ts = await self._post_initial_message(self._channel_id)
        self._ts = ts
        await self._db.kv_set(KV_STATUS_MESSAGE_TS, ts)

    async def update(self, snapshot: PrinterSnapshot, loaded: Filament | None, age_seconds: float) -> None:
        async with self._lock:
            if not self._channel_id or not self._ts:
                return
            view = render_status(snapshot, loaded, age_seconds)
            # Wrap the blocks in a legacy attachment so the `color` field
            # paints the left border. Still uses modern Block Kit inside.
            attachments = [{"color": view.color, "blocks": view.blocks}]
            try:
                await self._app.client.chat_update(
                    channel=self._channel_id,
                    ts=self._ts,
                    text=view.text,
                    attachments=attachments,
                    blocks=[],
                )
            except SlackApiError as exc:
                code = exc.response.get("error") if exc.response else "?"
                if code == "message_not_found":
                    new_ts = await self._post_initial_message(self._channel_id)
                    self._ts = new_ts
                    await self._db.kv_set(KV_STATUS_MESSAGE_TS, new_ts)
                else:
                    logger.warning("chat.update failed: %s", code)

    async def _resolve_channel(self, identifier: str) -> str | None:
        if identifier.startswith("C") and len(identifier) >= 9 and identifier[1:].isalnum():
            return identifier
        name = identifier.lstrip("#").lower()
        cursor: str | None = None
        for _ in range(20):  # safety bound
            resp = await self._app.client.conversations_list(
                types="public_channel,private_channel",
                cursor=cursor,
                limit=200,
            )
            for ch in resp.get("channels", []):
                if ch.get("name", "").lower() == name:
                    return ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
        return None

    async def _post_initial_message(self, channel_id: str) -> str:
        # Try to join the channel first so chat:write.public scope is honored.
        try:
            await self._app.client.conversations_join(channel=channel_id)
        except SlackApiError:
            pass

        resp = await self._app.client.chat_postMessage(
            channel=channel_id,
            text="Prusa bot is online. Spinning up the live status message…",
        )
        ts = resp["ts"]
        try:
            await self._app.client.pins_add(channel=channel_id, timestamp=ts)
        except SlackApiError:
            pass
        return ts

    async def _verify_message_exists(self, channel_id: str, ts: str) -> bool:
        try:
            resp = await self._app.client.conversations_history(
                channel=channel_id, latest=ts, oldest=ts, inclusive=True, limit=1
            )
            for msg in resp.get("messages", []):
                if msg.get("ts") == ts:
                    return True
        except SlackApiError as exc:
            logger.debug("verify message_exists failed: %s", exc)
        return False


class BotApp:
    """Bolt app + handlers + notification emitter."""

    def __init__(
        self,
        config: Config,
        db: Database,
        prusalink: PrusaLinkClient,
        get_current_snapshot,  # callable: () -> PrinterSnapshot | None
    ):
        self.config = config
        self.db = db
        self.prusalink = prusalink
        self._snapshot_fn = get_current_snapshot
        self.app = AsyncApp(token=config.slack_bot_token)
        self.messenger = StatusMessenger(self.app, db, config.status_channel)
        self._register_handlers()

    # public API

    async def initialize(self) -> None:
        await self.messenger.initialize()
        if self.config.filament_inventory_seed:
            await self._seed_inventory()

    async def update_status_message(self, snapshot: PrinterSnapshot, age_seconds: float) -> None:
        loaded = await self.db.get_loaded_filament()
        await self.messenger.update(snapshot, loaded, age_seconds)

    async def emit_events(self, events: list[TransitionEvent], snapshot: PrinterSnapshot) -> None:
        """Send DMs to trackers for each meaningful event, then clean up."""

        for event in events:
            if event.kind == TransitionKind.STARTED:
                # No DM on start (nobody opted in yet), but record starter slot.
                await self.db.record_job_event(
                    job_key=event.job_key,
                    file_name=event.file_name,
                    started_at=time.time(),
                    ended_at=None,
                    ended_state=None,
                )
                continue

            trackers = await self.db.trackers_for(event.job_key)
            for user_id in trackers:
                await self._dm_user(user_id, _event_to_dm_text(event))

            if event.kind in {
                TransitionKind.FINISHED,
                TransitionKind.CANCELLED,
                TransitionKind.ERROR,
            }:
                await self.db.clear_trackers(event.job_key)
                await self.db.kv_set(f"{KV_JOB_STARTER}{event.job_key}", "")
                await self.db.record_job_event(
                    job_key=event.job_key,
                    file_name=event.file_name,
                    started_at=None,
                    ended_at=time.time(),
                    ended_state=event.kind.value,
                )

    # registration

    def _register_handlers(self) -> None:
        self.app.action("toggle_track")(self._on_toggle_track)
        self.app.action("open_swap_filament")(self._on_open_swap_filament)
        self.app.action("open_manage_inventory")(self._on_open_manage_inventory)
        self.app.action("inventory_remove")(self._on_inventory_remove)
        self.app.action("job_pause")(self._on_job_pause)
        self.app.action("job_resume")(self._on_job_resume)
        self.app.action("job_cancel_confirm")(self._on_job_cancel_confirm)

        self.app.view("swap_filament_submit")(self._on_swap_filament_submit)
        self.app.view("add_filament_submit")(self._on_add_filament_submit)
        self.app.view("cancel_confirm_submit")(self._on_cancel_confirm_submit)

    # handlers

    async def _on_toggle_track(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        user_id = body["user"]["id"]
        job_key = body["actions"][0]["value"]
        snap = self._snapshot_fn()
        if not snap or snap.job_key != job_key or not snap.has_active_job:
            await self._respond_stale(client, body, "This print is no longer active.")
            return
        existing = await self.db.trackers_for(job_key)
        if user_id in existing:
            await self.db.remove_tracker(user_id, job_key)
            await self._ephemeral(client, body, "You won't get DMs for this print.")
        else:
            await self.db.add_tracker(user_id, job_key)
            # Record the first tracker as the "starter" for cancel-policy purposes.
            starter_key = f"{KV_JOB_STARTER}{job_key}"
            if not await self.db.kv_get(starter_key):
                await self.db.kv_set(starter_key, user_id)
            await self._ephemeral(
                client, body, "You'll get a DM when this print finishes, pauses, or errors."
            )
        # Refresh status so the button label flips.
        snap2 = self._snapshot_fn()
        if snap2:
            await self.update_status_message(snap2, 0.0)

    async def _on_open_swap_filament(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        inventory = await self.db.list_filaments()
        loaded = await self.db.get_loaded_filament()
        view = _swap_filament_view(inventory, loaded)
        await client.views_open(trigger_id=body["trigger_id"], view=view)

    async def _on_open_manage_inventory(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        inventory = await self.db.list_filaments()
        await client.views_open(
            trigger_id=body["trigger_id"], view=_manage_inventory_view(inventory)
        )

    async def _on_inventory_remove(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        try:
            filament_id = int(body["actions"][0]["value"])
        except (KeyError, ValueError):
            return
        await self.db.remove_filament(filament_id)
        inventory = await self.db.list_filaments()
        view_id = body["view"]["id"]
        await client.views_update(view_id=view_id, view=_manage_inventory_view(inventory))

    async def _on_job_pause(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        await self._do_job_command(
            client, body, expected_state=STATE_PRINTING, action="pause"
        )

    async def _on_job_resume(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        await self._do_job_command(
            client, body, expected_state=STATE_PAUSED, action="resume"
        )

    async def _on_job_cancel_confirm(self, ack, body, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        job_key = body["actions"][0]["value"]
        snap = self._snapshot_fn()
        if not snap or snap.job_key != job_key or not snap.has_active_job:
            await self._respond_stale(client, body, "This print is no longer active.")
            return

        if self.config.cancel_policy == "starter_only":
            starter = await self.db.kv_get(f"{KV_JOB_STARTER}{job_key}")
            if starter and starter != body["user"]["id"]:
                await self._ephemeral(
                    client,
                    body,
                    "Only the person who started tracking this print can cancel it.",
                )
                return

        await client.views_open(
            trigger_id=body["trigger_id"],
            view=_cancel_confirm_view(job_key, snap.file_name or "?"),
        )

    async def _on_swap_filament_submit(self, ack, body, view) -> None:  # type: ignore[no-untyped-def]
        await ack()
        state = view["state"]["values"]
        selected = state["filament_select_block"]["filament_select"].get("selected_option")
        if not selected:
            await self.db.set_loaded_filament(None)
        else:
            fid = int(selected["value"])
            await self.db.set_loaded_filament(fid)
        snap = self._snapshot_fn()
        if snap:
            await self.update_status_message(snap, 0.0)

    async def _on_add_filament_submit(self, ack, body, view, client) -> None:  # type: ignore[no-untyped-def]
        raw = view["state"]["values"]["new_name_block"]["new_name"].get("value", "")
        try:
            name = sanitize_filament_name(raw)
        except SanitizeError as exc:
            await ack(
                response_action="errors",
                errors={"new_name_block": str(exc)},
            )
            return
        try:
            await self.db.add_filament(name)
        except InventoryFull as exc:
            await ack(response_action="errors", errors={"new_name_block": str(exc)})
            return
        except DuplicateFilament as exc:
            await ack(
                response_action="errors",
                errors={
                    "new_name_block": f"Already in inventory as {exc.args[0]!r}."
                },
            )
            return
        await ack(response_action="clear")

    async def _on_cancel_confirm_submit(self, ack, body, view, client) -> None:  # type: ignore[no-untyped-def]
        await ack()
        job_key = view["private_metadata"]
        snap = self._snapshot_fn()
        if not snap or snap.job_key != job_key or not snap.has_active_job or not snap.job_id:
            await self._dm_user(
                body["user"]["id"],
                "Couldn't cancel: that print is no longer the active job.",
            )
            return
        try:
            await self.prusalink.cancel(snap.job_id)
            await self._dm_user(
                body["user"]["id"],
                f"Cancel sent for {snap.file_name or 'the current print'}.",
            )
        except PrusaLinkAuthError:
            await self._dm_user(body["user"]["id"], "Cancel failed: bad PrusaLink credentials.")
        except PrusaLinkUnreachable:
            await self._dm_user(body["user"]["id"], "Cancel failed: printer is unreachable.")
        except PrusaLinkError as exc:
            await self._dm_user(body["user"]["id"], f"Cancel failed: {exc}")

    # helpers

    async def _do_job_command(self, client, body, expected_state: str, action: str) -> None:  # type: ignore[no-untyped-def]
        job_key = body["actions"][0]["value"]
        snap = self._snapshot_fn()
        if not snap or snap.job_key != job_key:
            await self._respond_stale(client, body, "This print is no longer active.")
            return
        if snap.state != expected_state or not snap.job_id:
            await self._respond_stale(
                client, body, f"Can't {action} right now: printer is {snap.state.lower()}."
            )
            return
        try:
            if action == "pause":
                await self.prusalink.pause(snap.job_id)
            elif action == "resume":
                await self.prusalink.resume(snap.job_id)
        except PrusaLinkAuthError:
            await self._ephemeral(client, body, "Bad PrusaLink credentials.")
        except PrusaLinkUnreachable:
            await self._ephemeral(client, body, "Printer is unreachable right now.")
        except PrusaLinkError as exc:
            await self._ephemeral(client, body, f"{action.title()} failed: {exc}")

    async def _ephemeral(self, client, body, text: str) -> None:  # type: ignore[no-untyped-def]
        channel = body.get("channel", {}).get("id") or self.messenger.channel_id
        if not channel:
            return
        try:
            await client.chat_postEphemeral(channel=channel, user=body["user"]["id"], text=text)
        except SlackApiError as exc:
            logger.debug("ephemeral failed: %s", exc)

    async def _respond_stale(self, client, body, text: str) -> None:  # type: ignore[no-untyped-def]
        await self._ephemeral(client, body, text)

    async def _dm_user(self, user_id: str, text: str) -> None:
        try:
            im = await self.app.client.conversations_open(users=user_id)
            channel = im["channel"]["id"]
            await self.app.client.chat_postMessage(channel=channel, text=text)
        except SlackApiError as exc:
            logger.warning("DM to %s failed: %s", user_id, exc.response.get("error") if exc.response else exc)

    async def _seed_inventory(self) -> None:
        for raw in self.config.filament_inventory_seed:
            try:
                name = sanitize_filament_name(raw)
            except SanitizeError:
                continue
            try:
                await self.db.add_filament(name)
            except (DuplicateFilament, InventoryFull):
                pass


# Modal views

def _swap_filament_view(inventory: list[Filament], loaded: Filament | None) -> dict[str, Any]:
    options = [
        {
            "text": {"type": "plain_text", "text": (f.name[:75] or "(unnamed)")},
            "value": str(f.id),
        }
        for f in inventory[:99]
    ]
    if not options:
        return {
            "type": "modal",
            "callback_id": "swap_filament_submit",
            "title": {"type": "plain_text", "text": "Swap filament"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Your inventory is empty. Open *Manage inventory* and add a spool first.",
                    },
                }
            ],
        }

    initial = None
    if loaded:
        for opt in options:
            if opt["value"] == str(loaded.id):
                initial = opt
                break

    select: dict[str, Any] = {
        "type": "static_select",
        "action_id": "filament_select",
        "placeholder": {"type": "plain_text", "text": "Pick a spool"},
        "options": options,
    }
    if initial:
        select["initial_option"] = initial

    return {
        "type": "modal",
        "callback_id": "swap_filament_submit",
        "title": {"type": "plain_text", "text": "Swap filament"},
        "submit": {"type": "plain_text", "text": "Set loaded"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "filament_select_block",
                "label": {"type": "plain_text", "text": "Which spool is loaded now?"},
                "element": select,
                "optional": True,
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Note: this is bookkeeping — what *you say* is loaded. "
                            "PrusaLink can't tell us which exact spool is on the spindle."
                        ),
                    }
                ],
            },
        ],
    }


def _manage_inventory_view(inventory: list[Filament]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Inventory* ({len(inventory)} / {MAX_INVENTORY})",
            },
        }
    ]
    if not inventory:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No spools yet. Add one below._",
                },
            }
        )
    for f in inventory:
        label = f.name + (" ✓ loaded" if f.is_loaded else "")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"• {label}"},
                "accessory": {
                    "type": "button",
                    "action_id": "inventory_remove",
                    "text": {"type": "plain_text", "text": "Remove"},
                    "style": "danger",
                    "value": str(f.id),
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Remove this spool?"},
                        "text": {"type": "plain_text", "text": f"Remove {f.name}?"},
                        "confirm": {"type": "plain_text", "text": "Remove"},
                        "deny": {"type": "plain_text", "text": "Keep"},
                    },
                },
            }
        )
    blocks.append({"type": "divider"})

    return {
        "type": "modal",
        "callback_id": "add_filament_submit",
        "title": {"type": "plain_text", "text": "Manage inventory"},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": blocks + [
            {
                "type": "input",
                "block_id": "new_name_block",
                "label": {"type": "plain_text", "text": "Add a new spool"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "new_name",
                    "max_length": 64,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. Prusament PETG Anthracite Grey",
                    },
                },
            },
        ],
    }


def _cancel_confirm_view(job_key: str, file_name: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "cancel_confirm_submit",
        "private_metadata": job_key,
        "title": {"type": "plain_text", "text": "Cancel print?"},
        "submit": {"type": "plain_text", "text": "Yes, cancel"},
        "close": {"type": "plain_text", "text": "Keep printing"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"You're about to cancel *{file_name}*.\n\n"
                        ":warning: This stops the print and cannot be undone."
                    ),
                },
            }
        ],
    }


# DM text helpers

def _event_to_dm_text(event: TransitionEvent) -> str:
    file_part = f" ({event.file_name})" if event.file_name else ""
    if event.kind is TransitionKind.FINISHED:
        return f":white_check_mark: Print finished{file_part}."
    if event.kind is TransitionKind.PAUSED:
        return f":double_vertical_bar: Print paused{file_part}."
    if event.kind is TransitionKind.RESUMED:
        return f":arrow_forward: Print resumed{file_part}."
    if event.kind is TransitionKind.ERROR:
        detail = f" — {event.detail}" if event.detail else ""
        return f":rotating_light: Printer error{file_part}{detail}."
    if event.kind is TransitionKind.CANCELLED:
        return f":octagonal_sign: Print cancelled{file_part}."
    return f"Event: {event.kind.value}{file_part}"
