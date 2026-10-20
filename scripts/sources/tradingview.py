"""Fetch intraday OHLCV candles from TradingView via tvdatafeed.

tvdatafeed replays TradingView's own websocket feed (the same data the
desktop/web chart renders), so it tracks TradingView's displayed price
without needing the desktop app open. It is an unofficial scrape of an
undocumented protocol, not a published API, so it can break without notice
if TradingView changes it.
"""

from __future__ import annotations

import os
import time

import pandas as pd

# tvdatafeed bar timestamps come back in the exchange's local timezone, not
# UTC, so they must be localized before converting to the UTC ISO strings
# the rest of the dashboard expects (matching the Alpaca bar shape).
EXCHANGE_TZ = "America/New_York"

# TradingView's primary listing exchange per ticker, used when the caller
# doesn't pass one explicitly.
TV_EXCHANGE_OVERRIDES = {
    "QQQ": "NASDAQ",
    "NDX": "NASDAQ",
}

_client = None

# In nologin mode, TradingView's websocket closes the connection before
# sending data for some symbols (observed with index tickers like NDX),
# which the tvdatafeed library logs as "Connection to remote host was
# lost" / "no data" rather than raising. Retrying every poll cycle just
# spams those logs without ever succeeding, so once it fails a few times
# in a row we back off for a while before trying again.
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 300
_consecutive_failures = 0
_cooldown_until = 0.0


def import_tvdatafeed():
    try:
        from tvDatafeed import Interval, TvDatafeed
    except ModuleNotFoundError as exc:
        # Raised as RuntimeError (not SystemExit) because callers in the live
        # dashboard treat TradingView as an optional source and fall back to
        # Alpaca/Yahoo on failure; SystemExit would escape a plain `except
        # Exception` and silently kill the polling thread instead.
        raise RuntimeError(
            "Missing dependency: tvdatafeed. Install with:\n"
            "  python3 -m pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git"
        ) from exc
    return TvDatafeed, Interval


def get_client():
    global _client
    if _client is not None:
        return _client
    TvDatafeed, _ = import_tvdatafeed()
    username = os.environ.get("TV_USERNAME")
    password = os.environ.get("TV_PASSWORD")
    _client = TvDatafeed(username, password) if username and password else TvDatafeed()
    return _client


def _tv_exchange(ticker: str) -> str:
    return TV_EXCHANGE_OVERRIDES.get(ticker.upper(), "NASDAQ")


def fetch_bars(ticker: str, n_bars: int = 1500, exchange: str | None = None) -> list[dict]:
    """1-minute OHLCV bars for `ticker`, oldest first, same shape as Alpaca bars."""
    global _consecutive_failures, _cooldown_until

    now = time.monotonic()
    if now < _cooldown_until:
        return []

    _, Interval = import_tvdatafeed()
    tv = get_client()
    df = tv.get_hist(
        symbol=ticker.upper(),
        exchange=exchange or _tv_exchange(ticker),
        interval=Interval.in_1_minute,
        n_bars=n_bars,
    )
    if df is None or df.empty:
        _consecutive_failures += 1
        if _consecutive_failures >= _FAILURE_THRESHOLD:
            _cooldown_until = now + _COOLDOWN_SECONDS
        return []

    _consecutive_failures = 0
    _cooldown_until = 0.0
    bars = []
    for ts, row in df.iterrows():
        utc_ts = pd.Timestamp(ts).tz_localize(EXCHANGE_TZ).tz_convert("UTC")
        bars.append(
            {
                "t": utc_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"]),
            }
        )
    return bars
