#!/usr/bin/env python3
"""Append the latest dashboard summary to a lightweight history CSV."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


FIELDS = [
    "snapshot_utc",
    "snapshot_vn",
    "ticker",
    "expiry",
    "spot",
    "avg_iv_pct",
    "net_gex",
    "net_dex",
    "call_resistance",
    "put_support",
    "gamma_wall",
    "gamma_flip",
    "dex_wall",
    "delta_flip",
    "total_call_gex",
    "total_put_gex",
    "total_call_dex",
    "total_put_dex",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update lightweight options dashboard history.")
    parser.add_argument("--data-root", type=Path, default=Path("data/options"))
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--history-dir", type=Path, default=Path("history"))
    return parser.parse_args()


def latest_summary_path(data_root: Path, ticker: str) -> Path:
    matches = [
        path
        for path in data_root.glob(f"*/{ticker.upper()}_*_summary.json")
        if len(path.stem.split("_")) == 3
    ]
    if not matches:
        raise FileNotFoundError(f"No latest summary JSON found under {data_root} for {ticker}")
    return max(matches, key=lambda path: path.stat().st_mtime)


def to_vn_time(value: str | None) -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(timespec="seconds")


def row_from_summary(summary: dict) -> dict[str, str | float | int | None]:
    snapshot_utc = summary.get("snapshot_utc")
    return {
        "snapshot_utc": snapshot_utc,
        "snapshot_vn": to_vn_time(snapshot_utc),
        "ticker": summary.get("ticker"),
        "expiry": summary.get("expiry"),
        "spot": summary.get("spot"),
        "avg_iv_pct": (summary.get("avg_iv") * 100) if summary.get("avg_iv") is not None else None,
        "net_gex": summary.get("net_gex"),
        "net_dex": summary.get("net_dex"),
        "call_resistance": summary.get("call_resistance"),
        "put_support": summary.get("put_support"),
        "gamma_wall": summary.get("gamma_wall_abs"),
        "gamma_flip": summary.get("gamma_flip"),
        "dex_wall": summary.get("dex_wall_abs"),
        "delta_flip": summary.get("delta_flip"),
        "total_call_gex": summary.get("total_call_gex"),
        "total_put_gex": summary.get("total_put_gex"),
        "total_call_dex": summary.get("total_call_dex"),
        "total_put_dex": summary.get("total_put_dex"),
    }


def read_existing(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    summary_path = latest_summary_path(args.data_root, args.ticker)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    new_row = row_from_summary(summary)

    history_path = args.history_dir / "snapshots.csv"
    rows = read_existing(history_path)
    rows = [
        row
        for row in rows
        if not (row.get("snapshot_utc") == new_row["snapshot_utc"] and row.get("ticker") == new_row["ticker"])
    ]
    rows.append(new_row)
    rows = sorted(rows, key=lambda row: row.get("snapshot_utc") or "")

    write_rows(history_path, rows)

    latest_summary_ticker = args.history_dir / f"latest_summary_{args.ticker.upper()}.json"
    latest_summary_ticker.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    latest_summary = args.history_dir / "latest_summary.json"
    if args.ticker.upper() == "QQQ":
        latest_summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Updated {history_path} with {len(rows)} rows")
    print(f"Latest summary: {latest_summary_ticker}")


if __name__ == "__main__":
    main()
