from __future__ import annotations

import json

import pandas as pd
import pytest

from live_dashboard.data_store import DataStore
from live_dashboard.time_utils import NY_TZ, VN_TZ


def make_store(tmp_path) -> DataStore:
    return DataStore(tmp_path, ny_tz=NY_TZ, vn_tz=VN_TZ, cache_root=tmp_path / ".cache")


def test_history_snapshot_id_round_trip(tmp_path) -> None:
    history_dir = tmp_path / "2026-09-01" / "history"
    history_dir.mkdir(parents=True)
    path = history_dir / "QQQ_2026-09-01_snapshots.parquet"
    pd.DataFrame([{"ticker": "QQQ", "snapshot_utc": "2026-09-01T20:00:00+00:00"}]).to_parquet(path)
    store = make_store(tmp_path)

    snapshot_id = store.history_snapshot_id(path, "2026-09-01T20:00:00+00:00")

    assert snapshot_id.startswith("history:")
    assert store.parse_history_snapshot_id(snapshot_id) == (path.resolve(), "2026-09-01T20:00:00+00:00")


def test_summary_path_from_id_rejects_path_escape(tmp_path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError):
        store.summary_path_from_id("../outside_summary.json")


def test_list_trading_days_from_history_and_json(tmp_path) -> None:
    history_dir = tmp_path / "2026-09-01" / "history"
    history_dir.mkdir(parents=True)
    history_path = history_dir / "QQQ_2026-09-01_snapshots.parquet"
    pd.DataFrame(
        [
            {
                "ticker": "QQQ",
                "snapshot_utc": "2026-09-01T13:30:00+00:00",
                "expiry": "2026-09-01",
                "spot": 100.0,
            },
            {
                "ticker": "QQQ",
                "snapshot_utc": "2026-09-01T20:00:00+00:00",
                "expiry": "2026-09-01",
                "spot": 101.0,
            },
        ]
    ).to_parquet(history_path)
    day_dir = tmp_path / "2026-09-02"
    day_dir.mkdir()
    (day_dir / "QQQ_2026-09-02_summary.json").write_text(
        json.dumps({"snapshot_utc": "2026-09-02T20:00:00+00:00", "expiry": "2026-09-02"}),
        encoding="utf-8",
    )
    store = make_store(tmp_path)

    days = store.list_trading_days("QQQ")

    assert [day["trading_date"] for day in days] == ["2026-09-02", "2026-09-01"]
    assert days[1]["snapshot_count"] == 2
    assert days[1]["latest_snapshot_id"].startswith("history:")

def test_latest_snapshot_id_uses_unvalidated_cache_for_past_day(tmp_path, monkeypatch) -> None:
    history_dir = tmp_path / "2026-09-01" / "history"
    history_dir.mkdir(parents=True)
    history_path = history_dir / "QQQ_2026-09-01_snapshots.parquet"
    pd.DataFrame(
        [{"ticker": "QQQ", "snapshot_utc": "2026-09-01T20:00:00+00:00", "expiry": "2026-09-01"}]
    ).to_parquet(history_path)
    cache_root = tmp_path / ".cache"
    first_store = DataStore(tmp_path, ny_tz=NY_TZ, vn_tz=VN_TZ, cache_root=cache_root)
    assert first_store.list_trading_days("QQQ")[0]["trading_date"] == "2026-09-01"

    def fail_read_parquet(*args, **kwargs):
        raise AssertionError("past-day resolver should avoid parquet read when persistent cache exists")

    def fail_fingerprint(*args, **kwargs):
        raise AssertionError("past-day resolver should avoid source fingerprint when persistent cache exists")

    monkeypatch.setattr(pd, "read_parquet", fail_read_parquet)
    second_store = DataStore(tmp_path, ny_tz=NY_TZ, vn_tz=VN_TZ, cache_root=cache_root)
    monkeypatch.setattr(second_store, "_trading_days_source_fingerprint", fail_fingerprint)

    assert second_store.latest_snapshot_id_for_trading_day("day:2026-09-01", "QQQ").startswith("history:")
