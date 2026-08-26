#!/usr/bin/env python3
"""Backfill call_volume/put_volume from saved Yahoo raw JSON files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "options"
RAW_NAME_RE = re.compile(r"^(?P<ticker>.+)_(?P<expiry>\d{4}-\d{2}-\d{2}|ALL)_(?P<ts>\d{6})_yahoo\.json$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill option volume columns into by-strike and history stores.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", default=None, help="Only backfill one data/options/YYYY-MM-DD folder.")
    return parser.parse_args()


def volume_frame(raw_path: Path) -> pd.DataFrame | None:
    match = RAW_NAME_RE.match(raw_path.name)
    if not match:
        return None
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = []
    for side, out_col in [("calls", "call_volume"), ("puts", "put_volume")]:
        for row in raw.get(side, []):
            strike = pd.to_numeric(row.get("strike"), errors="coerce")
            volume = pd.to_numeric(row.get("volume"), errors="coerce")
            if pd.isna(strike):
                continue
            rows.append({"strike": float(strike), out_col: 0.0 if pd.isna(volume) else float(volume)})
    if not rows:
        return None
    frame = pd.DataFrame(rows).groupby("strike", as_index=False).sum()
    for col in ["call_volume", "put_volume"]:
        if col not in frame:
            frame[col] = 0.0
    return frame[["strike", "call_volume", "put_volume"]]


def patch_by_strike(path: Path, volumes: pd.DataFrame) -> pd.DataFrame | None:
    if not path.exists():
        return None
    data = pd.read_parquet(path)
    data = data.drop(columns=[col for col in ["call_volume", "put_volume"] if col in data.columns])
    data = data.merge(volumes, on="strike", how="left")
    data[["call_volume", "put_volume"]] = data[["call_volume", "put_volume"]].fillna(0.0)
    data.to_parquet(path, index=False)
    return data


def patch_history(output_dir: Path, ticker: str, expiry: str, updated: list[tuple[str, pd.DataFrame]]) -> None:
    history_path = output_dir / "history" / f"{ticker}_{expiry}_by_strike_history.parquet"
    if not history_path.exists() or not updated:
        return
    history = pd.read_parquet(history_path)
    history = history.drop(
        columns=[col for col in ["call_volume", "put_volume"] if col in history.columns],
        errors="ignore",
    )
    patched = pd.concat(
        [
            frame.assign(ticker=ticker, expiry=expiry, snapshot_utc=snapshot_utc)
            for snapshot_utc, frame in updated
        ],
        ignore_index=True,
    )
    volume_cols = patched[["ticker", "expiry", "snapshot_utc", "strike", "call_volume", "put_volume"]]
    history = history.merge(volume_cols, on=["ticker", "expiry", "snapshot_utc", "strike"], how="left")
    history[["call_volume", "put_volume"]] = history[["call_volume", "put_volume"]].fillna(0.0)
    sort_cols = [col for col in ["snapshot_utc", "strike"] if col in history.columns]
    if sort_cols:
        history = history.sort_values(sort_cols)
    history.to_parquet(history_path, index=False)
    history.to_csv(history_path.with_suffix(".csv"), index=False)


def main() -> None:
    args = parse_args()
    ticker = args.ticker.upper()
    roots = [args.data_root / args.date] if args.date else sorted(path for path in args.data_root.iterdir() if path.is_dir())
    patched_count = 0
    for output_dir in roots:
        grouped: dict[str, list[tuple[str, pd.DataFrame]]] = {}
        for raw_path in sorted((output_dir / "raw").glob(f"{ticker}_*_yahoo.json")):
            match = RAW_NAME_RE.match(raw_path.name)
            if not match:
                continue
            expiry = match.group("expiry")
            ts = match.group("ts")
            volumes = volume_frame(raw_path)
            if volumes is None:
                continue
            by_strike_path = output_dir / f"{ticker}_{expiry}_{ts}_by_strike.parquet"
            by_strike = patch_by_strike(by_strike_path, volumes)
            if by_strike is None:
                continue
            summary_path = output_dir / f"{ticker}_{expiry}_{ts}_summary.json"
            if not summary_path.exists():
                continue
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            snapshot_utc = summary.get("snapshot_utc")
            if not snapshot_utc:
                continue
            grouped.setdefault(expiry, []).append((snapshot_utc, by_strike))
            patched_count += 1
        for expiry, updated in grouped.items():
            patch_history(output_dir, ticker, expiry, updated)
            latest = output_dir / f"{ticker}_{expiry}_by_strike.parquet"
            if latest.exists() and updated:
                updated[-1][1].to_parquet(latest, index=False)
    print(f"Backfilled volume into {patched_count} by-strike snapshots.")


if __name__ == "__main__":
    main()
