#!/usr/bin/env python3
"""Collect intraday option snapshots for the Volatility Flow panel.

Run this locally at 20:25 Vietnam time when you want a 90-minute, 1-minute
cadence collection window. Each iteration calls the normal one-command
dashboard runner, so replay_index.jsonl grows and Volatility Flow gets a new
point after every successful snapshot.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect volatility-flow snapshots on a fixed cadence.")
    parser.add_argument("--ticker", default="QQQ", help="Ticker symbol, default QQQ.")
    parser.add_argument("--expiry", default=None, help="Optional expiry YYYY-MM-DD.")
    parser.add_argument("--duration-minutes", type=int, default=90, help="Collection duration, default 90.")
    parser.add_argument("--interval-seconds", type=int, default=60, help="Snapshot interval, default 60.")
    parser.add_argument("--window", type=float, default=14, help="Strike window for dashboard rendering.")
    parser.add_argument("--rate", type=float, default=0.04, help="Risk-free rate.")
    parser.add_argument("--top", type=int, default=10, help="Top levels to record.")
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately if one snapshot fails. Default keeps trying next minute.",
    )
    return parser.parse_args()


def run_snapshot(args: argparse.Namespace, index: int, total: int) -> bool:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cmd = [
        sys.executable,
        "scripts/run_gex_dashboard.py",
        "--ticker",
        args.ticker.upper(),
        "--rate",
        str(args.rate),
        "--top",
        str(args.top),
        "--window",
        str(args.window),
        "--no-open",
    ]
    if args.expiry:
        cmd += ["--expiry", args.expiry]

    print(f"\n[{now}] snapshot {index}/{total}")
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, check=False)
    if result.returncode == 0:
        return True
    print(f"Snapshot {index} failed with exit code {result.returncode}", file=sys.stderr)
    return False


def main() -> None:
    args = parse_args()
    total = max(1, int(args.duration_minutes * 60 / args.interval_seconds) + 1)
    started = datetime.now()
    ends = started + timedelta(minutes=args.duration_minutes)
    print(
        f"Collecting {args.ticker.upper()} every {args.interval_seconds}s "
        f"for {args.duration_minutes} minutes."
    )
    print(f"Start: {started:%Y-%m-%d %H:%M:%S}")
    print(f"End:   {ends:%Y-%m-%d %H:%M:%S}")

    successes = 0
    failures = 0
    next_run = time.monotonic()

    for index in range(1, total + 1):
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        ok = run_snapshot(args, index, total)
        if ok:
            successes += 1
        else:
            failures += 1
            if args.stop_on_error:
                raise SystemExit(1)

        next_run += args.interval_seconds

    print("\nDone.")
    print(f"Successful snapshots: {successes}")
    print(f"Failed snapshots:     {failures}")
    print("Open the latest *_interactive.html under data/options/YYYY-MM-DD/")


if __name__ == "__main__":
    main()
