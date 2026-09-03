from __future__ import annotations

from datetime import datetime, timedelta, timezone

from live_dashboard.cache import ResponseCache
from live_dashboard.intraday_file_cache import IntradayFileCache, session_is_closed
from live_dashboard.snapshot_file_cache import SnapshotFileCache


def closed_session() -> dict:
    close = datetime.now(timezone.utc) - timedelta(days=1)
    return {
        "trading_date": close.date().isoformat(),
        "market_open_utc": (close - timedelta(hours=6)).isoformat(),
        "market_close_utc": close.isoformat(),
        "collection_start_utc": (close - timedelta(hours=6, minutes=30)).isoformat(),
    }


def open_session() -> dict:
    close = datetime.now(timezone.utc) + timedelta(days=1)
    return {
        "trading_date": close.date().isoformat(),
        "market_open_utc": (close - timedelta(hours=6)).isoformat(),
        "market_close_utc": close.isoformat(),
        "collection_start_utc": (close - timedelta(hours=6, minutes=30)).isoformat(),
    }


def test_response_cache_ttl_and_delete() -> None:
    cache = ResponseCache(max_entries=2)
    cache.set("a", b"alpha")

    assert cache.get("a", ttl_seconds=60) == b"alpha"
    assert cache.get("a", ttl_seconds=-1) is None

    cache.set("b", b"bravo")
    cache.delete("b")
    assert cache.get("b", ttl_seconds=60) is None


def test_response_cache_evicts_oldest_entry() -> None:
    cache = ResponseCache(max_entries=2)
    cache.set("a", b"alpha")
    cache.set("b", b"bravo")
    cache.set("c", b"charlie")

    assert cache.get("a", ttl_seconds=60) is None
    assert cache.get("b", ttl_seconds=60) == b"bravo"
    assert cache.get("c", ttl_seconds=60) == b"charlie"


def test_intraday_file_cache_round_trip(tmp_path) -> None:
    cache = IntradayFileCache(tmp_path)
    points = [{"time": "2026-09-01T13:30:00+00:00", "spot": 100.0}]
    ribbon = [{"time": "2026-09-01T13:30:00+00:00", "rows": [{"strike": 100, "net_gex": 1.0}]}]

    cache.save(ticker="ndx", trading_date="2026-09-01", window=14, points=points, ribbon=ribbon)

    assert cache.load(ticker="NDX", trading_date="2026-09-01", window=14) == (points, ribbon)
    assert cache.load(ticker="NDX", trading_date="2026-09-01", window=10) is None


def test_snapshot_file_cache_only_saves_closed_sessions(tmp_path) -> None:
    cache = SnapshotFileCache(tmp_path)
    closed_state = {"session": closed_session(), "points": [], "gex_ribbon": []}
    open_state = {"session": open_session(), "points": [], "gex_ribbon": []}

    cache.save(ticker="QQQ", snapshot_id="history:closed#1", window=14, state=closed_state)
    cache.save(ticker="QQQ", snapshot_id="history:open#1", window=14, state=open_state)

    assert cache.load(ticker="QQQ", snapshot_id="history:closed#1", window=14) == closed_state
    assert cache.load(ticker="QQQ", snapshot_id="history:open#1", window=14) is None


def test_session_is_closed() -> None:
    assert session_is_closed(closed_session()) is True
    assert session_is_closed(open_session()) is False
