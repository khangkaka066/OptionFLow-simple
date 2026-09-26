"""Delta-bucket skew statistics (25Δ/10Δ skew, butterfly, skew slope, term
slope) for the live Volatility Skew panel.

InsiderFinance does not publish the exact formulas behind its Volatility Skew
widget, so these follow the standard industry convention (risk reversal /
butterfly on fixed delta buckets) rather than trying to reverse-engineer an
exact numeric match. Inputs are the per-tenor `call`/`put` IV curves already
built by `render_gex_interactive.build_tenor_curves` (columns: strike, iv;
`iv` as a fraction, e.g. 0.115 not 11.5).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bsm import bs_delta

# Mirrors live_dashboard.greek_surface_service.RAW_CHAIN_RATE. Duplicated
# (rather than imported) to avoid a live_dashboard -> scripts import cycle;
# both values must be kept in sync if the risk-free rate assumption changes.
RATE = 0.04


def attach_delta(curve: pd.DataFrame, spot: float, years: float, option_type: str) -> pd.DataFrame:
    """Add a `delta` column to a strike/iv curve via Black-Scholes.

    InsiderFinance's own per-contract delta is discarded upstream (see
    scripts/sources/insiderfinance.py), so delta is recomputed here directly
    from each strike's IV rather than threaded through the whole pipeline.
    """
    out = curve.copy()
    if out.empty or "strike" not in out or "iv" not in out:
        out["delta"] = pd.Series(dtype=float)
        return out
    out["delta"] = [
        bs_delta(float(spot), float(strike), float(years), RATE, float(iv), option_type)
        if np.isfinite(strike) and np.isfinite(iv)
        else float("nan")
        for strike, iv in zip(out["strike"], out["iv"])
    ]
    return out


def interp_at_delta(
    strikes: np.ndarray, deltas: np.ndarray, ivs: np.ndarray, target_abs_delta: float
) -> tuple[float, float] | None:
    """Strike & IV at |delta| == target_abs_delta, by interpolation only.

    `deltas` must be the signed BSM delta aligned with `strikes`/`ivs` (calls
    in [0, 1], puts in [-1, 0]). Returns None when the target isn't bracketed
    by the observed data — extrapolating a 25Δ/10Δ point past the most liquid
    strikes would fabricate a number, not measure one.
    """
    order = np.argsort(strikes)
    strikes = np.asarray(strikes)[order]
    deltas = np.asarray(deltas)[order]
    ivs = np.asarray(ivs)[order]
    valid = np.isfinite(strikes) & np.isfinite(deltas) & np.isfinite(ivs)
    strikes, deltas, ivs = strikes[valid], deltas[valid], ivs[valid]
    if len(strikes) < 2:
        return None
    abs_deltas = np.abs(deltas)
    # abs(delta) is monotonic in strike within a single side (call or put),
    # decreasing away from ATM; np.interp needs its xp strictly increasing.
    if abs_deltas[0] > abs_deltas[-1]:
        abs_deltas = abs_deltas[::-1]
        strikes = strikes[::-1]
        ivs = ivs[::-1]
    if target_abs_delta < abs_deltas[0] or target_abs_delta > abs_deltas[-1]:
        return None
    strike = float(np.interp(target_abs_delta, abs_deltas, strikes))
    iv = float(np.interp(target_abs_delta, abs_deltas, ivs))
    return strike, iv


def _point_or_none(point: tuple[float, float] | None) -> dict | None:
    if point is None:
        return None
    strike, iv = point
    return {"strike": strike, "iv": iv}


def compute_tenor_skew_stats(
    call_curve: pd.DataFrame,
    put_curve: pd.DataFrame,
    atm_strike: float,
    atm_iv: float,
    spot: float,
    years: float,
) -> dict:
    """25Δ/10Δ skew, 25Δ butterfly, and a local skew slope for one tenor.

    `call_curve`/`put_curve` are the OTM-side strike/iv curves already built
    by build_tenor_curves (calls: strike > atm_strike; puts: strike <
    atm_strike, each curve including the shared ATM point).
    """
    result: dict = {
        "call_25d": None, "put_25d": None, "call_10d": None, "put_10d": None,
        "skew_25d": None, "butterfly_25d": None, "skew_10d": None, "skew_slope": None,
    }
    if not np.isfinite(spot) or spot <= 0 or not np.isfinite(years) or years <= 0:
        return result

    call_d = attach_delta(call_curve, spot, years, "call")
    put_d = attach_delta(put_curve, spot, years, "put")

    call_25 = interp_at_delta(call_d["strike"].to_numpy(), call_d["delta"].to_numpy(), call_d["iv"].to_numpy(), 0.25)
    put_25 = interp_at_delta(put_d["strike"].to_numpy(), put_d["delta"].to_numpy(), put_d["iv"].to_numpy(), 0.25)
    call_10 = interp_at_delta(call_d["strike"].to_numpy(), call_d["delta"].to_numpy(), call_d["iv"].to_numpy(), 0.10)
    put_10 = interp_at_delta(put_d["strike"].to_numpy(), put_d["delta"].to_numpy(), put_d["iv"].to_numpy(), 0.10)

    result["call_25d"] = _point_or_none(call_25)
    result["put_25d"] = _point_or_none(put_25)
    result["call_10d"] = _point_or_none(call_10)
    result["put_10d"] = _point_or_none(put_10)

    if call_25 is not None and put_25 is not None:
        result["skew_25d"] = (put_25[1] - call_25[1]) * 100.0
        if np.isfinite(atm_iv):
            result["butterfly_25d"] = ((put_25[1] + call_25[1]) / 2.0 - atm_iv) * 100.0
    if call_10 is not None and put_10 is not None:
        result["skew_10d"] = (put_10[1] - call_10[1]) * 100.0

    combined = pd.concat([put_curve, call_curve], ignore_index=True).dropna(subset=["strike", "iv"])
    if len(combined) >= 2 and np.isfinite(spot) and spot > 0:
        pct_otm = (combined["strike"].to_numpy(dtype=float) / float(spot) - 1.0) * 100.0
        iv_pts = combined["iv"].to_numpy(dtype=float) * 100.0
        if len(set(pct_otm)) >= 2:
            slope, _ = np.polyfit(pct_otm, iv_pts, 1)
            result["skew_slope"] = float(slope)

    return result


def attach_term_slope(tenor_payloads: list[dict]) -> None:
    """Mutate `tenor_payloads` in place, adding `term_slope`/`term_slope_label`.

    term_slope = next-dated tenor's ATM IV minus this tenor's ATM IV, in IV
    points; compared against the next tenor by DTE, not calendar order of the
    input list (multi-tenor payloads aren't guaranteed pre-sorted).
    """
    ordered = sorted(
        (t for t in tenor_payloads if isinstance(t.get("dte"), (int, float))),
        key=lambda t: t["dte"],
    )
    for i, tenor in enumerate(ordered):
        this_iv = tenor.get("atm_iv")
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            next_iv = nxt.get("atm_iv")
            if isinstance(this_iv, (int, float)) and isinstance(next_iv, (int, float)) and np.isfinite(this_iv) and np.isfinite(next_iv):
                tenor["term_slope"] = (next_iv - this_iv) * 100.0
                tenor["term_slope_label"] = f"vs {nxt.get('expiry')}"
                continue
        tenor["term_slope"] = None
        tenor["term_slope_label"] = "Term flat"
    for tenor in tenor_payloads:
        tenor.setdefault("term_slope", None)
        tenor.setdefault("term_slope_label", "Term flat")
