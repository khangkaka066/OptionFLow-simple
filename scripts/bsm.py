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
