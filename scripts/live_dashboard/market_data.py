from __future__ import annotations

import os
from collections.abc import Callable

import pandas as pd
import requests

from sources import yahoo

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


def alpaca_credentials() -> tuple[str | None, str | None]:
    return os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")


def candles_for_session(ticker: str, session: dict) -> tuple[list[dict], str | None]:
    api_key, secret_key = alpaca_credentials()
    if not api_key or not secret_key:
        return [], "Alpaca API key not configured"
    try:
        return fetch_alpaca_bars(
            ticker,
            session["market_open_utc"],
            end_iso=session.get("market_close_utc"),
        ), None
    except Exception as exc:
        return [], str(exc)


def fetch_alpaca_bars(ticker: str, start_iso: str, timeframe: str = "1Min", end_iso: str | None = None) -> list[dict]:
    api_key, secret_key = alpaca_credentials()
    if not api_key or not secret_key:
        raise RuntimeError("Alpaca API key not configured")
    params = {
        "timeframe": timeframe,
        "start": start_iso,
        "limit": 1000,
        "feed": "iex",
        "adjustment": "raw",
    }
    if end_iso:
        params["end"] = end_iso
    resp = requests.get(
        ALPACA_DATA_URL.format(symbol=ticker),
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        },
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    bars = resp.json().get("bars") or []
    return [
        {
            "t": bar["t"],
            "o": float(bar["o"]),
            "h": float(bar["h"]),
            "l": float(bar["l"]),
            "c": float(bar["c"]),
            "v": float(bar["v"]),
        }
        for bar in bars
    ]


def fetch_day_high_low(ticker: str) -> tuple[float | None, float | None]:
    try:
        yf = yahoo.import_yfinance()
        ticker_obj = yf.Ticker(yahoo._yahoo_symbol(ticker))
        return yahoo.get_day_high_low(ticker_obj)
    except Exception:
        return None, None


def fetch_futures_spot_at(futures_ticker: str, snapshot_utc: str | None) -> float | None:
    if not futures_ticker or not snapshot_utc:
        return None
    snapshot = pd.to_datetime(snapshot_utc, errors="coerce", utc=True)
    if pd.isna(snapshot):
        return None
    try:
        yf = yahoo.import_yfinance()
        ticker_obj = yf.Ticker(yahoo._yahoo_symbol(futures_ticker))
        bars = ticker_obj.history(
            start=(snapshot - pd.Timedelta(minutes=45)).to_pydatetime(),
            end=(snapshot + pd.Timedelta(minutes=45)).to_pydatetime(),
            interval="1m",
        )
        if bars.empty or "Close" not in bars:
            return None
        bars = bars.copy()
        bars["_ts"] = pd.to_datetime(bars.index, errors="coerce", utc=True)
        bars = bars[bars["_ts"].notna()].sort_values("_ts")
        if bars.empty:
            return None
        prior = bars[bars["_ts"] <= snapshot]
        row = prior.iloc[-1] if not prior.empty else bars.iloc[0]
        close = row.get("Close")
        return float(close) if pd.notna(close) else None
    except Exception:
        return None


def fetch_futures_basis(futures_ticker: str, cash_spot: float, snapshot_utc: str | None = None) -> float | None:
    if not futures_ticker:
        return None
    try:
        futures_spot = fetch_futures_spot_at(futures_ticker, snapshot_utc)
        if futures_spot is None:
            yf = yahoo.import_yfinance()
            futures_spot = yahoo.get_spot(yf.Ticker(yahoo._yahoo_symbol(futures_ticker)))
        return float(futures_spot) - float(cash_spot)
    except Exception:
        return None


def apply_futures_basis(summary: dict | None, futures_ticker: str, tick_size: float | None = 0.25) -> dict | None:
    if summary is None or not futures_ticker or summary.get("spot") is None:
        return summary
    basis = fetch_futures_basis(futures_ticker, summary["spot"], summary.get("snapshot_utc"))
    if basis is not None:
        summary["futures_basis"] = basis
        summary["futures_ticker"] = futures_ticker
        if tick_size is not None and tick_size > 0:
            summary["futures_tick_size"] = tick_size
    return summary


def alpaca_candles_collector(
    ticker: str,
    state,
    *,
    market_session_utc: Callable[[], dict],
    poll_seconds: int = 30,
) -> None:
    while True:
        with state.lock:
            if not state.running:
                break
        api_key, secret_key = alpaca_credentials()
        if not api_key or not secret_key:
            with state.lock:
                state.candles_error = "Alpaca API key not configured"
            sleep_fn(poll_seconds)
            continue
        try:
            start_iso = market_session_utc()["market_open_utc"]
            bars = fetch_alpaca_bars(ticker, start_iso)
            with state.lock:
                state.candles = bars
                state.candles_error = None
        except Exception as exc:
            with state.lock:
                state.candles_error = str(exc)
        sleep_fn(poll_seconds)


def sleep_fn(seconds: int) -> None:
    import time

    time.sleep(seconds)
