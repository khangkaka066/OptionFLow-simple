#!/usr/bin/env python3
"""Backfill the columnar data/market dataset from existing options snapshots."""

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
    parser = argparse.ArgumentParser(description="Backfill normalized data/market parquet dataset.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", default=None, help="Only backfill one data/options/YYYY-MM-DD folder.")
    return parser.parse_args()


def yahoo_raw_frame(raw_path: Path, capture_ts: str, ticker: str) -> pd.DataFrame | None:
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    rows = []
    for side in ["calls", "puts"]:
        rows.extend(raw.get(side, []))
    if not rows:
        return None
    return storage.normalize_raw_chain(
        pd.DataFrame(rows),
        capture_ts=capture_ts,
        ticker=ticker,
        source="yahoo",
        spot=raw.get("spot"),
    )


def timestamp_from_name(path: Path, ticker: str, expiry: str) -> str | None:
    prefix = f"{ticker}_{expiry}_"
    suffix = "_summary.json"
    if not path.name.startswith(prefix) or not path.name.endswith(suffix):
        return None
    value = path.name[len(prefix) : -len(suffix)]
    return value if value.isdigit() else None


def rows_for_summary(summary_path: Path, ticker: str) -> dict | None:
    output_dir = summary_path.parent
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    expiry = str(summary.get("expiry") or "")
    capture_ts = summary.get("snapshot_utc")
    if not expiry or not capture_ts:
        return None
    by_strike_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))
    if not by_strike_path.exists():
        return None

    by_strike = pd.read_parquet(by_strike_path)
    trading_date = summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or output_dir.name
    expiry_scope = summary.get("expiry") or expiry

    raw_frames = []
    ts = timestamp_from_name(summary_path, ticker, expiry)
    if ts:
        raw_path = output_dir / "raw" / f"{ticker}_{expiry}_{ts}_yahoo.json"
        if raw_path.exists():
            raw_frame = yahoo_raw_frame(raw_path, capture_ts, ticker)
            if raw_frame is not None:
                raw_frames.append(raw_frame)

    strike = by_strike.copy()
    strike.insert(0, "capture_ts", capture_ts)
    strike.insert(1, "ticker", ticker)
    strike.insert(2, "expiry_scope", expiry_scope)
    if "expiry" not in strike:
        strike.insert(3, "expiry", expiry)

    intraday = storage.build_intraday_rows(
        strike, summary, capture_ts=capture_ts, ticker=ticker, trading_date=trading_date
    )

    skew_cols = [col for col in ["capture_ts", "ticker", "expiry", "strike", "call_iv", "put_iv", "iv"] if col in strike]
    skew = strike.loc[:, skew_cols].copy()
    spot = float(summary.get("spot") or 0)
    if spot > 0 and "strike" in skew:
        rel = (pd.to_numeric(skew["strike"], errors="coerce") / spot) - 1.0
        skew["moneyness"] = pd.cut(rel.abs(), [-0.001, 0.01, 0.05, float("inf")], labels=["ATM", "NEAR", "FAR"]).astype(str)
    else:
        skew["moneyness"] = None

    session = pd.DataFrame([{
        "trading_date": trading_date,
        "capture_ts": capture_ts,
        "ticker": ticker,
        "expiry_scope": expiry_scope,
        "spot": summary.get("spot"),
        "avg_iv": summary.get("avg_iv"),
        "gamma_flip": summary.get("gamma_flip"),
        "delta_flip": summary.get("delta_flip"),
        "gamma_wall": summary.get("gamma_wall_abs"),
        "dex_wall": summary.get("dex_wall_abs"),
        "call_resistance": summary.get("call_resistance"),
        "put_support": summary.get("put_support"),
    }])
    return {
        "output_dir": output_dir,
        "trading_date": trading_date,
        "raw_chain": pd.concat(raw_frames, ignore_index=True) if raw_frames else None,
        "by_strike": strike,
        "intraday_metrics": pd.DataFrame(intraday),
        "skew_snapshot": skew,
        "session_levels": session,
    }


def main() -> None:
    args = parse_args()
    ticker = args.ticker.upper()
    roots = [args.data_root / args.date] if args.date else sorted(path for path in args.data_root.iterdir() if path.is_dir())
    count = 0
    for output_dir in roots:
        grouped: dict[str, dict[str, list[pd.DataFrame]]] = {}
        session_frames: list[pd.DataFrame] = []
        for summary_path in sorted(output_dir.glob(f"{ticker}_*_summary.json")):
            result = rows_for_summary(summary_path, ticker)
            if result is None:
                continue
            count += 1
            base = storage.market_day_dir(output_dir, ticker, result["trading_date"])
            tables = grouped.setdefault(str(base), {"raw_chain": [], "by_strike": [], "intraday_metrics": [], "skew_snapshot": []})
            for name in tables:
                frame = result.get(name)
                if frame is not None and not frame.empty:
                    tables[name].append(frame)
            session_frames.append(result["session_levels"])

        for base_text, tables in grouped.items():
            base = Path(base_text)
            if tables["raw_chain"]:
                storage.append_market_table(base / "raw_chain.parquet", pd.concat(tables["raw_chain"], ignore_index=True), ["capture_ts", "source", "ticker", "expiry", "strike", "option_type"])
            if tables["by_strike"]:
                storage.append_market_table(base / "by_strike.parquet", pd.concat(tables["by_strike"], ignore_index=True), ["capture_ts", "ticker", "expiry_scope", "expiry", "strike"])
            if tables["intraday_metrics"]:
                storage.append_market_table(base / "intraday_metrics.parquet", pd.concat(tables["intraday_metrics"], ignore_index=True), ["bucket_ts", "ticker", "metric_name", "strike"])
            if tables["skew_snapshot"]:
                storage.append_market_table(base / "skew_snapshot.parquet", pd.concat(tables["skew_snapshot"], ignore_index=True), ["capture_ts", "ticker", "expiry", "strike"])
        if session_frames:
            market_root = output_dir.parents[1] / "market"
            storage.append_market_table(market_root / "session_levels_intraday.parquet", pd.concat(session_frames, ignore_index=True), ["trading_date", "capture_ts", "ticker", "expiry_scope"])

    backfill_daily_session_levels(output_dir.parents[1] / "market", ticker)
    print(f"Backfilled {count} snapshots into data/market.")


def backfill_daily_session_levels(market_root: Path, ticker: str) -> int:
    """Roll session_levels_intraday.parquet up to one row/day in session_levels_daily.sqlite.

    iv_rank is computed chronologically (each day only sees prior days), matching
    how it is computed live in storage.upsert_daily_session_level.
    """
    # storage.market_day_dir() expects an "options"-sibling dir to locate market_root;
    # this placeholder path is only used for that arithmetic, never read from disk.
    options_placeholder = market_root.parent / "options" / "_"
    intraday_path = market_root / "session_levels_intraday.parquet"
    if not intraday_path.exists():
        return 0
    intraday = pd.read_parquet(intraday_path)
    intraday = intraday[intraday["ticker"] == ticker]
    if intraday.empty:
        return 0
    intraday = intraday.sort_values(["trading_date", "capture_ts"])
    updated = 0
    for trading_date, group in intraday.groupby("trading_date"):
        last_row = group.iloc[-1]
        level_row = {
            "spot": last_row.get("spot"),
            "avg_iv": last_row.get("avg_iv"),
            "gamma_flip": last_row.get("gamma_flip"),
            "delta_flip": last_row.get("delta_flip"),
            "gamma_wall": last_row.get("gamma_wall"),
            "dex_wall": last_row.get("dex_wall"),
            "call_resistance": last_row.get("call_resistance"),
            "put_support": last_row.get("put_support"),
        }
        storage.upsert_daily_session_level(
            options_placeholder,
            ticker=ticker,
            trading_date=str(trading_date),
            level_row=level_row,
        )
        updated += 1
    return updated


if __name__ == "__main__":
    main()
