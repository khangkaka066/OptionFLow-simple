#!/usr/bin/env python3
"""Export key GEX levels to a single-line text format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export GEX summary as a single-line text file.")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--expiry", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def choose_summary(input_dir: Path, ticker: str | None, expiry: str | None) -> Path:
    """Pick the untimestamped 'latest' summary, i.e. exactly `{TICKER}_{EXPIRY}_summary.json`."""
    if ticker and expiry:
        exact = input_dir / f"{ticker.upper()}_{expiry}_summary.json"
        if exact.exists():
            return exact
    matches = sorted(input_dir.glob("[A-Z]*_????-??-??_summary.json"))
    if ticker:
        matches = [p for p in matches if p.name.startswith(f"{ticker.upper()}_")]
    if expiry:
        matches = [p for p in matches if f"_{expiry}_summary.json" in p.name]
    if not matches:
        raise FileNotFoundError(f"No latest summary JSON found in {input_dir}")
    return matches[0]


def fmt_level(value, decimals: int = 0) -> str:
    if value is None:
        return "NA"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "NA"
    if decimals:
        return f"{number:.{decimals}f}"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def build_line(summary: dict) -> str:
    ticker = summary["ticker"].upper()
    top = summary.get("top_abs_gex_levels", [])
    gex_levels = [item.get("strike") for item in top[:10]]
    while len(gex_levels) < 10:
        gex_levels.append(None)

    # HVL is not directly available from this Yahoo/BS approximation.
    hvl = summary.get("spot")
    hvl_0dte = None

    parts = [
        f"${ticker}: Call Resistance",
        fmt_level(summary.get("call_resistance")),
        "Put Support",
        fmt_level(summary.get("put_support")),
        "HVL",
        fmt_level(hvl, decimals=2),
        "1D Min",
        "NA",
        "1D Max",
        "NA",
        "Call Resistance 0DTE",
        fmt_level(summary.get("call_resistance")),
        "Put Support 0DTE",
        fmt_level(summary.get("put_support")),
        "HVL 0DTE",
        fmt_level(hvl_0dte),
        "Gamma Wall 0DTE",
        fmt_level(summary.get("gamma_wall_abs")),
        "Gamma Flip",
        fmt_level(summary.get("gamma_flip")),
        "Delta Flip",
        fmt_level(summary.get("delta_flip")),
    ]

    for idx, strike in enumerate(gex_levels, start=1):
        parts.extend([f"GEX {idx}", fmt_level(strike)])

    return ", ".join(parts)


def main() -> None:
    args = parse_args()
    summary_path = choose_summary(args.input_dir, args.ticker, args.expiry)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    output = args.output or args.input_dir / f"{summary['ticker']}_{summary['expiry']}_levels.txt"
    line = build_line(summary)
    output.write_text(line + "\n", encoding="utf-8")
    print(f"Saved levels text: {output}")
    print(line)


if __name__ == "__main__":
    main()
