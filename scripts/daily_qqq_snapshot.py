#!/usr/bin/env python3
"""Fetch a reconciled CBOE+Yahoo option chain and compute GEX/DEX levels.

Pipeline: CBOE delayed (structural/OI) + Yahoo latest (spot/bid/ask/IV)
  -> source reconciliation -> intraday T -> BSM delta/gamma -> DEX/GEX
  -> Gamma Wall/Put Support/Call Resistance/Gamma Flip/Delta Flip
  -> raw + Parquet + JSON snapshot, appended to the day's replay index.

This is a personal-research approximation, not a production-grade dealer
model. See exposure.py for the GEX/DEX convention used.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import storage
from bsm import years_from_override, years_to_expiry
from exposure import aggregate_by_strike, build_summary, compute_greeks, nearest_atm_iv
from reconcile import reconcile_chain
from sources import cboe, yahoo

DEFAULT_TICKER = "QQQ"
DEFAULT_RATE = 0.04
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "options"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch CBOE+Yahoo reconciled option chain and compute Black-Scholes GEX/DEX levels."
    )
    parser.add_argument("--ticker", default=DEFAULT_TICKER, help="Ticker symbol, default QQQ.")
    parser.add_argument(
        "--expiry",
        default=None,
        help="Expiration date YYYY-MM-DD. If omitted, use first available Yahoo expiry.",
    )
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE, help="Risk-free rate.")
    parser.add_argument("--top", type=int, default=10, help="Number of top GEX/DEX levels to record.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Root output folder.")
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date YYYY-MM-DD. Defaults to today's local date.",
    )
    parser.add_argument(
        "--time-to-expiry-days",
        type=float,
        default=None,
        help="Override T in calendar days. Useful when recomputing a saved chain.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help=(
            "Recompute from the most recent saved raw CBOE+Yahoo JSON in this date directory "
            "instead of fetching live data. Requires --expiry."
        ),
    )
    parser.add_argument(
        "--refresh-spot",
        action="store_true",
        help="When using --input-dir, fetch a fresh Yahoo spot instead of the saved one.",
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
    args = parser.parse_args()
    if args.all_expiries and args.expiry:
        raise SystemExit("--all-expiries and --expiry are mutually exclusive.")
    if args.all_expiries and args.input_dir:
        raise SystemExit("--all-expiries with --input-dir is not supported yet.")
    return args


def resolve_snapshot_day(value: str | None) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else datetime.now().date()


def load_saved_raw(input_dir: Path, ticker: str, expiry: str) -> tuple[dict, dict]:
    raw_dir = input_dir / "raw"
    cboe_matches = sorted(raw_dir.glob(f"{ticker}_{expiry}_*_cboe.json"))
    yahoo_matches = sorted(raw_dir.glob(f"{ticker}_{expiry}_*_yahoo.json"))
    if not cboe_matches or not yahoo_matches:
        raise FileNotFoundError(f"No saved raw CBOE/Yahoo JSON found in {raw_dir} for {ticker} {expiry}")
    cboe_raw = json.loads(cboe_matches[-1].read_text(encoding="utf-8"))
    yahoo_raw = json.loads(yahoo_matches[-1].read_text(encoding="utf-8"))
    return cboe_raw, yahoo_raw


def yahoo_chain_to_raw(yahoo_data: yahoo.YahooChain) -> dict:
    return {
        "ticker": yahoo_data.ticker,
        "expiry": yahoo_data.expiry,
        "spot": yahoo_data.spot,
        "calls": yahoo_data.calls.to_dict(orient="records"),
        "puts": yahoo_data.puts.to_dict(orient="records"),
    }


def yahoo_raw_to_chain(raw: dict) -> yahoo.YahooChain:
    return yahoo.YahooChain(
        ticker=raw["ticker"],
        expiry=raw["expiry"],
        spot=float(raw["spot"]),
        calls=pd.DataFrame(raw["calls"]),
        puts=pd.DataFrame(raw["puts"]),
        available_expirations=[raw["expiry"]],
        included_expirations=[raw["expiry"]],
    )


def print_summary(summary_dict: dict, report_dict: dict) -> None:
    print(f"\n=== {summary_dict['ticker']} {summary_dict['expiry']} ===")
    print(f"Spot (yahoo): {summary_dict['spot']:.2f}  T(years): {summary_dict['years_to_expiry']:.5f}")
    print(
        f"Net GEX: {summary_dict['net_gex']:,.0f}   Gamma Wall: {summary_dict['gamma_wall_abs']}   "
        f"Gamma Flip: {summary_dict['gamma_flip']}"
    )
    print(
        f"Call Resistance: {summary_dict['call_resistance']}   Put Support: {summary_dict['put_support']}"
    )
    print(f"Net DEX: {summary_dict['net_dex']:,.0f}   Delta Flip: {summary_dict['delta_flip']}")
    print(
        "Reconciliation: "
        f"matched={report_dict['matched_both_sources']} cboe_only={report_dict['cboe_only']} "
        f"yahoo_only={report_dict['yahoo_only']} iv_flagged={report_dict['iv_flagged_count']} "
        f"oi_fallback={report_dict['oi_fallback_count']} spot_flagged={report_dict['spot_flagged']}"
    )


def main() -> None:
    args = parse_args()
    ticker_symbol = args.ticker.upper()
    snapshot_day = resolve_snapshot_day(args.snapshot_date)
    output_dir = args.output_root / snapshot_day.isoformat()
    ts = datetime.now().strftime("%H%M%S")

    included_expiries: list[str] | None = None

    if args.all_expiries:
        yahoo_data = yahoo.fetch_multi_chain(ticker_symbol, horizon_days=args.expiry_horizon_days)
        expiry = yahoo_data.expiry  # "ALL"
        included_expiries = yahoo_data.included_expirations
        cboe_raw = cboe.fetch_raw(ticker_symbol)
        cboe_data = cboe.parse_chain(cboe_raw, ticker_symbol, expiry=None)
        cboe_data.chain = cboe_data.chain[cboe_data.chain["expiry"].isin(included_expiries)]
    elif args.input_dir:
        if not args.expiry:
            raise SystemExit("--input-dir requires --expiry.")
        cboe_raw, yahoo_raw = load_saved_raw(args.input_dir, ticker_symbol, args.expiry)
        cboe_data = cboe.parse_chain(cboe_raw, ticker_symbol, args.expiry)
        yahoo_data = yahoo_raw_to_chain(yahoo_raw)
        if args.refresh_spot:
            yf = yahoo.import_yfinance()
            yahoo_data.spot = yahoo.get_spot(yf.Ticker(yahoo._yahoo_symbol(ticker_symbol)))
        expiry = args.expiry
    else:
        yahoo_data = yahoo.fetch_chain(ticker_symbol, args.expiry)
        expiry = yahoo_data.expiry
        cboe_data = cboe.fetch_chain(ticker_symbol, expiry)

    effective_day = yahoo.effective_snapshot_day(snapshot_day, yahoo_data.calls, yahoo_data.puts)

    if args.time_to_expiry_days is not None:
        days, years = years_from_override(args.time_to_expiry_days)
        years_by_expiry = {exp: years for exp in (included_expiries or [expiry])}
    elif args.all_expiries:
        years_by_expiry = {}
        days_by_expiry = {}
        for exp in included_expiries:
            exp_days, exp_years = years_to_expiry(exp, effective_day)
            years_by_expiry[exp] = exp_years
            days_by_expiry[exp] = exp_days
        nearest_expiry = min(included_expiries)
        days, years = days_by_expiry[nearest_expiry], years_by_expiry[nearest_expiry]
    else:
        days, years = years_to_expiry(expiry, effective_day)
        years_by_expiry = {expiry: years}

    reconciled, report = reconcile_chain(
        cboe_data.chain, yahoo_data.calls, yahoo_data.puts, yahoo_data.spot, cboe_data.underlying_price
    )
    chain = compute_greeks(reconciled, yahoo_data.spot, years_by_expiry, args.rate)
    by_strike = aggregate_by_strike(chain)

    tenor_atm_iv: dict[str, float] | None = None
    if args.all_expiries and included_expiries:
        tenor_atm_iv = {}
        for exp in sorted(included_expiries)[:3]:
            exp_chain = chain[chain["expiry"] == exp]
            if exp_chain.empty:
                continue
            exp_by_strike = aggregate_by_strike(exp_chain)
            atm_iv = nearest_atm_iv(exp_by_strike, yahoo_data.spot)
            if atm_iv is not None:
                tenor_atm_iv[exp] = atm_iv
    summary = build_summary(
        ticker_symbol,
        expiry,
        yahoo_data.spot,
        days,
        years,
        args.rate,
        chain,
        by_strike,
        args.top,
        snapshot_day,
        effective_day,
        included_expiries=included_expiries,
    )
    summary_dict = asdict(summary)
    report_dict = report.as_dict()

    raw_paths = storage.save_raw(output_dir, ticker_symbol, expiry, ts, cboe_data.raw, yahoo_chain_to_raw(yahoo_data))
    raw_frames = [
        storage.normalize_raw_chain(
            cboe_data.chain,
            capture_ts=summary_dict["snapshot_utc"],
            ticker=ticker_symbol,
            source="cboe",
            spot=cboe_data.underlying_price,
        ),
        storage.normalize_raw_chain(
            pd.concat([yahoo_data.calls, yahoo_data.puts], ignore_index=True),
            capture_ts=summary_dict["snapshot_utc"],
            ticker=ticker_symbol,
            source="yahoo",
            spot=yahoo_data.spot,
        ),
    ]
    snapshot_paths = storage.save_snapshot(output_dir, ticker_symbol, expiry, ts, by_strike, summary_dict, report_dict)
    storage.update_latest(output_dir, ticker_symbol, expiry, by_strike, summary_dict)
    storage.append_replay_index(output_dir, ticker_symbol, expiry, ts, snapshot_paths)
    history_paths = storage.append_history_store(
        output_dir,
        ticker_symbol,
        expiry,
        ts,
        by_strike,
        summary_dict,
        report_dict,
        raw_paths=raw_paths,
        snapshot_paths=snapshot_paths,
    )
    market_paths = storage.append_market_dataset(
        output_dir,
        ticker=ticker_symbol,
        summary_dict=summary_dict,
        by_strike=by_strike,
        raw_frames=raw_frames,
        tenor_atm_iv=tenor_atm_iv,
    )
    deleted_raw = storage.delete_raw(raw_paths)

    print_summary(summary_dict, report_dict)
    print(f"\nSaved outputs to: {output_dir}")
    print(f"History snapshots: {history_paths['snapshots']}")
    print(f"History by-strike: {history_paths['by_strike_history']}")
    print(f"Market dataset: {market_paths.get('by_strike')}")
    if deleted_raw:
        print(f"Deleted raw JSON after history write: {', '.join(str(p) for p in deleted_raw)}")


if __name__ == "__main__":
    main()
