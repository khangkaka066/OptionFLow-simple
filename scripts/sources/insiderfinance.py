"""Fetch the primary option chain (OI/IV/bid/ask) from InsiderFinance's gamma-exposure page.

Passive/public only: plain unauthenticated GET of the rendered page, reading the
Next.js `__NEXT_DATA__` payload embedded in the HTML. No login, no private API,
no auth headers. delta/gamma are carried through for reference only — the
downstream pipeline computes its own BSM greeks from impliedVolatility.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import pandas as pd
import requests

INSIDERFINANCE_URL = "https://www.insiderfinance.io/gamma-exposure/{ticker}"
INSIDERFINANCE_SPOT_URL = "https://www.insiderfinance.io/api/v1/tickers/details/{ticker}"
IF_FETCH_RETRIES = 3
IF_RETRY_BACKOFF_SECONDS = 2.0

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

CHAIN_COLUMNS = [
    "strike",
    "option_type",
    "expiry",
    "if_oi",
    "if_iv",
    "if_bid",
    "if_ask",
    "if_delta",
    "if_gamma",
]


@dataclass
class InsiderFinanceChain:
    ticker: str
    spot: float | None
    raw: dict
    chain: pd.DataFrame  # columns: CHAIN_COLUMNS


def fetch_raw(ticker: str, timeout: float = 20.0) -> dict:
    url = INSIDERFINANCE_URL.format(ticker=ticker.upper())
    last_exc: Exception | None = None
    for attempt in range(IF_FETCH_RETRIES):
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            response.raise_for_status()
            match = _NEXT_DATA_RE.search(response.text)
            if match is None:
                raise ValueError("insiderfinance: __NEXT_DATA__ script not found in response")
            next_data = json.loads(match.group(1))
            initial_data = next_data["props"]["pageProps"]["initialData"]
            if isinstance(initial_data, str):
                initial_data = json.loads(initial_data)
            return initial_data
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < IF_FETCH_RETRIES - 1:
                time.sleep(IF_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_exc


def fetch_spot(ticker: str, timeout: float = 20.0) -> float | None:
    """Fetch a near-real-time spot price from InsiderFinance's ticker-details API.

    Unlike the gamma-exposure page (a cached/periodic snapshot), this endpoint
    updates on every request and is the preferred spot source for greeks.
    """
    url = INSIDERFINANCE_SPOT_URL.format(ticker=ticker.upper())
    last_exc: Exception | None = None
    for attempt in range(IF_FETCH_RETRIES):
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            response.raise_for_status()
            price = response.json().get("profile", {}).get("price")
            return float(price) if price is not None else None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < IF_FETCH_RETRIES - 1:
                time.sleep(IF_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_exc


def parse_chain(raw: dict, ticker: str, expiry: str | None = None) -> InsiderFinanceChain:
    spot = raw.get("spot")
    spot = float(spot) if spot is not None else None

    rows = []
    for opt in raw.get("options", []):
        try:
            opt_expiry = f"{int(opt['expireYear']):04d}-{int(opt['expireMonth']):02d}-{int(opt['expireDay']):02d}"
        except (KeyError, TypeError, ValueError):
            continue
        if expiry is not None and opt_expiry != expiry:
            continue
        cp = opt.get("cp")
        option_type = "call" if cp == "C" else "put" if cp == "P" else None
        if option_type is None:
            continue
        rows.append(
            {
                "strike": opt.get("strike"),
                "option_type": option_type,
                "expiry": opt_expiry,
                "if_oi": opt.get("openInterest"),
                "if_iv": opt.get("impliedVol"),
                "if_bid": opt.get("bid"),
                "if_ask": opt.get("ask"),
                "if_delta": opt.get("delta"),
                "if_gamma": opt.get("gamma"),
            }
        )

    chain = pd.DataFrame(rows, columns=CHAIN_COLUMNS)
    return InsiderFinanceChain(ticker=ticker.upper(), spot=spot, raw=raw, chain=chain)


def fetch_chain(ticker: str, expiry: str | None = None, timeout: float = 20.0) -> InsiderFinanceChain:
    raw = fetch_raw(ticker, timeout=timeout)
    return parse_chain(raw, ticker, expiry)
