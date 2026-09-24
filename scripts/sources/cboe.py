"""Fetch structural option chain data (strikes/expiries/OI) from CBOE's free delayed quotes feed.

This hits the unofficial, unauthenticated endpoint that powers the public
cdn.cboe.com delayed-quotes pages. It is not a supported/paid CBOE API: the
shape or availability can change without notice. It is used here only for
open interest / contract structure, cross-checked against Yahoo.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import pandas as pd
import requests

CBOE_DELAYED_QUOTES_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
CBOE_FETCH_RETRIES = 3
CBOE_RETRY_BACKOFF_SECONDS = 2.0

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
    last_exc: Exception | None = None
    for attempt in range(CBOE_FETCH_RETRIES):
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < CBOE_FETCH_RETRIES - 1:
                time.sleep(CBOE_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_exc


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


def supplement_volume(primary: pd.DataFrame, cboe_chain: pd.DataFrame) -> pd.DataFrame:
    """Attach volume without changing the primary provider's contract universe or quotes.

    Index roots with different settlement conventions can share these keys.
    Without a contract root in the primary data, ambiguous matches stay missing.
    """
    keys = ["strike", "option_type", "expiry"]
    volumes = cboe_chain[keys + ["cboe_volume"]].copy()
    volumes = volumes.loc[~volumes.duplicated(keys, keep=False)]
    values = pd.to_numeric(volumes["cboe_volume"], errors="coerce")
    volumes["cboe_volume"] = values.where((values >= 0) & (values < float("inf")))
    merged = primary.drop(columns=["volume", "volume_source"], errors="ignore").merge(
        volumes, on=keys, how="left", validate="many_to_one", sort=False,
    )
    merged["volume"] = merged.pop("cboe_volume")
    merged["volume_source"] = merged["volume"].notna().map({True: "cboe", False: "unavailable"})
    return merged


def supplement_quotes(primary: pd.DataFrame, cboe_chain: pd.DataFrame) -> pd.DataFrame:
    """Fill invalid IV and quote pairs; keep InsiderFinance OI, spot and good quotes."""
    keys = ["strike", "option_type", "expiry"]
    quotes = cboe_chain[keys + ["cboe_iv", "cboe_bid", "cboe_ask"]].copy()
    quotes = quotes.loc[~quotes.duplicated(keys, keep=False)]
    merged = primary.merge(quotes, on=keys, how="left", validate="many_to_one", sort=False)
    for column in ["impliedVolatility", "bid", "ask", "cboe_iv", "cboe_bid", "cboe_ask"]:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
    primary_iv_ok = merged["impliedVolatility"].between(0.01, 5.0)
    fallback_iv_ok = merged["cboe_iv"].between(0.01, 5.0)
    use_iv = ~primary_iv_ok & fallback_iv_ok
    merged["iv_source"] = primary_iv_ok.map({True: "insiderfinance", False: "unavailable"})
    merged.loc[use_iv, "impliedVolatility"] = merged.loc[use_iv, "cboe_iv"]
    merged.loc[use_iv, "iv_source"] = "cboe"

    def valid_quotes(bid: pd.Series, ask: pd.Series) -> pd.Series:
        return (bid > 0) & (ask >= bid) & (ask < float("inf"))

    primary_quotes_ok = valid_quotes(merged["bid"], merged["ask"])
    use_quotes = ~primary_quotes_ok & valid_quotes(merged["cboe_bid"], merged["cboe_ask"])
    merged["quote_source"] = primary_quotes_ok.map({True: "insiderfinance", False: "unavailable"})
    # Replace the pair together: never synthesize a spread from two providers.
    merged.loc[use_quotes, ["bid", "ask"]] = merged.loc[use_quotes, ["cboe_bid", "cboe_ask"]].to_numpy()
    merged.loc[use_quotes, "quote_source"] = "cboe"
    return merged.drop(columns=["cboe_iv", "cboe_bid", "cboe_ask"])
