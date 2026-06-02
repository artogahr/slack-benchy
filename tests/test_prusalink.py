import httpx
import pytest
import respx

from prusa_slack_bot.prusalink import (
    STATE_FINISHED,
    STATE_IDLE,
    STATE_PRINTING,
    PrusaLinkAuthError,
    PrusaLinkClient,
    PrusaLinkUnreachable,
)


BASE = "http://printer.test"


@pytest.fixture
async def client():
    c = PrusaLinkClient(BASE, api_key="secret")
    await c.open()
    try:
        yield c
    finally:
        await c.close()


@respx.mock
async def test_v1_status_idle_parsed(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer": {
                    "state": "IDLE",
                    "temp_nozzle": 28.4,
                    "temp_bed": 24.1,
                },
                "job": None,
            },
        )
    )
    snap = await client.get_snapshot()
    assert snap.online is True
    assert snap.state == STATE_IDLE
    assert snap.job_key is None
    assert snap.nozzle_temp_c == pytest.approx(28.4)
    assert snap.bed_temp_c == pytest.approx(24.1)
    assert snap.has_active_job is False


@respx.mock
async def test_v1_status_printing_with_job(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer": {"state": "PRINTING", "temp_nozzle": 215.0, "temp_bed": 60.0},
                "job": {
                    "id": 17,
                    "progress": 42.3,
                    "time_remaining": 3600,
                    "time_printing": 1800,
                    "file": {"display_name": "Calicat.gcode"},
                },
            },
        )
    )
    snap = await client.get_snapshot()
    assert snap.state == STATE_PRINTING
    assert snap.file_name == "Calicat.gcode"
    assert snap.job_id == "17"
    assert snap.job_key == "Calicat.gcode::17"
    assert snap.progress_percent == pytest.approx(42.3)
    assert snap.time_remaining_s == 3600
    assert snap.has_active_job is True


@respx.mock
async def test_eta_negative_clamped_to_zero(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer": {"state": "PRINTING"},
                "job": {
                    "id": 1,
                    "progress": 99.0,
                    "time_remaining": -42,
                    "file": {"name": "m.gcode"},
                },
            },
        )
    )
    snap = await client.get_snapshot()
    assert snap.time_remaining_s == 0


@respx.mock
async def test_eta_absurdly_high_dropped(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer": {"state": "PRINTING"},
                "job": {
                    "id": 1,
                    "progress": 1.0,
                    "time_remaining": 999_999_999,
                    "file": {"name": "m.gcode"},
                },
            },
        )
    )
    snap = await client.get_snapshot()
    assert snap.time_remaining_s is None


@respx.mock
async def test_progress_out_of_range_dropped(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer": {"state": "PRINTING"},
                "job": {
                    "id": 1,
                    "progress": 250.0,
                    "file": {"name": "m.gcode"},
                },
            },
        )
    )
    snap = await client.get_snapshot()
    assert snap.progress_percent is None


@respx.mock
async def test_auth_failure_raises(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(401))
    with pytest.raises(PrusaLinkAuthError):
        await client.get_snapshot()


@respx.mock
async def test_v1_404_falls_back_to_legacy(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/printer").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": {
                    "text": "Operational",
                    "flags": {"operational": True, "printing": False, "ready": True},
                },
                "temperature": {
                    "tool0": {"actual": 27.2, "target": 0.0},
                    "bed": {"actual": 24.0, "target": 0.0},
                },
            },
        )
    )
    respx.get(f"{BASE}/api/job").mock(
        return_value=httpx.Response(
            200,
            json={
                "job": {"file": {"name": "idle.gcode"}},
                "progress": {"completion": None, "printTime": None, "printTimeLeft": None},
                "state": "Operational",
            },
        )
    )
    snap = await client.get_snapshot()
    assert snap.state == STATE_IDLE
    assert snap.nozzle_temp_c == pytest.approx(27.2)


@respx.mock
async def test_v1_legacy_state_finished_from_flag(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/printer").mock(
        return_value=httpx.Response(
            200,
            json={"state": {"text": "Operational", "flags": {"finished": True}}},
        )
    )
    respx.get(f"{BASE}/api/job").mock(return_value=httpx.Response(200, json={}))
    snap = await client.get_snapshot()
    assert snap.state == STATE_FINISHED


@respx.mock
async def test_timeout_becomes_unreachable(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(PrusaLinkUnreachable):
        await client.get_snapshot()


@respx.mock
async def test_connect_refused_becomes_unreachable(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/status").mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(PrusaLinkUnreachable):
        await client.get_snapshot()


@respx.mock
async def test_pause_hits_v1(client: PrusaLinkClient):
    route = respx.post(f"{BASE}/api/v1/job/17/pause").mock(return_value=httpx.Response(204))
    await client.pause("17")
    assert route.called


@respx.mock
async def test_pause_v1_fail_falls_back_to_legacy(client: PrusaLinkClient):
    respx.post(f"{BASE}/api/v1/job/17/pause").mock(return_value=httpx.Response(404))
    legacy = respx.post(f"{BASE}/api/job").mock(return_value=httpx.Response(204))
    await client.pause("17")
    assert legacy.called


@respx.mock
async def test_cancel_tries_v1_delete_then_legacy(client: PrusaLinkClient):
    v1 = respx.delete(f"{BASE}/api/v1/job/17").mock(return_value=httpx.Response(404))
    legacy = respx.post(f"{BASE}/api/job").mock(return_value=httpx.Response(204))
    await client.cancel("17")
    assert v1.called and legacy.called


@respx.mock
async def test_api_key_header_sent():
    c = PrusaLinkClient(BASE, api_key="topsecret")
    await c.open()
    try:
        route = respx.get(f"{BASE}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"printer": {"state": "IDLE"}, "job": None})
        )
        await c.get_snapshot()
        assert route.calls.last.request.headers["X-Api-Key"] == "topsecret"
    finally:
        await c.close()


@respx.mock
async def test_snapshot_image_tries_endpoints_and_returns_none_when_absent(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/cameras/snap").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/api/v1/camera/snap").mock(return_value=httpx.Response(404))
    assert await client.fetch_snapshot_image() is None


@respx.mock
async def test_snapshot_image_returns_bytes(client: PrusaLinkClient):
    respx.get(f"{BASE}/api/v1/cameras/snap").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n...")
    )
    img = await client.fetch_snapshot_image()
    assert img is not None and img.startswith(b"\x89PNG")
