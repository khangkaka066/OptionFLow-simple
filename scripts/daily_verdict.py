#!/usr/bin/env python3
"""CLI for the daily 0DTE dealer-hedging verdict (BUY LESS|SELL MORE vs
BUY MORE|SELL LESS). The core computation lives in
live_dashboard/dealer_verdict.py — it's also wired live into the Key Level
Summary dashboard panel (see live_server.refresh_key_level_summary).

This script only reuses already-collected on-disk option-chain history
(`data/options/<date>/history/...`, written by the live collector) — it does
not fetch anything over the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_dashboard.dealer_verdict import DEFAULT_WINDOW, compute_dealer_verdict  # noqa: E402
from live_dashboard.time_utils import market_session_utc, session_for_trading_date  # noqa: E402
from live_server import seed_session_data  # noqa: E402  (reuse existing data loading)


def run(ticker: str, date: str | None, window: float = DEFAULT_WINDOW) -> dict:
    today_session = session_for_trading_date(date) if date else market_session_utc()
    return compute_dealer_verdict(
        ticker,
        today_session,
        session_for_trading_date=session_for_trading_date,
        seed_session_data=seed_session_data,
        window=window,
    )


def format_report(result: dict) -> str:
    def fmt_pct(v: float | None) -> str:
        return f"{v:.2f}%" if v is not None else "n/a"

    def fmt_px(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "n/a"

    a, b = result["branch_a"], result["branch_b"]
    lines = [
        f"=== {result['ticker']} 0DTE verdict — {result['trading_date']} ===",
        "",
        f"Branch A (EOD close)      {result['prev2_date']} {fmt_pct(a['prev2'])} -> "
        f"{result['prev1_date']} {fmt_pct(a['prev1'])}  => {a['direction']}",
        f"Branch B (session avg)    {result['prev2_date']} {fmt_pct(b['prev2'])} -> "
        f"{result['prev1_date']} {fmt_pct(b['prev1'])}  => {b['direction']}",
        f"Branches agree: {result['branches_agree']}  (confidence: {result['confidence']})",
        "",
        f"Strike ref (prior close {result['strike_ref_date']}): {fmt_px(result['strike_ref'])}",
        f"Spot @ 9:00 ET ({result['spot_900et_time'] or 'n/a'}): {fmt_px(result['spot_900et'])}",
        f"Spot vs strike: "
        + (
            "ABOVE" if result["spot_above_strike"] else "BELOW"
            if result["spot_above_strike"] is not None
            else "n/a"
        ),
        "",
        f">>> VERDICT: {result['verdict']}",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily 0DTE dealer-hedging verdict")
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", default=None, help="NY trading date (YYYY-MM-DD); default = today")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of the report")
    args = parser.parse_args()

    result = run(args.ticker, args.date, args.window)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
