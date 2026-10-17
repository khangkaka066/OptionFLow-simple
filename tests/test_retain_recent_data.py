from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd

from retain_recent_data import run_retention


def test_retention_keeps_seven_days_and_preserves_iv_fields(tmp_path, monkeypatch):
    options = tmp_path / "data" / "options"
    market = tmp_path / "data" / "market"
    cache = tmp_path / "data" / "cache"
    old_option = options / "2026-10-02"
    recent_option = options / "2026-10-03"
    old_option.mkdir(parents=True)
    recent_option.mkdir(parents=True)
    (old_option / "QQQ_2026-10-02_summary.json").write_text("{}")
    (recent_option / "QQQ_2026-10-03_summary.json").write_text("{}")
    old_market = market / "ticker=QQQ" / "date=2026-10-02"
    recent_market = market / "ticker=QQQ" / "date=2026-10-03"
    old_market.mkdir(parents=True)
    recent_market.mkdir(parents=True)
    (old_market / "raw_chain.parquet").write_bytes(b"old")
    (recent_market / "raw_chain.parquet").write_bytes(b"new")
    pd.DataFrame({"trading_date": ["2026-10-02", "2026-10-03"], "ticker": ["QQQ", "QQQ"]}).to_parquet(
        market / "session_levels_intraday.parquet", index=False,
    )
    conn = sqlite3.connect(market / "session_levels_daily.sqlite")
    conn.execute("CREATE TABLE session_levels_daily(trading_date TEXT, ticker TEXT, spot REAL, avg_iv REAL, iv_rank REAL, gamma_flip REAL)")
    conn.executemany("INSERT INTO session_levels_daily VALUES (?,?,?,?,?,?)", [
        ("2026-10-02", "QQQ", 700, 0.2, 50, 695),
        ("2026-10-03", "QQQ", 701, 0.21, 55, 696),
    ])
    conn.commit(); conn.close()
    old_cache = cache / "intraday" / "QQQ" / "2026-10-02_w14.json"
    old_cache.parent.mkdir(parents=True)
    old_cache.write_text(json.dumps({"old": True}))
    old_volume = options / "volume" / "history" / "QQQ_volume_20261002_101000.json"
    recent_volume = options / "volume" / "history" / "QQQ_volume_20261003_101000.json"
    old_volume.parent.mkdir(parents=True)
    old_volume.write_text("{}")
    recent_volume.write_text("{}")

    def archive(root):
        assert old_option.exists()  # Archive must happen before deletion.
        archive_dir = root / "iv_rank"
        archive_dir.mkdir()
        (archive_dir / "QQQ.json").write_text("[]")
        return {"QQQ": 1}

    monkeypatch.setattr("retain_recent_data._archive_iv_rank", archive)
    preview = run_retention(apply=False, root=tmp_path, today=date(2026, 10, 9), verbose=False)
    assert preview["cutoff"] == "2026-10-03"
    assert old_option.exists()
    result = run_retention(apply=True, root=tmp_path, today=date(2026, 10, 9), verbose=False)
    assert result["iv_rank_archived"] == {"QQQ": 1}
    assert not old_option.exists() and recent_option.exists()
    assert not old_market.exists() and recent_market.exists()
    assert not old_cache.exists()
    assert not old_volume.exists() and recent_volume.exists()
    assert (options / "iv_rank" / "QQQ.json").exists()
    assert pd.read_parquet(market / "session_levels_intraday.parquet")["trading_date"].tolist() == ["2026-10-03"]
    conn = sqlite3.connect(market / "session_levels_daily.sqlite")
    old = conn.execute("SELECT spot, avg_iv, iv_rank, gamma_flip FROM session_levels_daily WHERE trading_date='2026-10-02'").fetchone()
    recent = conn.execute("SELECT gamma_flip FROM session_levels_daily WHERE trading_date='2026-10-03'").fetchone()
    conn.close()
    assert old == (700, 0.2, 50, None)
    assert recent == (696,)


def test_iv_rank_history_survives_deleted_snapshot_directories(tmp_path, monkeypatch):
    import live_server

    options = tmp_path / "options"
    archive_dir = options / "iv_rank"
    archive_dir.mkdir(parents=True)
    archive_dir.joinpath("QQQ.json").write_text(json.dumps([
        {"date": "2026-10-01", "ticker": "QQQ", "snapshot_utc": "2026-10-01T20:00:00+00:00", "spot": 700, "avg_iv_pct": 20.0},
        {"date": "2026-10-02", "ticker": "QQQ", "snapshot_utc": "2026-10-02T20:00:00+00:00", "spot": 701, "avg_iv_pct": 22.0},
    ]))
    monkeypatch.setattr(live_server, "DATA_ROOT", options)
    monkeypatch.setattr(live_server, "IV_RANK_ARCHIVE_ROOT", archive_dir)
    rows = live_server.build_iv_rank_history_rows("QQQ")
    assert [row["date"] for row in rows] == ["2026-10-01", "2026-10-02"]
    assert rows[-1]["iv_rank_pct"] == 100.0
