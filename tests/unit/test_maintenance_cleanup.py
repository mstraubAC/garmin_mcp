"""Unit tests for the periodic SQLite cleanup task."""

import asyncio
import time

import pytest

from garmin_mcp.auth.storage import Storage
from garmin_mcp.maintenance.cleanup import cleanup_loop, tick_once


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "state.db")
    yield s
    s.close()


async def test_tick_once_returns_zero_counts_on_empty_db(storage):
    counts = await tick_once(storage)
    assert counts == {"clients": 0, "pending": 0}


async def test_tick_once_drops_old_unused_clients(storage):
    storage.register_client("old-unused", None, {}, None)
    storage._conn.execute(
        "UPDATE oauth_clients SET registered_at = ? WHERE client_id = ?",
        (int(time.time()) - 86400 * 2, "old-unused"),
    )
    storage.register_client("recent", None, {}, None)

    counts = await tick_once(storage, never_used_after=86400, idle_after=86400 * 90)
    assert counts["clients"] == 1
    assert storage.get_client("recent") is not None
    assert storage.get_client("old-unused") is None


async def test_tick_once_drops_expired_pending(storage):
    storage.store_pending_authorization(
        "exp",
        "c",
        "https://x/cb",
        None,
        "x",
        "S256",
        False,
        [],
        None,
        ttl_seconds=-1,
    )
    storage.store_pending_authorization(
        "ok",
        "c",
        "https://x/cb",
        None,
        "x",
        "S256",
        False,
        [],
        None,
        ttl_seconds=600,
    )
    counts = await tick_once(storage)
    assert counts["pending"] == 1


async def test_cleanup_loop_runs_until_cancelled(storage, monkeypatch):
    """The loop should sleep, tick, sleep, tick … and exit cleanly when
    cancelled."""
    ticks = 0

    async def fake_tick(*_a, **_kw):
        nonlocal ticks
        ticks += 1
        return {"clients": 0, "pending": 0}

    monkeypatch.setattr("garmin_mcp.maintenance.cleanup.tick_once", fake_tick)

    task = asyncio.create_task(cleanup_loop(storage, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ticks >= 2


async def test_cleanup_loop_swallows_tick_exceptions(storage, monkeypatch, caplog):
    """A failing tick must not break the loop."""
    calls = 0

    async def boom(*_a, **_kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk full")
        return {"clients": 0, "pending": 0}

    monkeypatch.setattr("garmin_mcp.maintenance.cleanup.tick_once", boom)

    task = asyncio.create_task(cleanup_loop(storage, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Tick was called more than once → loop kept running after the first failure
    assert calls >= 2
