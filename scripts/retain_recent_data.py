"""Keep dashboard data for the last seven calendar days, preserving IV Rank.

Date folders and cache entries older than the cutoff are removed. The IV Rank
series is archived by ticker before any option snapshots are pruned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
RETENTION_DAYS = 7
DATE_FORMAT = "%Y-%m-%d"


def cutoff_date(today: date | None = None) -> date:
    return (today or datetime.now(LOCAL_TZ).date()) - timedelta(days=RETENTION_DAYS - 1)


def _dated_directories(root: Path, prefix: str, cutoff: str) -> list[Path]:
    found = []
    for path in root.glob(prefix + "*"):
        if not path.is_dir():
            continue
        value = path.name.removeprefix(prefix)
        try:
            parsed = datetime.strptime(value, DATE_FORMAT).date()
        except ValueError:
            continue
        if parsed.isoformat() < cutoff:
            found.append(path)
    return sorted(found)


def _cache_date(path: Path) -> str | None:
    if path.parent.parent.name == "intraday":
        return path.name[:10]
    if path.parent.parent.name != "snapshot":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = payload.get("state") or {}
        return (state.get("session") or {}).get("trading_date") or (state.get("latest_summary") or {}).get("requested_snapshot_date")
    except (OSError, ValueError, TypeError):
        return None


def _volume_date(path: Path) -> str | None:
    match = re.search(r"_volume_(\d{8})_", path.name)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = payload.get("generated_at") or payload.get("source_timestamp")
        return str(stamp)[:10] if stamp else None
    except (OSError, ValueError, TypeError):
        return None


def _archive_iv_rank(options_root: Path) -> dict[str, int]:
    # Import lazily: live_server already owns the ATM-IV logic used by the panel.
    import live_server

    tickers = {
        path.name.split("_", 1)[0].upper()
        for path in options_root.glob("20*/*_summary.json")
        if "_" in path.name
    }
    tickers.update(path.stem.upper() for path in (options_root / "iv_rank").glob("*.json"))
    counts = {}
    original_root = live_server.DATA_ROOT
    original_archive = live_server.IV_RANK_ARCHIVE_ROOT
    try:
        live_server.DATA_ROOT = options_root
        live_server.IV_RANK_ARCHIVE_ROOT = options_root / "iv_rank"
        for ticker in sorted(tickers):
            rows = live_server.build_iv_rank_history_rows(ticker)
            if rows:
                live_server.write_iv_rank_archive(ticker, rows)
                counts[ticker] = len(rows)
    finally:
        live_server.DATA_ROOT = original_root
        live_server.IV_RANK_ARCHIVE_ROOT = original_archive
    return counts


def _prune_intraday_table(path: Path, cutoff: str, apply: bool) -> int:
    if not path.is_file():
        return 0
    frame = pd.read_parquet(path)
    if "trading_date" not in frame:
        return 0
    keep = frame[frame["trading_date"].astype(str) >= cutoff]
    removed = len(frame) - len(keep)
    if apply and removed:
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".parquet", dir=str(path.parent))
        os.close(fd)
        try:
            keep.to_parquet(temp_name, index=False)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return removed


def _prune_daily_levels(path: Path, cutoff: str, apply: bool) -> int:
    if not path.is_file():
        return 0
    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(session_levels_daily)")}
        if "trading_date" not in columns:
            return 0
        old = conn.execute("SELECT COUNT(*) FROM session_levels_daily WHERE trading_date < ?", (cutoff,)).fetchone()[0]
        # Old rows retain only spot/average IV/rank for the IV Rank exception.
        clear_cols = [name for name in columns if name not in {"trading_date", "ticker", "spot", "avg_iv", "iv_rank"}]
        if apply and old and clear_cols:
            conn.execute(
                "UPDATE session_levels_daily SET " + ", ".join(f'"{name}"=NULL' for name in clear_cols)
                + " WHERE trading_date < ?", (cutoff,),
            )
            conn.commit()
        return old
    finally:
        conn.close()


def run_retention(*, apply: bool, root: Path = PROJECT_ROOT, today: date | None = None, verbose: bool = True) -> dict:
    cutoff = cutoff_date(today).isoformat()
    options_root = root / "data" / "options"
    market_root = root / "data" / "market"
    cache_root = root / "data" / "cache"
    old_options = _dated_directories(options_root, "", cutoff)
    old_market = [
        path
        for ticker_root in market_root.glob("ticker=*") if ticker_root.is_dir()
        for path in _dated_directories(ticker_root, "date=", cutoff)
    ]
    old_cache = []
    for category in ("intraday", "snapshot"):
        for path in (cache_root / category).glob("*/*.json"):
            cache_day = _cache_date(path)
            if cache_day is None or cache_day < cutoff:
                old_cache.append(path)
    trading_day_caches = list((cache_root / "data_store").glob("*.json"))
    old_volume = [
        path for path in (options_root / "volume").rglob("*.json")
        if (_volume_date(path) or "") < cutoff
    ]
    old_xynth = []
    for path in (root / "data" / "xynth").rglob("*.json"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
            day = path.stem
        else:
            try:
                day = str(json.loads(path.read_text(encoding="utf-8")).get("captured_at_utc") or "")[:10]
            except (OSError, ValueError):
                day = None
        if day and day < cutoff:
            old_xynth.append(path)
    intraday_path = market_root / "session_levels_intraday.parquet"
    sqlite_path = market_root / "session_levels_daily.sqlite"
    intraday_rows = _prune_intraday_table(intraday_path, cutoff, apply=False)
    old_sqlite_rows = _prune_daily_levels(sqlite_path, cutoff, apply=False)
    result = {
        "cutoff": cutoff,
        "options_dirs": len(old_options),
        "market_dirs": len(old_market),
        "cache_files": len(old_cache) + len(trading_day_caches),
        "volume_files": len(old_volume),
        "xynth_files": len(old_xynth),
        "intraday_rows": intraday_rows,
        "old_sqlite_rows_reduced_to_iv": old_sqlite_rows,
        "iv_rank_archived": {},
    }
    if verbose:
        print(("Pruning" if apply else "Would prune") + " dashboard data before " + cutoff + ": " + str(result))
    if not apply:
        return result
    if old_options:
        result["iv_rank_archived"] = _archive_iv_rank(options_root)
    _prune_intraday_table(intraday_path, cutoff, apply=True)
    _prune_daily_levels(sqlite_path, cutoff, apply=True)
    for path in old_options + old_market:
        shutil.rmtree(path)
    for path in old_cache + trading_day_caches + old_volume + old_xynth:
        path.unlink(missing_ok=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Remove old data (default: dry run)")
    args = parser.parse_args()
    run_retention(apply=args.apply)


if __name__ == "__main__":
    main()
