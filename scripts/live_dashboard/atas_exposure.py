"""Small, versioned, read-only exposure payload for the ATAS X indicator.

This exports the existing OI/type-sign model; it does not infer dealer inventory
or turn a delayed quote into real-time data. Prices remain in the source symbol.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


EXPOSURE_FIELDS = (
    "net_gex", "net_dex", "net_vex", "net_chex",
    "call_gex", "put_gex", "call_dex", "put_dex",
    "call_vex", "put_vex", "call_chex", "put_chex", "call_oi", "put_oi",
)
LEVEL_FIELDS = {
    "gex": ("gamma_wall_abs", "gamma_flip"),
    "dex": ("dex_wall_abs", "delta_flip"),
    "vex": ("vanna_wall_abs", "vanna_flip"),
    "chex": ("charm_wall_abs", "charm_flip"),
}


def finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def parse_utc(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def build_atas_exposure(snapshot: dict, *, ticker: str, basis: str = "intraday",
                        now: datetime | None = None, stale_after: float = 180) -> dict:
    if basis not in {"intraday", "eod"}:
        raise ValueError("basis must be intraday or eod")
    summary_key, rows_key = (
        ("latest_summary", "by_strike") if basis == "intraday"
        else ("levels_summary", "levels_by_strike")
    )
    summary = snapshot.get(summary_key) or {}
    if not summary:
        raise LookupError(f"No {basis} exposure snapshot yet")
    if str(summary.get("ticker") or "").upper() != ticker.upper():
        raise ValueError("Requested ticker does not match the exposure snapshot")
    now = now or datetime.now(timezone.utc)
    timestamp = parse_utc(summary.get("snapshot_utc"))
    age = (now - timestamp).total_seconds() if timestamp else None
    stale = age is None or age < -30 or age > stale_after or bool(summary.get("source_is_stale"))
    session = snapshot.get("session") or {}
    market_open = parse_utc(session.get("market_open_utc"))
    market_close = parse_utc(session.get("market_close_utc"))
    market_open_now = bool(market_open and market_close and market_open <= now < market_close)
    status = "eod" if basis == "eod" else (
        "collector_error" if snapshot.get("latest_error") else
        "session_closed" if market_close and now >= market_close else
        "premarket" if market_open and now < market_open else
        "stale" if stale else "updating"
    )
    rows = []
    seen = set()
    for source in snapshot.get(rows_key) or []:
        strike = finite_number(source.get("strike"))
        if strike is None or strike <= 0:
            continue
        if strike in seen:
            raise ValueError("Duplicate strike in aggregated exposure snapshot")
        seen.add(strike)
        rows.append({"strike": strike, **{k: finite_number(source.get(k)) for k in EXPOSURE_FIELDS}})
    if not rows:
        raise LookupError(f"No {basis} exposure rows yet")
    rows.sort(key=lambda item: item["strike"])
    levels = {"call_resistance": finite_number(summary.get("call_resistance")),
              "put_support": finite_number(summary.get("put_support"))}
    for fields in LEVEL_FIELDS.values():
        levels.update({key: finite_number(summary.get(key)) for key in fields})
    return {
        "schema_version": 1,
        "ticker": ticker.upper(),
        "price_basis": "source",
        "model": "classic_oi_type_sign_estimate",
        "basis": basis,
        "snapshot_utc": timestamp.isoformat() if timestamp else None,
        "generated_utc": now.isoformat(),
        "source_timestamp": summary.get("source_timestamp"),
        "source_is_stale": bool(summary.get("source_is_stale")),
        "chain_source": summary.get("chain_source"),
        "spot_source": summary.get("spot_source"),
        "age_seconds": round(age, 1) if age is not None else None,
        "stale": stale,
        "status": status,
        "market_open": market_open_now,
        "session_locked": bool(snapshot.get("session_locked")),
        "collector_error": bool(snapshot.get("latest_error")),
        "next_fetch": snapshot.get("next_fetch"),
        "expiry": summary.get("expiry"),
        "included_expiries": summary.get("included_expiries") or [],
        "spot": finite_number(summary.get("spot")),
        "net_totals": {key: finite_number(summary.get(key)) for key in EXPOSURE_FIELDS[:4]},
        "row_totals": {key: sum(row[key] or 0 for row in rows) for key in EXPOSURE_FIELDS[:4]},
        "rows_scope": "server_strike_window",
        "levels": levels,
        "rows": rows,
    }


def fetch_qqq_close_1559(trading_date: str) -> dict:
    """Exact completed M1 close only; no nearest-price/daily-close fallback."""
    import pandas as pd
    from sources import yahoo

    day = datetime.strptime(trading_date, "%Y-%m-%d").date()
    target = pd.Timestamp(f"{day.isoformat()} 15:59", tz="America/New_York")
    if target.tz_convert("UTC").to_pydatetime().timestamp() + 60 > datetime.now(timezone.utc).timestamp():
        raise ValueError("The requested 15:59 minute has not closed")
    yf = yahoo.import_yfinance()
    bars = yf.Ticker("QQQ").history(
        start=target.to_pydatetime(), end=(target + pd.Timedelta(minutes=2)).to_pydatetime(),
        interval="1m", prepost=True, auto_adjust=False, timeout=12,
    )
    if bars.empty or "Close" not in bars or bars.index.tz is None:
        raise LookupError("QQQ 15:59 M1 close is unavailable; enter the locked ratio manually")
    exact = bars[bars.index.tz_convert("UTC") == target.tz_convert("UTC")]
    close = finite_number(exact.iloc[-1]["Close"]) if not exact.empty else None
    if close is None or close <= 0:
        raise LookupError("Exact QQQ 15:59 M1 close is missing")
    return {"schema_version": 1, "ticker": "QQQ", "close": close,
            "bar_start_utc": target.tz_convert("UTC").isoformat(),
            "reference_date": day.isoformat(), "source": "yahoo_m1_unadjusted"}
