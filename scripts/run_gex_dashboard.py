#!/usr/bin/env python3
"""One-command runner for the QQQ GEX dataset + dashboard workflow.

CBOE delayed (structural/OI) + Yahoo latest (spot/bid/ask/IV) -> reconciliation
-> intraday T -> BSM greeks -> DEX/GEX -> key levels -> HTML dashboard,
with raw + Parquet + JSON snapshot storage and a replay index.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "darwin":
    _OPEN_CMD = ["open"]
elif sys.platform.startswith("win"):
    _OPEN_CMD = ["cmd", "/c", "start", ""]
else:
    _OPEN_CMD = ["xdg-open"]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "options"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run daily_qqq_snapshot.py and render_gex_interactive.py in one command."
    )
    parser.add_argument("--ticker", default="QQQ", help="Ticker symbol, default QQQ.")
    parser.add_argument(
        "--expiry",
        default=None,
        help="Expiration date YYYY-MM-DD. If omitted, Yahoo's first available expiry is used.",
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date YYYY-MM-DD. Defaults to today's local date.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="Use an already-saved dataset instead of downloading from Yahoo.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root output folder for generated dataset.",
    )
    parser.add_argument(
        "--time-to-expiry-days",
        type=float,
        default=None,
        help="Override T in calendar days, useful when recomputing saved chains.",
    )
    parser.add_argument("--rate", type=float, default=0.04, help="Risk-free rate.")
    parser.add_argument("--top", type=int, default=10, help="Number of top GEX levels.")
    parser.add_argument(
        "--window",
        type=float,
        default=14,
        help="Strike window around spot for dashboard display.",
    )
    parser.add_argument(
        "--interactive-output",
        type=Path,
        default=None,
        help="Optional explicit output HTML path for interactive zoom/hover dashboard.",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Skip interactive HTML dashboard generation.",
    )
    parser.add_argument(
        "--levels-output",
        type=Path,
        default=None,
        help="Optional explicit output TXT path for one-line key levels.",
    )
    parser.add_argument(
        "--no-levels-text",
        action="store_true",
        help="Skip one-line key levels TXT generation.",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Only render dashboard from --input-dir. Requires --input-dir and --expiry.",
    )
    parser.add_argument(
        "--refresh-spot",
        action="store_true",
        help="When using --input-dir, fetch latest Yahoo spot instead of saved summary spot.",
    )
    parser.add_argument(
        "--no-refresh-spot",
        action="store_true",
        help="When using --input-dir, keep the saved summary spot.",
    )
    parser.add_argument(
        "--all-expiries",
        action="store_true",
        help="Aggregate GEX/DEX across all expiries within --expiry-horizon-days (MenthorQ/SpotGamma-style).",
    )
    parser.add_argument(
        "--expiry-horizon-days",
        type=int,
        default=45,
        help="With --all-expiries, only include expiries within this many calendar days (default 45).",
    )
    parser.add_argument(
        "--futures-ticker",
        default=None,
        help="Futures ticker (e.g. NQ1!) whose live price is used to compute the basis "
        "against the summary's cash spot, forwarded to export_gex_levels_text.py so every "
        "exported level lines up on a futures chart. Omit to export raw cash-index levels.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't automatically open the generated HTML dashboard.",
    )
    return parser.parse_args()


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(part) for part in cmd))
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)


def resolve_snapshot_date(value: str | None) -> str:
    return value or datetime.now().date().isoformat()


def infer_expiry(output_dir: Path, ticker: str, requested_expiry: str | None) -> str:
    """Determine which expiry the just-run fetch produced.

    Prefers the last matching entry in replay_index.jsonl (append-only log of
    every fetch, in run order) over globbing summary*.json files, since a
    day's output dir accumulates summaries from earlier runs/expiries too —
    picking alphabetically-first would silently pick a stale expiry.
    """
    if requested_expiry:
        return requested_expiry

    index_path = output_dir / "replay_index.jsonl"
    if index_path.exists():
        last_expiry = None
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("ticker") == ticker.upper():
                last_expiry = entry.get("expiry")
        if last_expiry:
            return last_expiry

    summaries = sorted(output_dir.glob(f"{ticker.upper()}_????-??-??_summary.json"))
    if not summaries:
        raise FileNotFoundError(
            f"Could not infer expiry because no latest summary JSON exists in {output_dir}"
        )
    name = summaries[0].name
    prefix = f"{ticker.upper()}_"
    suffix = "_summary.json"
    return name[len(prefix) : -len(suffix)]


def main() -> None:
    args = parse_args()
    ticker = args.ticker.upper()
    snapshot_date = resolve_snapshot_date(args.snapshot_date)

    if args.skip_fetch:
        if not args.input_dir or not args.expiry:
            raise SystemExit("--skip-fetch requires both --input-dir and --expiry.")
        dataset_dir = args.input_dir
        expiry = args.expiry
    else:
        snapshot_cmd = [
            sys.executable,
            "scripts/daily_qqq_snapshot.py",
            "--ticker",
            ticker,
            "--snapshot-date",
            snapshot_date,
            "--output-root",
            str(args.output_root),
            "--rate",
            str(args.rate),
            "--top",
            str(args.top),
        ]
        if args.expiry:
            snapshot_cmd += ["--expiry", args.expiry]
        if args.input_dir:
            snapshot_cmd += ["--input-dir", str(args.input_dir)]
            if not args.expiry:
                raise SystemExit("--input-dir requires --expiry.")
        if args.refresh_spot or (args.input_dir and not args.no_refresh_spot):
            snapshot_cmd += ["--refresh-spot"]
        if args.time_to_expiry_days is not None:
            snapshot_cmd += ["--time-to-expiry-days", str(args.time_to_expiry_days)]
        if args.all_expiries:
            snapshot_cmd += ["--all-expiries", "--expiry-horizon-days", str(args.expiry_horizon_days)]

        run(snapshot_cmd)
        dataset_dir = args.output_root / snapshot_date
        expiry = "ALL" if args.all_expiries else infer_expiry(dataset_dir, ticker, args.expiry)

    interactive = None
    if not args.no_interactive:
        interactive_cmd = [
            sys.executable,
            "scripts/render_gex_interactive.py",
            "--input-dir",
            str(dataset_dir),
            "--ticker",
            ticker,
            "--expiry",
            expiry,
            "--window",
            str(args.window),
        ]
        if args.interactive_output:
            interactive_cmd += ["--output", str(args.interactive_output)]
        run(interactive_cmd)
        interactive = args.interactive_output or dataset_dir / f"{ticker}_{expiry}_interactive.html"

    levels_text = None
    if not args.no_levels_text:
        levels_cmd = [
            sys.executable,
            "scripts/export_gex_levels_text.py",
            "--input-dir",
            str(dataset_dir),
            "--ticker",
            ticker,
            "--expiry",
            expiry,
        ]
        if args.levels_output:
            levels_cmd += ["--output", str(args.levels_output)]
        if args.futures_ticker:
            levels_cmd += ["--futures-ticker", args.futures_ticker]
        run(levels_cmd)
        levels_text = args.levels_output or dataset_dir / f"{ticker}_{expiry}_levels.txt"

    print("\nDone.")
    print(f"Dataset: {dataset_dir}")
    if interactive:
        print(f"Dashboard (HTML): {interactive}")
    if levels_text:
        print(f"Levels text: {levels_text}")

    if interactive and not args.no_open:
        subprocess.run(_OPEN_CMD + [str(interactive)], check=False)


if __name__ == "__main__":
    main()
