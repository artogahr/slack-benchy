"""PrusaLink HTTP client.

Supports v1 endpoints with a legacy-endpoint fallback, X-Api-Key and HTTP
Digest auth, request timeouts, and a normalized snapshot returned to callers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PrusaLinkError(RuntimeError):
    """Base error for PrusaLink calls."""


class PrusaLinkAuthError(PrusaLinkError):
    """Raised when authentication fails (401/403)."""


class PrusaLinkUnreachable(PrusaLinkError):
    """Raised when the printer is not answering (timeout, refused, DNS)."""


# Normalized state set the rest of the bot reasons about.
STATE_IDLE = "IDLE"
STATE_PRINTING = "PRINTING"
STATE_PAUSED = "PAUSED"
STATE_FINISHED = "FINISHED"
STATE_ATTENTION = "ATTENTION"
STATE_ERROR = "ERROR"
STATE_STOPPED = "STOPPED"
STATE_BUSY = "BUSY"
STATE_OFFLINE = "OFFLINE"
STATE_UNKNOWN = "UNKNOWN"

_TERMINAL_JOB_STATES = {STATE_FINISHED, STATE_STOPPED}
_ACTIVE_PRINT_STATES = {STATE_PRINTING, STATE_PAUSED, STATE_ATTENTION}


@dataclass(frozen=True)
class PrinterSnapshot:
    online: bool
    state: str
    job_id: str | None
    job_key: str | None
    file_name: str | None
    progress_percent: float | None
    time_remaining_s: int | None
    time_printing_s: int | None
    material: str | None
    nozzle_temp_c: float | None
    bed_temp_c: float | None
    error_message: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_active_job(self) -> bool:
        return self.state in _ACTIVE_PRINT_STATES and self.job_key is not None


def _coerce_state(raw: str | None) -> str:
    if not raw:
        return STATE_UNKNOWN
    s = str(raw).strip().upper().replace(" ", "_")
    mapping = {
        "OPERATIONAL": STATE_IDLE,
        "READY": STATE_IDLE,
        "IDLE": STATE_IDLE,
        "FINISHED": STATE_FINISHED,
        "CANCEL_FINISHED": STATE_STOPPED,
        "STOPPED": STATE_STOPPED,
        "PRINTING": STATE_PRINTING,
        "PAUSED": STATE_PAUSED,
        "PAUSING": STATE_PAUSED,
        "RESUMING": STATE_PRINTING,
        "ATTENTION": STATE_ATTENTION,
        "ERROR": STATE_ERROR,
        "BUSY": STATE_BUSY,
    }
    return mapping.get(s, s)


def _clamp_seconds(value: Any) -> int | None:
    """Sanity-bound a time-remaining value. Negative is clamped to 0, absurd is dropped."""

    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return 0
    if n > 30 * 24 * 3600:
        return None
    return n


def _clamp_progress(value: Any) -> float | None:
    if value is None:
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if p < 0 or p > 100:
        return None
    return p


def _job_key(file_name: str | None, job_id: str | None) -> str | None:
    """Stable identifier for a print across polls.

    PrusaLink job ids can recycle across reboots, and may not always be present.
    Combining file name with id gives us a workable fingerprint without us
    having to persist a guess of when a print "really" started.
    """

    if not file_name and not job_id:
        return None
    return f"{file_name or '?'}::{job_id or '?'}"


class PrusaLinkClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._base = base_url.rstrip("/")
        self._headers = {"Accept": "application/json"}
        if api_key:
            self._headers["X-Api-Key"] = api_key
        self._auth: httpx.Auth | None = None
        if not api_key and username and password:
            self._auth = httpx.DigestAuth(username, password)
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> PrusaLinkClient:
        await self.open()
        return self

    async def __aexit__(self, *_) -> None:  # type: ignore[no-untyped-def]
        await self.close()

    async def open(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            auth=self._auth,
            timeout=self._timeout,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str) -> httpx.Response:
        assert self._client is not None, "Client not opened"
        try:
            return await self._client.get(path)
        except httpx.TimeoutException as exc:
            raise PrusaLinkUnreachable(f"Timed out reaching {path}") from exc
        except httpx.ConnectError as exc:
            raise PrusaLinkUnreachable(f"Cannot connect to printer ({exc})") from exc
        except httpx.RequestError as exc:
            raise PrusaLinkUnreachable(f"Network error reaching {path}: {exc}") from exc

    async def _post(self, path: str) -> httpx.Response:
        assert self._client is not None, "Client not opened"
        try:
            return await self._client.post(path)
        except httpx.TimeoutException as exc:
            raise PrusaLinkUnreachable(f"Timed out posting to {path}") from exc
        except httpx.ConnectError as exc:
            raise PrusaLinkUnreachable(f"Cannot connect to printer ({exc})") from exc
        except httpx.RequestError as exc:
            raise PrusaLinkUnreachable(f"Network error posting to {path}: {exc}") from exc

    @staticmethod
    def _check_auth(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise PrusaLinkAuthError(
                f"PrusaLink rejected the credentials (HTTP {resp.status_code}). "
                "Check PRUSALINK_API_KEY, or PRUSALINK_USERNAME/PASSWORD if you use Digest auth."
            )

    async def get_snapshot(self) -> PrinterSnapshot:
        """Fetch the current printer + job snapshot, normalized."""

        v1 = await self._try_v1_snapshot()
        if v1 is not None:
            return v1
        return await self._legacy_snapshot()

    async def _try_v1_snapshot(self) -> PrinterSnapshot | None:
        resp = await self._get("/api/v1/status")
        self._check_auth(resp)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            logger.debug("v1 status returned %s, will try legacy", resp.status_code)
            return None

        try:
            data = resp.json()
        except ValueError as exc:
            raise PrusaLinkError("PrusaLink returned non-JSON on /api/v1/status") from exc

        printer = data.get("printer") or {}
        job = data.get("job") or {}
        state = _coerce_state(printer.get("state"))

        file_name = None
        job_id = None
        if job:
            file_obj = job.get("file") or {}
            file_name = file_obj.get("display_name") or file_obj.get("name")
            jid = job.get("id")
            job_id = str(jid) if jid is not None else None

        # If the job summary in /status is thin, ask /job directly.
        if state in _ACTIVE_PRINT_STATES and (file_name is None or not job):
            job_resp = await self._get("/api/v1/job")
            self._check_auth(job_resp)
            if job_resp.status_code < 400:
                try:
                    job_data = job_resp.json()
                except ValueError:
                    job_data = {}
                file_obj = job_data.get("file") or {}
                file_name = file_name or file_obj.get("display_name") or file_obj.get("name")
                jid = job_data.get("id")
                job_id = job_id or (str(jid) if jid is not None else None)
                job = {**job_data, **job}

        return PrinterSnapshot(
            online=True,
            state=state,
            job_id=job_id,
            job_key=_job_key(file_name, job_id),
            file_name=file_name,
            progress_percent=_clamp_progress(job.get("progress")),
            time_remaining_s=_clamp_seconds(job.get("time_remaining")),
            time_printing_s=_clamp_seconds(job.get("time_printing")),
            material=(printer.get("material") or {}).get("material")
            if isinstance(printer.get("material"), dict)
            else printer.get("material"),
            nozzle_temp_c=_safe_float(printer.get("temp_nozzle")),
            bed_temp_c=_safe_float(printer.get("temp_bed")),
            error_message=printer.get("error"),
            raw=data,
        )

    async def _legacy_snapshot(self) -> PrinterSnapshot:
        """Older firmware: /api/printer + /api/job."""

        p_resp = await self._get("/api/printer")
        self._check_auth(p_resp)
        if p_resp.status_code >= 400:
            raise PrusaLinkError(
                f"PrusaLink /api/printer returned HTTP {p_resp.status_code}; "
                "this firmware may not be supported."
            )
        try:
            p_data = p_resp.json()
        except ValueError as exc:
            raise PrusaLinkError("PrusaLink returned non-JSON on /api/printer") from exc

        state_obj = p_data.get("state") or {}
        state_flags = state_obj.get("flags") or {}
        state_text = state_obj.get("text") or ""
        state = _state_from_legacy_flags(state_flags, state_text)

        j_resp = await self._get("/api/job")
        self._check_auth(j_resp)
        j_data: dict[str, Any] = {}
        if j_resp.status_code < 400:
            try:
                j_data = j_resp.json()
            except ValueError:
                j_data = {}

        job = j_data.get("job") or {}
        progress = j_data.get("progress") or {}
        file_obj = job.get("file") or {}
        file_name = file_obj.get("display") or file_obj.get("name")
        material = job.get("filament", {}).get("name") if isinstance(job.get("filament"), dict) else None
        job_id = str(j_data.get("id")) if j_data.get("id") is not None else None

        temps = (p_data.get("temperature") or {})
        nozzle = _safe_float((temps.get("tool0") or {}).get("actual"))
        bed = _safe_float((temps.get("bed") or {}).get("actual"))

        return PrinterSnapshot(
            online=True,
            state=state,
            job_id=job_id,
            job_key=_job_key(file_name, job_id),
            file_name=file_name,
            progress_percent=_clamp_progress(progress.get("completion")),
            time_remaining_s=_clamp_seconds(progress.get("printTimeLeft")),
            time_printing_s=_clamp_seconds(progress.get("printTime")),
            material=material,
            nozzle_temp_c=nozzle,
            bed_temp_c=bed,
            error_message=state_obj.get("error") if state == STATE_ERROR else None,
            raw={"printer": p_data, "job": j_data},
        )

    async def pause(self, job_id: str) -> None:
        await self._job_command(job_id, "pause")

    async def resume(self, job_id: str) -> None:
        await self._job_command(job_id, "resume")

    async def cancel(self, job_id: str) -> None:
        # PrusaLink uses DELETE on /api/v1/job/{id} for cancel in current firmwares;
        # older versions accept POST /api/job with a stop command. Try both.
        assert self._client is not None
        v1 = await self._client.delete(f"/api/v1/job/{job_id}")
        self._check_auth(v1)
        if v1.status_code < 400:
            return
        legacy = await self._client.post(
            "/api/job", json={"command": "cancel"}
        )
        self._check_auth(legacy)
        if legacy.status_code >= 400:
            raise PrusaLinkError(
                f"Cancel failed (v1 HTTP {v1.status_code}, legacy HTTP {legacy.status_code})."
            )

    async def _job_command(self, job_id: str, command: str) -> None:
        v1 = await self._post(f"/api/v1/job/{job_id}/{command}")
        self._check_auth(v1)
        if v1.status_code < 400:
            return
        # legacy fallback
        assert self._client is not None
        legacy = await self._client.post("/api/job", json={"command": command})
        self._check_auth(legacy)
        if legacy.status_code >= 400:
            raise PrusaLinkError(
                f"{command} failed (v1 HTTP {v1.status_code}, legacy HTTP {legacy.status_code})."
            )

    async def fetch_snapshot_image(self) -> bytes | None:
        """Return a current webcam image, or None if the printer has no camera."""

        for path in ("/api/v1/cameras/snap", "/api/v1/camera/snap"):
            assert self._client is not None
            try:
                resp = await self._client.get(path)
            except httpx.RequestError:
                continue
            if resp.status_code == 404:
                continue
            self._check_auth(resp)
            if resp.status_code < 400 and resp.content:
                return resp.content
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_from_legacy_flags(flags: dict[str, Any], text: str) -> str:
    if flags.get("error"):
        return STATE_ERROR
    if flags.get("printing"):
        return STATE_PRINTING
    if flags.get("paused"):
        return STATE_PAUSED
    if flags.get("finished"):
        return STATE_FINISHED
    if flags.get("ready") or flags.get("operational"):
        return STATE_IDLE
    return _coerce_state(text)
