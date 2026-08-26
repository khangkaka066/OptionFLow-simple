#!/usr/bin/env python3
"""Export key GEX levels to a single-line text format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sources import yahoo


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export GEX summary as a single-line text file.")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--expiry", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--futures-ticker",
        default=None,
        help="Futures ticker (e.g. NQ1!) whose live price is used to compute the basis "
        "against the summary's cash spot, added to every exported level so it lines up "
        "on a futures chart. Omit to export raw cash-index levels (default).",
    )
    return parser.parse_args()


def fetch_futures_basis(futures_ticker: str, cash_spot: float) -> float | None:
    if not futures_ticker:
        return None
    try:
        yf = yahoo.import_yfinance()
        futures_spot = yahoo.get_spot(yf.Ticker(yahoo._yahoo_symbol(futures_ticker)))
        return float(futures_spot) - float(cash_spot)
    except Exception:
        return None


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


def build_line(summary: dict, basis: float | None = None, futures_ticker: str | None = None) -> str:
    ticker = summary["ticker"].upper()
    top = summary.get("top_abs_gex_levels", [])
    gex_levels = [item.get("strike") for item in top[:10]]
    while len(gex_levels) < 10:
        gex_levels.append(None)

    def adj(value):
        if value is None or basis is None:
            return value
        return float(value) + basis

    label = f"${ticker} ({futures_ticker} adj.)" if basis else f"${ticker}"

    # HVL is approximated from the current spot; HVL 0DTE uses gamma flip when
    # available because this free-data pipeline does not have a paid HVL field.
    hvl = adj(summary.get("spot"))
    hvl_0dte = adj(summary.get("gamma_flip"))

    parts = [
        f"{label}: Call Resistance",
        fmt_level(adj(summary.get("call_resistance"))),
        "Put Support",
        fmt_level(adj(summary.get("put_support"))),
        "HVL",
        fmt_level(hvl, decimals=2),
        "1D Min",
        "NA",
        "1D Max",
        "NA",
        "Call Resistance 0DTE",
        fmt_level(adj(summary.get("call_resistance"))),
        "Put Support 0DTE",
        fmt_level(adj(summary.get("put_support"))),
        "HVL 0DTE",
        fmt_level(hvl_0dte),
        "Gamma Wall 0DTE",
        fmt_level(adj(summary.get("gamma_wall_abs"))),
    ]

    for idx, strike in enumerate(gex_levels, start=1):
        parts.extend([f"GEX {idx}", fmt_level(adj(strike))])

    return ", ".join(parts)


def main() -> None:
    args = parse_args()
    summary_path = choose_summary(args.input_dir, args.ticker, args.expiry)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    output = args.output or args.input_dir / f"{summary['ticker']}_{summary['expiry']}_levels.txt"
    basis = None
    if args.futures_ticker and summary.get("spot") is not None:
        basis = fetch_futures_basis(args.futures_ticker, summary["spot"])
    line = build_line(summary, basis=basis, futures_ticker=args.futures_ticker)
    output.write_text(line + "\n", encoding="utf-8")
    print(f"Saved levels text: {output}")
    print(line)


if __name__ == "__main__":
    main()
