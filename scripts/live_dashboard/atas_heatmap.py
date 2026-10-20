"""Compact 0DTE QQQ/NDX GEX time series for ATAS charts."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def build_atas_heatmap(state: dict, *, ticker: str, strike_range: float = 400) -> dict:
    """Keep every fresh 0DTE valuation; do not relabel classic GEX as dealer flow."""
    ticker = ticker.upper()
    if ticker not in {"NDX", "QQQ"} or not (
        5 <= strike_range <= 200 if ticker == "QQQ" else 50 <= strike_range <= 2000
    ):
        raise ValueError("Heatmap requires QQQ (5-200) or NDX (50-2000) strike range")
    session = state.get("session") or {}
    day = session.get("trading_date")
    market_open = _utc(session.get("market_open_utc"))
    market_close = _utc(session.get("market_close_utc"))
    if not isinstance(day, str) or market_open is None or market_close is None:
        raise ValueError("Missing New York trading session")

    spots = {}
    for point in state.get("points") or []:
        spot = _number(point.get("spot"))
        if spot is not None and spot > 0:
            spots[point.get("time")] = spot

    samples = {}
    for snapshot in state.get("gex_ribbon") or []:
        capture_text = snapshot.get("time")
        capture = _utc(capture_text)
        if (capture is None or not market_open <= capture < market_close or
                snapshot.get("expiry") != day):
            continue
        source = _utc(snapshot.get("source_timestamp"))
        if snapshot.get("source_timestamp") and (source is None or not -120 <= (capture - source).total_seconds() <= 2700):
            continue
        spot = spots.get(capture_text)
        if spot is None:
            continue
        rows = []
        for item in snapshot.get("rows") or []:
            strike = _number(item.get("strike"))
            gex = _number(item.get("net_gex"))
            if strike is not None and strike > 0 and gex is not None and gex != 0 and abs(strike - spot) <= strike_range:
                rows.append({"strike": strike, "net_gex": gex})
        if rows:
            samples[capture_text] = {"time": capture_text, "spot": spot,
                                     "rows": sorted(rows, key=lambda row: row["strike"])}

    ordered = [samples[key] for key in sorted(samples)]
    return {"schema_version": 1, "ticker": ticker, "model": "classic_oi_type_sign_estimate",
            "price_basis": "qqq_etf" if ticker == "QQQ" else "ndx_cash", "trading_date": day, "market_open_utc": market_open.isoformat(),
            "market_close_utc": market_close.isoformat(), "strike_range": strike_range,
            "status": "ok" if ordered else "waiting_0dte", "samples": ordered}
