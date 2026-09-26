from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from skew_stats import attach_delta, attach_term_slope, compute_tenor_skew_stats, interp_at_delta


def test_interp_at_delta_interpolates_between_bracketing_points():
    strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
    deltas = np.array([0.85, 0.65, 0.50, 0.35, 0.15])
    ivs = np.array([0.30, 0.25, 0.20, 0.22, 0.28])
    point = interp_at_delta(strikes, deltas, ivs, 0.25)
    assert point is not None
    strike, iv = point
    assert 105.0 < strike < 110.0
    assert 0.22 < iv < 0.28


def test_interp_at_delta_returns_none_outside_observed_range():
    strikes = np.array([95.0, 100.0, 105.0])
    deltas = np.array([0.65, 0.50, 0.35])
    ivs = np.array([0.25, 0.20, 0.22])
    assert interp_at_delta(strikes, deltas, ivs, 0.10) is None


def test_interp_at_delta_handles_put_side_negative_deltas():
    strikes = np.array([90.0, 95.0, 100.0])
    deltas = np.array([-0.10, -0.30, -0.50])
    ivs = np.array([0.28, 0.24, 0.20])
    point = interp_at_delta(strikes, deltas, ivs, 0.25)
    assert point is not None
    strike, iv = point
    assert 90.0 < strike < 95.0


def test_attach_delta_calls_are_positive_and_decreasing_in_strike():
    curve = pd.DataFrame({"strike": [95.0, 100.0, 105.0], "iv": [0.22, 0.20, 0.19]})
    out = attach_delta(curve, spot=100.0, years=0.1, option_type="call")
    assert (out["delta"] > 0).all()
    assert out["delta"].iloc[0] > out["delta"].iloc[1] > out["delta"].iloc[2]


def _symmetric_smile(spot: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A put-skewed smile: puts richer than calls at matching distance from ATM."""
    put_strikes = np.array([spot - 15, spot - 10, spot - 5, spot])
    call_strikes = np.array([spot, spot + 5, spot + 10, spot + 15])
    put_iv = np.array([0.28, 0.24, 0.21, 0.19])
    call_iv = np.array([0.19, 0.17, 0.155, 0.14])
    put_curve = pd.DataFrame({"strike": put_strikes, "iv": put_iv})
    call_curve = pd.DataFrame({"strike": call_strikes, "iv": call_iv})
    return call_curve, put_curve


def test_compute_tenor_skew_stats_put_skew_is_positive():
    spot = 100.0
    call_curve, put_curve = _symmetric_smile(spot)
    stats = compute_tenor_skew_stats(call_curve, put_curve, atm_strike=spot, atm_iv=0.19, spot=spot, years=0.05)
    assert stats["skew_25d"] is not None
    assert stats["skew_25d"] > 0  # puts richer than calls -> positive skew
    assert stats["skew_slope"] is not None
    assert stats["skew_slope"] < 0  # IV falls as strike rises through this smile


def test_compute_tenor_skew_stats_handles_missing_data_gracefully():
    empty = pd.DataFrame({"strike": [], "iv": []})
    stats = compute_tenor_skew_stats(empty, empty, atm_strike=100.0, atm_iv=0.2, spot=100.0, years=0.05)
    assert stats["skew_25d"] is None
    assert stats["call_25d"] is None
    assert stats["skew_slope"] is None


def test_attach_term_slope_orders_by_dte_and_labels_next_expiry():
    tenors = [
        {"expiry": "2026-09-30", "dte": 4, "atm_iv": 0.13},
        {"expiry": "2026-09-26", "dte": 0, "atm_iv": 0.11},
        {"expiry": "2026-09-28", "dte": 2, "atm_iv": 0.115},
    ]
    attach_term_slope(tenors)
    by_expiry = {t["expiry"]: t for t in tenors}
    assert by_expiry["2026-09-26"]["term_slope"] == pytest.approx((0.115 - 0.11) * 100.0)
    assert by_expiry["2026-09-26"]["term_slope_label"] == "vs 2026-09-28"
    assert by_expiry["2026-09-30"]["term_slope"] is None
    assert by_expiry["2026-09-30"]["term_slope_label"] == "Term flat"
