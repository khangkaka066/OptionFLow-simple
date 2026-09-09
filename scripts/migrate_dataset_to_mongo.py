#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import storage
from mongo_store import MongoDatasetStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "options"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate local option dataset files into MongoDB.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--ticker", default=None, help="Only migrate one ticker, e.g. QQQ.")
    parser.add_argument("--limit", type=int, default=None, help="Optional max snapshots to migrate.")
    return parser.parse_args()


def summary_paths(data_root: Path, ticker: str | None) -> list[Path]:
    pattern = f"{ticker.upper()}_*_*_summary.json" if ticker else "*_*_*_summary.json"
    paths = []
    for path in sorted(data_root.glob(f"*/{pattern}")):
        if len(path.stem.split("_")) == 4:
            paths.append(path)
    return paths


def by_strike_path_for(summary_path: Path) -> Path:
    return summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))


def reconciliation_path_for(summary_path: Path) -> Path:
    return summary_path.with_name(summary_path.name.replace("_summary.json", "_reconciliation.json"))


def migrate_snapshot(store: MongoDatasetStore, summary_path: Path) -> bool:
    parts = summary_path.stem.split("_")
    ticker = parts[0].upper()
    expiry = parts[1]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trading_date = summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or summary_path.parent.name

    reconciliation_path = reconciliation_path_for(summary_path)
    reconciliation = {}
    if reconciliation_path.exists():
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))

    store.upsert_snapshot(
        ticker=ticker,
        expiry=expiry,
        trading_date=trading_date,
        summary=summary,
        reconciliation=reconciliation,
    )

    by_strike_path = by_strike_path_for(summary_path)
    if not by_strike_path.exists():
        return True
    by_strike = pd.read_parquet(by_strike_path)
    if "expiry" not in by_strike:
        by_strike = by_strike.copy()
        by_strike["expiry"] = expiry
    expiry_scope = summary.get("expiry") or expiry
    strike_rows = by_strike.copy()
    for placeholder in ["net_vex", "net_chex"]:
        if placeholder not in strike_rows:
            strike_rows[placeholder] = None
    intraday_rows = storage.build_intraday_rows(
        strike_rows,
        summary,
        capture_ts=summary["snapshot_utc"],
        ticker=ticker,
        trading_date=trading_date,
    )
    level_row = {
        "trading_date": trading_date,
        "capture_ts": summary["snapshot_utc"],
        "ticker": ticker,
        "expiry_scope": expiry_scope,
        "spot": summary.get("spot"),
        "avg_iv": summary.get("avg_iv"),
        "gamma_flip": summary.get("gamma_flip"),
        "delta_flip": summary.get("delta_flip"),
        "gamma_wall": summary.get("gamma_wall_abs"),
        "dex_wall": summary.get("dex_wall_abs"),
        "call_resistance": summary.get("call_resistance"),
        "put_support": summary.get("put_support"),
    }
    store.upsert_market_dataset(
        ticker=ticker,
        trading_date=trading_date,
        expiry_scope=expiry_scope,
        summary=summary,
        by_strike=strike_rows,
        intraday_rows=intraday_rows,
        level_row=level_row,
    )
    return True


def main() -> None:
    args = parse_args()
    store = MongoDatasetStore.from_env()
    if store is None:
        raise SystemExit("Set MONGODB_URI before running this migration.")
    try:
        store.client.admin.command("ping")
    except Exception as exc:
        raise SystemExit(f"Cannot connect to MongoDB. Check MONGODB_URI, username, password, and Network Access. Error: {exc}")
    paths = summary_paths(args.data_root, args.ticker)
    if args.limit is not None:
        paths = paths[: args.limit]
    migrated = 0
    failed = 0
    for path in paths:
        try:
            if migrate_snapshot(store, path):
                migrated += 1
        except Exception as exc:
            failed += 1
            print(f"[mongo] failed {path}: {exc}", flush=True)
    print(f"Migrated {migrated} snapshots to MongoDB; failed={failed}.")


if __name__ == "__main__":
    main()
