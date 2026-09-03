from __future__ import annotations

from datetime import datetime, timedelta, timezone

from live_dashboard.intraday_file_cache import IntradayFileCache
from live_dashboard.intraday_service import IntradayService


class FakeSnapshotService:
    def __init__(self) -> None:
        self.calls = 0

    def load_snapshot_state(self, snapshot_id: str, ticker: str, window: float, *, refresh: bool = False) -> dict:
        self.calls += 1
        return {
            "history_snapshot_id": snapshot_id,
            "points": [{"time": "base"}],
            "gex_ribbon": [{"time": "base", "rows": []}],
            "session": {},
            "candles": [],
            "candles_error": None,
            "refresh_seen": refresh,
        }


def closed_session() -> dict:
    close = datetime.now(timezone.utc) - timedelta(days=1)
    return {
        "trading_date": "2026-09-01",
        "market_open_utc": (close - timedelta(hours=6)).isoformat(),
        "market_close_utc": close.isoformat(),
        "collection_start_utc": (close - timedelta(hours=6, minutes=30)).isoformat(),
    }


def test_intraday_service_uses_file_cache_and_refresh_bypasses_it(tmp_path) -> None:
    seed_calls = {"count": 0}

    def seed_session_data(ticker: str, session: dict, window: float):
        seed_calls["count"] += 1
        return ([{"time": "seed", "spot": 100}], [{"time": "seed", "rows": []}])

    service = IntradayService(
        session_for_trading_date=lambda trading_date: closed_session(),
        seed_session_data=seed_session_data,
        latest_snapshot_id_for_trading_day=lambda day_id, ticker: "history:test#close",
        candles_for_session=lambda ticker, session: ([], None),
        snapshot_service=FakeSnapshotService(),
        file_cache=IntradayFileCache(tmp_path),
    )

    first = service.load_state("2026-09-01", "NDX", 14)
    second = service.load_state("2026-09-01", "NDX", 14)
    refreshed = service.load_state("2026-09-01", "NDX", 14, refresh=True)

    assert seed_calls["count"] == 2
    assert first["points"] == [{"time": "seed", "spot": 100}]
    assert second["points"] == [{"time": "seed", "spot": 100}]
    assert refreshed["refresh_seen"] is True
