from pathlib import Path

import pytest

from slack_benchy.db import Database, DuplicateFilament, InventoryFull
from slack_benchy.sanitize import MAX_INVENTORY


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(tmp_path / "test.sqlite3")
    await d.open()
    try:
        yield d
    finally:
        await d.close()


async def test_kv_roundtrip(db: Database):
    assert await db.kv_get("missing") is None
    await db.kv_set("status_ts", "1700000000.000")
    assert await db.kv_get("status_ts") == "1700000000.000"
    await db.kv_set("status_ts", "1700000001.000")
    assert await db.kv_get("status_ts") == "1700000001.000"


async def test_kv_json_roundtrip(db: Database):
    payload = {"state": "PRINTING", "progress": 42}
    await db.kv_set_json("last_state", payload)
    assert await db.kv_get_json("last_state") == payload


async def test_filament_add_and_list(db: Database):
    a = await db.add_filament("PLA Galaxy Black")
    b = await db.add_filament("PETG Clear")
    items = await db.list_filaments()
    assert {f.name for f in items} == {a.name, b.name}


async def test_filament_dedupe_case_insensitive(db: Database):
    await db.add_filament("PLA Black")
    with pytest.raises(DuplicateFilament):
        await db.add_filament("pla  black")


async def test_filament_inventory_cap(db: Database):
    for i in range(MAX_INVENTORY):
        await db.add_filament(f"Filament {i}")
    with pytest.raises(InventoryFull):
        await db.add_filament("one too many")


async def test_set_loaded_filament_is_exclusive(db: Database):
    a = await db.add_filament("PLA Black")
    b = await db.add_filament("PETG Clear")
    await db.set_loaded_filament(a.id)
    loaded = await db.get_loaded_filament()
    assert loaded is not None and loaded.name == "PLA Black"

    await db.set_loaded_filament(b.id)
    loaded = await db.get_loaded_filament()
    assert loaded is not None and loaded.name == "PETG Clear"

    await db.set_loaded_filament(None)
    assert await db.get_loaded_filament() is None


async def test_remove_filament(db: Database):
    f = await db.add_filament("PLA Black")
    await db.remove_filament(f.id)
    assert await db.list_filaments() == []


async def test_trackers_idempotent(db: Database):
    first = await db.add_tracker("U1", "job-42")
    second = await db.add_tracker("U1", "job-42")
    assert first is True
    assert second is False
    assert await db.trackers_for("job-42") == ["U1"]


async def test_trackers_for_returns_in_join_order(db: Database):
    await db.add_tracker("U1", "job-42")
    await db.add_tracker("U2", "job-42")
    await db.add_tracker("U3", "job-42")
    assert await db.trackers_for("job-42") == ["U1", "U2", "U3"]


async def test_clear_trackers_only_targeted_job(db: Database):
    await db.add_tracker("U1", "job-42")
    await db.add_tracker("U1", "job-43")
    await db.clear_trackers("job-42")
    assert await db.trackers_for("job-42") == []
    assert await db.trackers_for("job-43") == ["U1"]
