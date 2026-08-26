"""Fetch structural option chain data (strikes/expiries/OI) from CBOE's free delayed quotes feed.

This hits the unofficial, unauthenticated endpoint that powers the public
cdn.cboe.com delayed-quotes pages. It is not a supported/paid CBOE API: the
shape or availability can change without notice. It is used here only for
open interest / contract structure, cross-checked against Yahoo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd
import requests

CBOE_DELAYED_QUOTES_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

# CBOE delayed-quotes convention: cash-settled index roots are underscore-prefixed.
CBOE_SYMBOL_OVERRIDES = {
    "NDX": "_NDX",
}

_OPTION_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


@dataclass
class CboeChain:
    ticker: str
    underlying_price: float | None
    raw: dict
    chain: pd.DataFrame  # columns: strike, option_type, expiry, cboe_oi, cboe_volume, cboe_bid, cboe_ask, cboe_iv


def parse_option_symbol(symbol: str) -> tuple[str, str, float] | None:
    """Parse an OSI-style CBOE contract symbol into (expiry YYYY-MM-DD, type, strike)."""
    match = _OPTION_SYMBOL_RE.match(symbol.strip())
    if not match:
        return None
    yy, mm, dd = match["date"][0:2], match["date"][2:4], match["date"][4:6]
    expiry = f"20{yy}-{mm}-{dd}"
    option_type = "call" if match["type"] == "C" else "put"
    strike = int(match["strike"]) / 1000.0
    return expiry, option_type, strike


def fetch_raw(ticker: str, timeout: float = 15.0) -> dict:
    symbol = CBOE_SYMBOL_OVERRIDES.get(ticker.upper(), ticker.upper())
    url = CBOE_DELAYED_QUOTES_URL.format(symbol=symbol)
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_chain(raw: dict, ticker: str, expiry: str | None = None) -> CboeChain:
    data = raw.get("data", raw)
    underlying_price = None
    for key in ("current_price", "last_trade_price", "close"):
        value = data.get(key)
        if value is not None:
            underlying_price = float(value)
            break

    rows = []
    for opt in data.get("options", []):
        parsed = parse_option_symbol(opt.get("option", ""))
        if parsed is None:
            continue
        opt_expiry, option_type, strike = parsed
        if expiry is not None and opt_expiry != expiry:
            continue
        rows.append(
            {
                "strike": strike,
                "option_type": option_type,
                "expiry": opt_expiry,
                "cboe_oi": opt.get("open_interest"),
                "cboe_volume": opt.get("volume"),
                "cboe_bid": opt.get("bid"),
                "cboe_ask": opt.get("ask"),
                "cboe_iv": opt.get("iv"),
            }
        )

    chain = pd.DataFrame(
        rows,
        columns=["strike", "option_type", "expiry", "cboe_oi", "cboe_volume", "cboe_bid", "cboe_ask", "cboe_iv"],
    )
    return CboeChain(ticker=ticker.upper(), underlying_price=underlying_price, raw=raw, chain=chain)


def fetch_chain(ticker: str, expiry: str | None = None, timeout: float = 15.0) -> CboeChain:
    raw = fetch_raw(ticker, timeout=timeout)
    return parse_chain(raw, ticker, expiry)
