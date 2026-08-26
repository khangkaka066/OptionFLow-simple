"""Black-Scholes gamma/delta and time-to-expiry (including intraday T for 0DTE)."""

from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import norm


def bs_gamma(spot: float, strike: float, years: float, rate: float, iv: float) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0 or not np.isfinite(iv):
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * years) / (iv * math.sqrt(years))
    return float(norm.pdf(d1) / (spot * iv * math.sqrt(years)))


def bs_delta(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    iv: float,
    option_type: str,
) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0 or not np.isfinite(iv):
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * years) / (iv * math.sqrt(years))
    if option_type == "call":
        return float(norm.cdf(d1))
    return float(norm.cdf(d1) - 1.0)


def bs_vanna(spot: float, strike: float, years: float, rate: float, iv: float) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0 or not np.isfinite(iv):
        return 0.0
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return float(-norm.pdf(d1) * d2 / iv)


def bs_charm(spot: float, strike: float, years: float, rate: float, iv: float) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0 or not np.isfinite(iv):
        return 0.0
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return float(norm.pdf(d1) * (d2 / (2 * years) - rate / (iv * sqrt_t)))


def bs_price(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    iv: float,
    option_type: str,
) -> float:
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0 or not np.isfinite(iv):
        return float("nan")
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * years) / (iv * math.sqrt(years))
    d2 = d1 - iv * math.sqrt(years)
    discount = math.exp(-rate * years)
    if option_type == "call":
        return float(spot * norm.cdf(d1) - strike * discount * norm.cdf(d2))
    return float(strike * discount * norm.cdf(-d2) - spot * norm.cdf(-d1))


def implied_volatility_from_price(
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    option_type: str,
    min_iv: float = 1e-4,
    max_iv: float = 5.0,
) -> float:
    """Invert Black-Scholes using a bid/ask mid price.

    The dashboard has no reliable free dividend-yield input, so this solver uses
    q=0. It is intentionally strict: stale or crossed quotes should return NaN
    instead of creating impossible IV jumps near ATM.
    """
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return float("nan")
    if option_type not in {"call", "put"}:
        return float("nan")

    discount = math.exp(-rate * years)
    intrinsic = max(spot - strike * discount, 0.0) if option_type == "call" else max(strike * discount - spot, 0.0)
    if price < intrinsic - 1e-6:
        return float("nan")

    low_price = bs_price(spot, strike, years, rate, min_iv, option_type)
    high_price = bs_price(spot, strike, years, rate, max_iv, option_type)
    if not np.isfinite(low_price) or not np.isfinite(high_price):
        return float("nan")
    if price < low_price - 1e-6 or price > high_price + 1e-6:
        return float("nan")

    low, high = min_iv, max_iv
    for _ in range(80):
        mid = (low + high) / 2.0
        model_price = bs_price(spot, strike, years, rate, mid, option_type)
        if not np.isfinite(model_price):
            return float("nan")
        if model_price < price:
            low = mid
        else:
            high = mid
    return float((low + high) / 2.0)


def years_to_expiry(expiry: str, snapshot_day: date) -> tuple[int, float]:
    """Intraday-aware T: same-day (0DTE) uses minutes remaining to the 16:00 NY close."""
    expiry_day = datetime.strptime(expiry, "%Y-%m-%d").date()
    days = max((expiry_day - snapshot_day).days, 0)
    if days == 0:
        ny_now = datetime.now(ZoneInfo("America/New_York"))
        if snapshot_day == ny_now.date():
            close_time = ny_now.replace(hour=16, minute=0, second=0, microsecond=0)
            remaining_minutes = max((close_time - ny_now).total_seconds() / 60.0, 30.0)
        else:
            # Historical same-day recompute has no intraday timestamp, so use one full
            # regular session as a conservative 0DTE approximation.
            remaining_minutes = 6.5 * 60
        years = remaining_minutes / (365.0 * 24.0 * 60.0)
    else:
        years = days / 365.0
    return days, years


def years_from_override(days_float: float) -> tuple[int, float]:
    days = max(float(days_float), 0.0)
    years = max(days / 365.0, 1 / (365.0 * 6.5 * 60))
    return int(days), years
