from __future__ import annotations

from collections.abc import Callable

import pandas as pd

DEFAULT_WINDOW = 14.0
FLAT_TOLERANCE_PCT = 0.05  # OTM IV points; smaller deltas read as FLAT, not RISE/FALL

SeedSessionData = Callable[[str, dict, float], tuple[list[dict], list[dict]]]
SessionForTradingDate = Callable[[str], dict]


def previous_trading_dates(trading_date: str, count: int) -> list[str]:
    """Earlier NY trading dates, oldest first. Weekday-only, like the rest of
    this dashboard's session helpers — does not know about market holidays."""
    day = pd.to_datetime(trading_date).date()
    out: list[str] = []
    while len(out) < count:
        day = day - pd.Timedelta(days=1)
        if day.weekday() < 5:
            out.append(day.isoformat())
    return list(reversed(out))


def otm_iv(point: dict) -> float | None:
    """Median IV across OTM legs only (calls above spot, puts below spot),
    matching frontend/assets/flow-panels.js:moneynessAggregate(..., "OTM")."""
    spot = point.get("spot")
    chain = point.get("chain") or []
    if spot is None or not chain:
        atm = point.get("atm_iv")
        return float(atm) if atm is not None else None
    strikes = sorted({row["k"] for row in chain if row.get("k") is not None})
    if len(strikes) > 1:
        diffs = [abs(strikes[i] - strikes[i - 1]) for i in range(1, len(strikes))]
        step = (sum(diffs) / len(diffs)) if diffs else 1.0
    else:
        step = 1.0
    samples: list[float] = []
    for row in chain:
        k = row.get("k")
        if k is None:
            continue
        dist = k - spot
        if abs(dist) <= step:
            continue  # ATM band, not OTM
        if dist > 0 and row.get("ci") is not None:
            samples.append(float(row["ci"]))
        elif dist < 0 and row.get("pi") is not None:
            samples.append(float(row["pi"]))
    if not samples:
        return None
    samples.sort()
    n = len(samples)
    mid = n // 2
    return samples[mid] if n % 2 else (samples[mid - 1] + samples[mid]) / 2.0


def branch_values(points: list[dict]) -> tuple[float | None, float | None]:
    """(EOD-close OTM IV, session-average OTM IV) for one trading day."""
    series = [v for p in points if (v := otm_iv(p)) is not None]
    if not series:
        return None, None
    return series[-1], sum(series) / len(series)


def classify(delta: float | None, tol: float = FLAT_TOLERANCE_PCT) -> str:
    if delta is None:
        return "UNKNOWN"
    if delta > tol:
        return "RISE"
    if delta < -tol:
        return "FALL"
    return "FLAT"


def verdict_for_direction(direction: str, spot_above_strike: bool) -> str:
    if direction not in ("RISE", "FALL"):
        return "NO EDGE"
    sign_iv = 1 if direction == "RISE" else -1
    sign_spot = 1 if spot_above_strike else -1
    return "BUY LESS | SELL MORE" if sign_iv * sign_spot > 0 else "BUY MORE | SELL LESS"


def compute_dealer_verdict(
    ticker: str,
    today_session: dict,
    *,
    session_for_trading_date: SessionForTradingDate,
    seed_session_data: SeedSessionData,
    window: float = DEFAULT_WINDOW,
) -> dict:
    """0DTE dealer-hedging verdict (BUY LESS|SELL MORE vs BUY MORE|SELL LESS).

    sign(ΔOTM IV) * sign(spot - prior_close_strike) > 0 → BUY LESS | SELL MORE
    sign(ΔOTM IV) * sign(spot - prior_close_strike) < 0 → BUY MORE  | SELL LESS

    ΔOTM IV is read two ways (Branch A: EOD close, Branch B: session average)
    across the two prior trading sessions; when they disagree, Branch A is
    the tiebreaker, flagged as low confidence. `spot` is read from the first
    snapshot at/after today's 9:00 ET collection start.
    """
    today_date = today_session["trading_date"]
    prev2_date, prev1_date = previous_trading_dates(today_date, 2)

    prev2_points, _ = seed_session_data(ticker, session_for_trading_date(prev2_date), window)
    prev1_points, _ = seed_session_data(ticker, session_for_trading_date(prev1_date), window)
    today_points, _ = seed_session_data(ticker, today_session, window)

    a2, b2 = branch_values(prev2_points)
    a1, b1 = branch_values(prev1_points)
    delta_a = (a1 - a2) if a1 is not None and a2 is not None else None
    delta_b = (b1 - b2) if b1 is not None and b2 is not None else None
    dir_a = classify(delta_a)
    dir_b = classify(delta_b)

    strike_ref = prev1_points[-1]["spot"] if prev1_points else None

    spot_900et = today_points[0]["spot"] if today_points else None
    spot_time = today_points[0]["time"] if today_points else None

    spot_above_strike = None
    if spot_900et is not None and strike_ref is not None:
        spot_above_strike = spot_900et > strike_ref

    branches_agree = dir_a == dir_b and dir_a in ("RISE", "FALL")
    final_direction = dir_a if dir_a in ("RISE", "FALL") else dir_b
    confidence = "HIGH" if branches_agree else ("LOW" if final_direction in ("RISE", "FALL") else "NONE")

    verdict = None
    if final_direction in ("RISE", "FALL") and spot_above_strike is not None:
        verdict = verdict_for_direction(final_direction, spot_above_strike)
    elif spot_900et is None:
        verdict = "PENDING — waiting for today's 9:00 ET snapshot"
    elif strike_ref is None:
        verdict = f"PENDING — no prior-session close found for {prev1_date}"
    else:
        verdict = "NO EDGE — OTM IV flat across both branches"

    return {
        "ticker": ticker.upper(),
        "trading_date": today_date,
        "prev1_date": prev1_date,
        "prev2_date": prev2_date,
        "branch_a": {"prev2": a2, "prev1": a1, "delta": delta_a, "direction": dir_a},
        "branch_b": {"prev2": b2, "prev1": b1, "delta": delta_b, "direction": dir_b},
        "branches_agree": branches_agree,
        "final_direction": final_direction,
        "confidence": confidence,
        "strike_ref": strike_ref,
        "strike_ref_date": prev1_date,
        "spot_900et": spot_900et,
        "spot_900et_time": spot_time,
        "spot_above_strike": spot_above_strike,
        "verdict": verdict,
    }
