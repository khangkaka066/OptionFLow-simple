#!/usr/bin/env python3
"""Backfill normalized history stores from existing timestamped snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import storage  # noqa: E402


DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "options"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill options history/*.parquet stores from saved snapshots.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", default=None, help="Only backfill one data/options/YYYY-MM-DD folder.")
    return parser.parse_args()


def timestamp_from_summary_path(path: Path, ticker: str, expiry: str) -> str | None:
    prefix = f"{ticker}_{expiry}_"
    suffix = "_summary.json"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    ts = name[len(prefix) : -len(suffix)]
    return ts if ts and ts.isdigit() else None


def raw_paths_for(output_dir: Path, ticker: str, expiry: str, ts: str) -> tuple[Path, Path] | None:
    cboe_path = output_dir / "raw" / f"{ticker}_{expiry}_{ts}_cboe.json"
    yahoo_path = output_dir / "raw" / f"{ticker}_{expiry}_{ts}_yahoo.json"
    if cboe_path.exists() and yahoo_path.exists():
        return cboe_path, yahoo_path
    return None


def snapshot_rows(summary_path: Path, ticker: str) -> tuple[str, dict, pd.DataFrame] | None:
    output_dir = summary_path.parent
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    expiry = str(summary.get("expiry") or "")
    if not expiry:
        return None
    ts = timestamp_from_summary_path(summary_path, ticker, expiry)
    if not ts:
        return None

    by_strike_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))
    if not by_strike_path.exists():
        return None
    by_strike = pd.read_parquet(by_strike_path)

    reconciliation_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_reconciliation.json"))
    reconciliation = {}
    if reconciliation_path.exists():
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))

    snapshot_paths = {
        "by_strike": by_strike_path,
        "summary": summary_path,
        "reconciliation": reconciliation_path if reconciliation_path.exists() else summary_path,
    }
    raw_paths = raw_paths_for(output_dir, ticker, expiry, ts)

    summary_row = {key: storage._json_safe(value) for key, value in summary.items()}
    summary_row.update(
        {
            "snapshot_date": summary.get("requested_snapshot_date") or output_dir.name,
            "timestamp": ts,
        }
    )
    if raw_paths:
        summary_row["raw_cboe_path"] = str(raw_paths[0].relative_to(output_dir))
        summary_row["raw_yahoo_path"] = str(raw_paths[1].relative_to(output_dir))
    summary_row["by_strike_path"] = str(snapshot_paths["by_strike"].relative_to(output_dir))
    summary_row["summary_path"] = str(snapshot_paths["summary"].relative_to(output_dir))
    summary_row["reconciliation_path"] = str(snapshot_paths["reconciliation"].relative_to(output_dir))
    for key, value in reconciliation.items():
        summary_row[f"recon_{key}"] = storage._json_safe(value)

    strike_rows = by_strike.copy()
    strike_rows.insert(0, "snapshot_date", summary_row["snapshot_date"])
    strike_rows.insert(0, "snapshot_utc", summary["snapshot_utc"])
    strike_rows.insert(0, "expiry", expiry)
    strike_rows.insert(0, "ticker", ticker)
    return expiry, summary_row, strike_rows


def main() -> None:
    args = parse_args()
    ticker = args.ticker.upper()
    roots = [args.data_root / args.date] if args.date else sorted(path for path in args.data_root.iterdir() if path.is_dir())
    count = 0
    for output_dir in roots:
        grouped: dict[str, tuple[list[dict], list[pd.DataFrame]]] = {}
        for summary_path in sorted(output_dir.glob(f"{ticker}_*_*_summary.json")):
            result = snapshot_rows(summary_path, ticker)
            if result is None:
                continue
            expiry, summary_row, strike_rows = result
            summary_rows, strike_frames = grouped.setdefault(expiry, ([], []))
            summary_rows.append(summary_row)
            strike_frames.append(strike_rows)
            count += 1
        for expiry, (summary_rows, strike_frames) in grouped.items():
            history_dir = output_dir / "history"
            storage._upsert_parquet(
                history_dir / f"{ticker}_{expiry}_snapshots.parquet",
                pd.DataFrame(summary_rows),
                ["snapshot_utc", "ticker", "expiry"],
            )
            storage._upsert_parquet(
                history_dir / f"{ticker}_{expiry}_by_strike_history.parquet",
                pd.concat(strike_frames, ignore_index=True),
                ["snapshot_utc", "ticker", "expiry", "strike"],
            )
    print(f"Backfilled {count} snapshots into history stores.")


if __name__ == "__main__":
    main()
