"""Fetch spot price + option chain (bid/ask/IV) from Yahoo Finance via yfinance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd


# Yahoo Finance index notation.
YAHOO_SYMBOL_OVERRIDES = {
    "NDX": "^NDX",
    "NQ1!": "NQ=F",
}


def _yahoo_symbol(ticker: str) -> str:
    return YAHOO_SYMBOL_OVERRIDES.get(ticker.upper(), ticker.upper())


@dataclass
class YahooChain:
    ticker: str
    expiry: str
    spot: float
    calls: pd.DataFrame
    puts: pd.DataFrame
    available_expirations: list[str]
    included_expirations: list[str] = field(default_factory=list)


def import_yfinance():
    try:
        import yfinance as yf
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: yfinance. Install with:\n"
            "  python3 -m pip install -r requirements-options.txt\n"
            "or:\n"
            "  python3 -m pip install yfinance"
        ) from exc
    return yf


def get_spot(ticker_obj) -> float:
    """Live spot price, aware of pre-market state.

    Before the open, `regularMarketPrice`/history close/fast_info.last_price
    all still reflect the previous session's close, not the live pre-market
    tape, so pre-market uses Yahoo's dedicated `preMarketPrice` field.

    After the close, `postMarketPrice` is intentionally NOT preferred: it is a
    thin, low-volume tape that can wander noticeably from the official close
    (observed ~1% drift), which would make EOD-labeled snapshots inconsistent
    depending on exactly when they were polled. `regularMarketPrice` (the
    official close) is used instead and stays stable for the rest of the day.
    """
    try:
        info = ticker_obj.info
    except Exception:
        info = {}

    market_state = info.get("marketState")
    if market_state == "PRE" and info.get("preMarketPrice"):
        return float(info["preMarketPrice"])
    if info.get("regularMarketPrice"):
        return float(info["regularMarketPrice"])

    try:
        last_price = ticker_obj.fast_info.last_price
        if last_price:
            return float(last_price)
    except Exception:
        pass

    history = ticker_obj.history(period="5d")
    if history.empty or "Close" not in history:
        raise RuntimeError("Could not fetch recent close from Yahoo Finance.")
    return float(history["Close"].dropna().iloc[-1])


def clean_chain(chain: pd.DataFrame, option_type: str, expiry: str) -> pd.DataFrame:
    data = chain.copy()
    data["option_type"] = option_type
    data["expiry"] = expiry
    for col in ["strike", "openInterest", "impliedVolatility", "volume", "bid", "ask"]:
        if col not in data:
            data[col] = np.nan
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data["openInterest"] = data["openInterest"].fillna(0)
    data["impliedVolatility"] = data["impliedVolatility"].replace([np.inf, -np.inf], np.nan)
    return data


def fetch_chain(ticker: str, expiry: str | None = None) -> YahooChain:
    yf = import_yfinance()
    ticker_obj = yf.Ticker(_yahoo_symbol(ticker))
    expirations = list(ticker_obj.options)
    if not expirations:
        raise RuntimeError(f"No Yahoo option expirations returned for {ticker.upper()}.")

    resolved_expiry = expiry or expirations[0]
    if resolved_expiry not in expirations:
        raise ValueError(
            f"Expiry {resolved_expiry} not found. First available expirations: {expirations[:10]}"
        )

    spot = get_spot(ticker_obj)
    raw_chain = ticker_obj.option_chain(resolved_expiry)
    calls = clean_chain(raw_chain.calls, "call", resolved_expiry)
    puts = clean_chain(raw_chain.puts, "put", resolved_expiry)
    return YahooChain(
        ticker=ticker.upper(),
        expiry=resolved_expiry,
        spot=spot,
        calls=calls,
        puts=puts,
        available_expirations=expirations,
        included_expirations=[resolved_expiry],
    )


def fetch_multi_chain(ticker: str, horizon_days: int = 45) -> YahooChain:
    """Fetch and concatenate option chains for every expiry within `horizon_days`.

    Always includes at least the nearest expiry, even if it falls outside the
    horizon, so the result is never empty.
    """
    yf = import_yfinance()
    ticker_obj = yf.Ticker(_yahoo_symbol(ticker))
    expirations = list(ticker_obj.options)
    if not expirations:
        raise RuntimeError(f"No Yahoo option expirations returned for {ticker.upper()}.")

    cutoff = date.today() + timedelta(days=horizon_days)
    included = [exp for exp in expirations if datetime.strptime(exp, "%Y-%m-%d").date() <= cutoff]
    if not included:
        included = [expirations[0]]

    spot = get_spot(ticker_obj)
    calls_frames = []
    puts_frames = []
    for exp in included:
        raw_chain = ticker_obj.option_chain(exp)
        calls_frames.append(clean_chain(raw_chain.calls, "call", exp))
        puts_frames.append(clean_chain(raw_chain.puts, "put", exp))

    calls = pd.concat(calls_frames, ignore_index=True)
    puts = pd.concat(puts_frames, ignore_index=True)
    return YahooChain(
        ticker=ticker.upper(),
        expiry="ALL",
        spot=spot,
        calls=calls,
        puts=puts,
        available_expirations=expirations,
        included_expirations=included,
    )


def infer_latest_trade_date(calls: pd.DataFrame, puts: pd.DataFrame) -> date | None:
    dates = []
    for data in [calls, puts]:
        if "lastTradeDate" not in data:
            continue
        parsed = pd.to_datetime(data["lastTradeDate"], errors="coerce", utc=True)
        if parsed.notna().any():
            dates.append(parsed.max().date())
    if not dates:
        return None
    return max(dates)


def get_day_high_low(ticker_obj) -> tuple[float | None, float | None]:
    """Intraday 1D low/high so far, from 1-minute bars of the current session."""
    try:
        bars = ticker_obj.history(period="1d", interval="1m")
    except Exception:
        return None, None
    if bars.empty or "High" not in bars or "Low" not in bars:
        return None, None
    high = bars["High"].dropna()
    low = bars["Low"].dropna()
    if high.empty or low.empty:
        return None, None
    return float(low.min()), float(high.max())


def effective_snapshot_day(
    requested_snapshot_day: date,
    calls: pd.DataFrame,
    puts: pd.DataFrame,
) -> date:
    latest_trade_day = infer_latest_trade_date(calls, puts)
    if latest_trade_day is not None and latest_trade_day < requested_snapshot_day:
        print(
            "NOTICE: Yahoo option chain appears stale relative to requested snapshot date. "
            f"latest lastTradeDate={latest_trade_day}, requested snapshot={requested_snapshot_day}. "
            f"Using effective snapshot date {latest_trade_day} for T-to-expiry."
        )
        return latest_trade_day
    return requested_snapshot_day
