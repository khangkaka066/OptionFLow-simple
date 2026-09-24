"""Fetch bid/ask size supplement data from Optionwatch's public contracts-snapshot API.

Passive/public only: plain unauthenticated GET, with standard browser-mimicking
`Origin`/`Referer` headers (this is a CORS check on optionwatch.io's side, not
auth — confirmed a bare User-Agent gets HTTP 500, adding these headers gets
HTTP 200; no cookies, tokens, or session state involved).

The encrypted `/api/stock/{ticker}/price` endpoint is never called from this
module and must never be added here.
"""

from __future__ import annotations

import re
import time

import pandas as pd
import requests

OPTIONWATCH_SNAPSHOT_URL = "https://api.optionwatch.io/api/contracts/snapshot/{ticker}/{expiry}"
OW_FETCH_RETRIES = 3
OW_RETRY_BACKOFF_SECONDS = 2.0

_OPTION_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")

CHAIN_COLUMNS = [
    "strike",
    "option_type",
    "expiry",
    "ow_bid_size",
    "ow_ask_size",
    "ow_bid_price",
    "ow_ask_price",
    "ow_last_price",
    "ow_last_size",
    "ow_last_time",
]


def _epoch_ms_to_iso(value) -> str | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, unit="ms", utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.isoformat()


def parse_option_symbol(symbol: str) -> tuple[str, str, float] | None:
    """Parse an OSI-style Optionwatch contract symbol into (expiry YYYY-MM-DD, type, strike)."""
    match = _OPTION_SYMBOL_RE.match(symbol.strip())
    if not match:
        return None
    yy, mm, dd = match["date"][0:2], match["date"][2:4], match["date"][4:6]
    expiry = f"20{yy}-{mm}-{dd}"
    option_type = "call" if match["type"] == "C" else "put"
    strike = int(match["strike"]) / 1000.0
    return expiry, option_type, strike


def _headers(ticker: str) -> dict:
    return {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://optionwatch.io",
        "Referer": f"https://optionwatch.io/quote/{ticker.upper()}",
    }


def fetch_snapshot(ticker: str, expiry: str, timeout: float = 15.0) -> dict:
    url = OPTIONWATCH_SNAPSHOT_URL.format(ticker=ticker.upper(), expiry=expiry)
    last_exc: Exception | None = None
    for attempt in range(OW_FETCH_RETRIES):
        try:
            response = requests.get(url, headers=_headers(ticker), timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < OW_FETCH_RETRIES - 1:
                time.sleep(OW_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_exc


def parse_snapshot(raw: dict, ticker: str) -> pd.DataFrame:
    rows = []
    for symbol, contract in raw.items():
        parsed = parse_option_symbol(symbol)
        if parsed is None:
            continue
        opt_expiry, option_type, strike = parsed
        rows.append(
            {
                "strike": strike,
                "option_type": option_type,
                "expiry": opt_expiry,
                "ow_bid_size": contract.get("bidSize"),
                "ow_ask_size": contract.get("askSize"),
                "ow_bid_price": contract.get("bidPrice"),
                "ow_ask_price": contract.get("askPrice"),
                "ow_last_price": contract.get("lastTradePrice"),
                "ow_last_size": contract.get("lastTradeSize"),
                "ow_last_time": _epoch_ms_to_iso(contract.get("lastTradeDate")),
            }
        )
    return pd.DataFrame(rows, columns=CHAIN_COLUMNS)


def fetch_chain(ticker: str, expiries: list[str], timeout: float = 15.0) -> pd.DataFrame:
    frames = []
    for expiry in expiries:
        raw = fetch_snapshot(ticker, expiry, timeout=timeout)
        frames.append(parse_snapshot(raw, ticker))
    if not frames:
        return pd.DataFrame(columns=CHAIN_COLUMNS)
    return pd.concat(frames, ignore_index=True)
