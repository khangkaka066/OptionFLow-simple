from __future__ import annotations

from datetime import time as dt_time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd


NY_TZ = ZoneInfo("America/New_York")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DEFAULT_COLLECT_START_OFFSET_MIN = 30.0

_collect_start_offset_min = DEFAULT_COLLECT_START_OFFSET_MIN


def set_collect_start_offset_min(value: float) -> None:
    global _collect_start_offset_min
    _collect_start_offset_min = float(value)


def market_session_utc(now_utc: pd.Timestamp | None = None) -> dict:
    """Current NY session, or the latest weekday session when NY is closed."""
    now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
    now_ny = now_utc.tz_convert(NY_TZ)
    trading_date = now_ny.date()
    while trading_date.weekday() >= 5:
        trading_date -= timedelta(days=1)
    open_ny = pd.Timestamp.combine(trading_date, dt_time(9, 30)).tz_localize(NY_TZ)
    close_ny = pd.Timestamp.combine(trading_date, dt_time(16, 0)).tz_localize(NY_TZ)
    return {
        "trading_date": trading_date.isoformat(),
        "market_open_utc": open_ny.tz_convert("UTC").isoformat(),
        "market_close_utc": close_ny.tz_convert("UTC").isoformat(),
    }


def session_for_trading_date(trading_date: str) -> dict:
    day = pd.to_datetime(trading_date, errors="raise").date()
    open_ny = pd.Timestamp.combine(day, dt_time(9, 30)).tz_localize(NY_TZ)
    close_ny = pd.Timestamp.combine(day, dt_time(16, 0)).tz_localize(NY_TZ)
    session = {
        "trading_date": day.isoformat(),
        "market_open_utc": open_ny.tz_convert("UTC").isoformat(),
        "market_close_utc": close_ny.tz_convert("UTC").isoformat(),
    }
    session["collection_start_utc"] = collection_start_utc(session["market_open_utc"])
    return session


def collection_start_utc(market_open_utc: str, offset_minutes: float | None = None) -> str:
    offset = _collect_start_offset_min if offset_minutes is None else offset_minutes
    open_ts = pd.Timestamp(market_open_utc)
    return (open_ts - pd.Timedelta(minutes=offset)).isoformat()
