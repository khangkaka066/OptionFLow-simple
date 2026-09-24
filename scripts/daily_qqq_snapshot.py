#!/usr/bin/env python3
"""Fetch a reconciled option chain and compute GEX/DEX levels.

Pipeline (default live path): InsiderFinance (structural/OI/IV/bid/ask,
  and spot) + Optionwatch (bid/ask size) -> primary reconciliation.
  Volume is taken from Pineify when available, with CBOE+Yahoo's
  volume kept only as a per-row fallback (see `volume_source` in the
  reconciliation report). Spot for greeks is InsiderFinance's own
  `spot` field, falling back to Yahoo/CBOE only if InsiderFinance
  omits it. CBOE+Yahoo are being phased out of the default path; see
  `report_dict["volume_source"]`/`report_dict["spot_source"]` to
  monitor fallback usage before removing them entirely.

The `--all-expiries` and `--input-dir` modes still run the original
CBOE+Yahoo-only pipeline unless `--source insiderfinance` is selected.
The explicit InsiderFinance mode supports single and multiple expiries and
uses InsiderFinance spot/chain, with CBOE volume and missing IV/quote supplements.

This is a personal-research approximation, not a production-grade dealer
model. See exposure.py for the GEX/DEX convention used.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import storage
from bsm import years_from_override, years_to_expiry
from cleanup_dead_data import run_cleanup
from exposure import aggregate_by_strike, build_summary, compute_greeks, nearest_atm_iv
from reconcile import reconcile_chain
from sources import cboe, insiderfinance, yahoo

DEFAULT_TICKER = "QQQ"
DEFAULT_RATE = 0.04
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "options"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch an option chain and compute Black-Scholes GEX/DEX levels."
    )
    parser.add_argument("--ticker", default=DEFAULT_TICKER, help="Ticker symbol, default QQQ.")
    parser.add_argument(
        "--source", choices=["auto", "insiderfinance"], default="auto",
        help="Use the existing pipeline or InsiderFinance chain/spot with CBOE volume and IV/quote supplements.",
    )
    parser.add_argument(
        "--expiry",
        default=None,
        help="Expiration date YYYY-MM-DD. If omitted, use the source's first available expiry.",
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
        help="With --all-expiries, expiry horizon in days (default 45); 0 includes all for --source insiderfinance.",
    )
    args = parser.parse_args()
    if args.source == "insiderfinance" and args.input_dir:
        raise SystemExit("--source insiderfinance does not support --input-dir.")
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


def empty_yahoo_chain(
    ticker: str, expiry: str, available_expirations: list[str], spot: float
) -> yahoo.YahooChain:
    """Placeholder YahooChain used when Yahoo is unreachable.

    Empty calls/puts let reconcile_chain() fall back entirely to CBOE fields
    via its existing .fillna() logic instead of erroring out.
    """
    columns = ["strike", "openInterest", "impliedVolatility", "volume", "bid", "ask", "option_type", "expiry"]
    empty = pd.DataFrame(columns=columns)
    return yahoo.YahooChain(
        ticker=ticker,
        expiry=expiry,
        spot=spot,
        calls=empty.copy(),
        puts=empty.copy(),
        available_expirations=available_expirations,
        included_expirations=[expiry],
    )


def print_summary(summary_dict: dict, report_dict: dict) -> None:
    print(f"\n=== {summary_dict['ticker']} {summary_dict['expiry']} ===")
    spot_source = report_dict.get("spot_source", "yahoo")
    print(f"Spot ({spot_source}): {summary_dict['spot']:.2f}  T(years): {summary_dict['years_to_expiry']:.5f}")
    print(
        f"Net GEX: {summary_dict['net_gex']:,.0f}   Gamma Wall: {summary_dict['gamma_wall_abs']}   "
        f"Gamma Flip: {summary_dict['gamma_flip']}"
    )
    print(
        f"Call Resistance: {summary_dict['call_resistance']}   Put Support: {summary_dict['put_support']}"
    )
    print(f"Net DEX: {summary_dict['net_dex']:,.0f}   Delta Flip: {summary_dict['delta_flip']}")
    if report_dict.get("chain_source") == "insiderfinance":
        print(
            f"Source: InsiderFinance; contracts={report_dict['total_strikes']}; "
            f"volume={report_dict['volume_source']} "
            f"(CBOE matched={report_dict['volume_cboe_count']}, "
            f"unavailable={report_dict['volume_unavailable_count']})"
        )
        return
    print(
        "Reconciliation: "
        f"matched={report_dict['matched_both_sources']} cboe_only={report_dict['cboe_only']} "
        f"yahoo_only={report_dict['yahoo_only']} iv_flagged={report_dict['iv_flagged_count']} "
        f"oi_fallback={report_dict['oi_fallback_count']} spot_flagged={report_dict['spot_flagged']} "
        f"yahoo_available={report_dict['yahoo_available']}"
    )


def main() -> None:
    args = parse_args()
    ticker_symbol = args.ticker.upper()
    snapshot_day = resolve_snapshot_day(args.snapshot_date)
    output_dir = args.output_root / snapshot_day.isoformat()
    ts = datetime.now().strftime("%H%M%S")

    included_expiries: list[str] | None = None
    yahoo_available = True
    if_data = None
    if_live_spot = None
    if_ow_chain = None
    if_ow_report = None
    ow_raw = None

    if args.source == "insiderfinance":
        if_data = insiderfinance.fetch_chain(ticker_symbol)
        if if_data.spot is None or not np.isfinite(if_data.spot) or if_data.spot <= 0:
            raise SystemExit("insiderfinance: missing or invalid snapshot spot")
        # At the EOD cron Vietnam is already on the following calendar day.
        effective_day = datetime.now(ZoneInfo("America/New_York")).date()
        source_timestamp = if_data.raw.get("timestamp")
        if source_timestamp:
            effective_day = datetime.fromisoformat(
                source_timestamp.replace("Z", "+00:00")
            ).astimezone(ZoneInfo("America/New_York")).date()
        reconciled, selected_expiries = insiderfinance.snapshot_chain(
            if_data, effective_day, expiry=args.expiry,
            all_expiries=args.all_expiries, horizon_days=args.expiry_horizon_days,
        )
        expiry = "ALL" if args.all_expiries else selected_expiries[0]
        included_expiries = selected_expiries if args.all_expiries else None
        report_dict = {
            "chain_source": "insiderfinance", "total_strikes": len(reconciled),
            "volume_source": "unavailable", "yahoo_available": False,
            "source_timestamp": source_timestamp, "source_is_stale": if_data.raw.get("isStale"),
            "fetch_source": if_data.raw.get("fetch_source"),
        }
        volume_data = None
        try:
            volume_data = cboe.fetch_chain(ticker_symbol)
            supplemented = cboe.supplement_volume(reconciled, volume_data.chain)
            reconciled = cboe.supplement_quotes(supplemented, volume_data.chain)
        except Exception as exc:
            volume_data = None
            report_dict["volume_error"] = str(exc)
            print(f"WARNING: CBOE supplements unavailable ({exc}); keeping InsiderFinance chain/spot.")
        volume_count = int(reconciled["volume"].notna().sum())
        report_dict.update({
            "volume_source": "cboe" if volume_count else "unavailable",
            "volume_cboe_count": volume_count,
            "volume_unavailable_count": len(reconciled) - volume_count,
            "volume_timestamp": volume_data.raw.get("timestamp") if volume_data is not None else None,
            "iv_cboe_count": int((reconciled["iv_source"] == "cboe").sum()),
            "quote_cboe_count": int((reconciled.get("quote_source", pd.Series(dtype=str)) == "cboe").sum()),
        })
    elif args.all_expiries:
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
        from reconcile import reconcile_if_ow_chain, reconcile_pineify_chain
        from sources import optionwatch, pineify

        # Primary chain: InsiderFinance (structural/OI/IV/bid/ask) + Optionwatch (bid/ask size).
        if_data = insiderfinance.fetch_chain(ticker_symbol)
        try:
            if_live_spot = insiderfinance.fetch_spot(ticker_symbol)
        except Exception:
            if_live_spot = None
        if_future_expiries = sorted(
            e for e in if_data.chain["expiry"].dropna().unique().tolist() if e >= date.today().isoformat()
        )
        if not if_future_expiries:
            raise SystemExit("insiderfinance: no upcoming expiries available")
        if args.expiry and args.expiry not in if_future_expiries:
            raise SystemExit(f"insiderfinance: requested expiry {args.expiry} not available")
        expiry = args.expiry or if_future_expiries[0]
        if_chain = if_data.chain[if_data.chain["expiry"] == expiry].reset_index(drop=True)

        ow_raw = optionwatch.fetch_snapshot(ticker_symbol, expiry)
        ow_chain = optionwatch.parse_snapshot(ow_raw, ticker_symbol)
        if_ow_chain, if_ow_report = reconcile_if_ow_chain(if_chain, ow_chain)

        # Best-effort supplement: Pineify volume/OI (public options-chain page only).
        pf_report: dict | None = None
        try:
            pf_chain = pineify.fetch_chain(ticker_symbol, expiry=expiry)
            if_ow_chain, pf_report = reconcile_pineify_chain(if_ow_chain, pf_chain)
        except Exception as exc:
            print(f"WARNING: Pineify unavailable ({exc}); continuing without pf_volume/pf_oi.")

        # Secondary fetch: CBOE + Yahoo, used only for Volume (and Yahoo spot) — unchanged cadence/logic.
        cboe_raw = cboe.fetch_raw(ticker_symbol)
        cboe_all = cboe.parse_chain(cboe_raw, ticker_symbol, expiry=None)
        cboe_expiries = sorted(cboe_all.chain["expiry"].dropna().unique().tolist())

        yahoo_available = True
        try:
            yahoo_data = yahoo.fetch_chain(ticker_symbol, expiry)
        except RuntimeError as exc:
            if not cboe_expiries or not cboe_all.underlying_price:
                raise
            yahoo_available = False
            print(f"WARNING: Yahoo unavailable ({exc}); volume/spot falling back to CBOE-only data.")
            fallback_spot = cboe_all.underlying_price or if_data.spot or 0.0
            yahoo_data = empty_yahoo_chain(ticker_symbol, expiry, cboe_expiries or [expiry], fallback_spot)

        cboe_data = cboe_all
        cboe_data.chain = cboe_data.chain[cboe_data.chain["expiry"] == expiry]

    if args.source != "insiderfinance":
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

    if args.source != "insiderfinance":
        volume_chain, volume_report = reconcile_chain(
            cboe_data.chain, yahoo_data.calls, yahoo_data.puts, yahoo_data.spot, cboe_data.underlying_price
        )
    if if_ow_chain is not None:
        reconciled = pd.merge(
            if_ow_chain,
            volume_chain[["strike", "option_type", "expiry", "volume"]].rename(
                columns={"volume": "cy_volume"}
            ),
            on=["strike", "option_type", "expiry"],
            how="left",
        )
        if "pf_volume" in reconciled.columns:
            reconciled["volume_source"] = np.where(
                reconciled["pf_volume"].notna(), "pineify", "cboe_yahoo_fallback"
            )
            reconciled["volume"] = reconciled["pf_volume"].fillna(reconciled["cy_volume"]).fillna(0.0)
        else:
            reconciled["volume_source"] = "cboe_yahoo_fallback"
            reconciled["volume"] = reconciled["cy_volume"].fillna(0.0)
        reconciled = reconciled.drop(columns=["cy_volume"])
        report_dict = volume_report.as_dict()
        report_dict["yahoo_available"] = yahoo_available
        report_dict["chain_source"] = "insiderfinance+optionwatch"
        report_dict.update({f"if_ow_{k}": v for k, v in if_ow_report.items()})
        report_dict["volume_pineify_count"] = int((reconciled["volume_source"] == "pineify").sum())
        report_dict["volume_cboe_yahoo_fallback_count"] = int(
            (reconciled["volume_source"] == "cboe_yahoo_fallback").sum()
        )
        if pf_report is not None:
            report_dict["pf_matched"] = pf_report["pf_matched"]
            report_dict["pf_total_pineify_rows"] = pf_report["pf_total_pineify_rows"]
    elif args.source != "insiderfinance":
        reconciled = volume_chain
        report_dict = volume_report.as_dict()
        report_dict["yahoo_available"] = yahoo_available
        report_dict["chain_source"] = "cboe+yahoo"

    # Spot for greeks: InsiderFinance's ticker-details API is authoritative when
    # available (near-real-time, updates every request); the gamma-exposure page's
    # own spot field is a cached/periodic snapshot and is only a fallback within
    # InsiderFinance, ahead of Yahoo spot for --all-expiries/--input-dir modes.
    if if_live_spot:
        spot_for_greeks = if_live_spot
        report_dict["spot_source"] = "insiderfinance_live"
    elif if_data is not None and if_data.spot:
        spot_for_greeks = if_data.spot
        report_dict["spot_source"] = "insiderfinance"
    else:
        spot_for_greeks = yahoo_data.spot
        report_dict["spot_source"] = "yahoo"

    chain = compute_greeks(reconciled, spot_for_greeks, years_by_expiry, args.rate)
    if args.source == "insiderfinance":
        report_dict["iv_unavailable_count"] = int(chain["impliedVolatility"].isna().sum())
    by_strike = aggregate_by_strike(chain)

    tenor_atm_iv: dict[str, float] | None = None
    if args.all_expiries and included_expiries:
        tenor_atm_iv = {}
        for exp in sorted(included_expiries)[:3]:
            exp_chain = chain[chain["expiry"] == exp]
            if exp_chain.empty:
                continue
            exp_by_strike = aggregate_by_strike(exp_chain)
            atm_iv = nearest_atm_iv(exp_by_strike, spot_for_greeks)
            if atm_iv is not None:
                tenor_atm_iv[exp] = atm_iv
    summary = build_summary(
        ticker_symbol,
        expiry,
        spot_for_greeks,
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
    if args.source == "insiderfinance":
        summary_dict.update({
            "chain_source": "insiderfinance", "spot_source": "insiderfinance",
            "source_timestamp": source_timestamp,
            "source_is_stale": if_data.raw.get("isStale"),
            "fetch_source": report_dict["fetch_source"],
            "iv_cboe_count": report_dict["iv_cboe_count"],
            "quote_cboe_count": report_dict["quote_cboe_count"],
            "iv_unavailable_count": report_dict["iv_unavailable_count"],
        })
        summary_dict.update({key: value for key, value in report_dict.items() if key.startswith("volume_")})

    if args.source == "insiderfinance":
        payloads = {"insiderfinance": if_data.raw}
        if volume_data is not None:
            payloads["cboe"] = volume_data.raw
        raw_paths = tuple(output_dir / "raw" / f"{ticker_symbol}_{expiry}_{ts}_{source}.json" for source in payloads)
        for raw_path, payload in zip(raw_paths, payloads.values()):
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        raw_frames = [storage.normalize_raw_chain(
            reconciled, capture_ts=summary_dict["snapshot_utc"],
            ticker=ticker_symbol, source="insiderfinance_reconciled", spot=if_data.spot,
            source_ts=source_timestamp,
        )]
    elif if_data is not None:
        raw_paths = storage.save_raw(
            output_dir,
            ticker_symbol,
            expiry,
            ts,
            if_data.raw,
            ow_raw,
            source_a_name="insiderfinance",
            source_b_name="optionwatch",
        )
        raw_frames = [
            storage.normalize_raw_chain(
                reconciled,
                capture_ts=summary_dict["snapshot_utc"],
                ticker=ticker_symbol,
                source="insiderfinance_optionwatch",
                spot=if_data.spot,
            ),
            storage.normalize_raw_chain(
                volume_chain,
                capture_ts=summary_dict["snapshot_utc"],
                ticker=ticker_symbol,
                source="cboe_yahoo_volume",
                spot=yahoo_data.spot,
            ),
        ]
    else:
        raw_paths = storage.save_raw(
            output_dir, ticker_symbol, expiry, ts, cboe_data.raw, yahoo_chain_to_raw(yahoo_data)
        )
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
    run_cleanup(apply=True, verbose=False)


if __name__ == "__main__":
    main()
