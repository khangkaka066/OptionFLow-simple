from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from exposure import aggregate_by_strike, aggregate_greeks_by_expiry_strike, compute_expected_move, compute_greeks, find_expected_move_anchor


def option_chain() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "expiry": "2026-09-18",
                "strike": 100.0,
                "option_type": "call",
                "impliedVolatility": 0.20,
                "openInterest": 10.0,
                "volume": 5.0,
                "bid": 0.0,
                "ask": 0.0,
            },
            {
                "expiry": "2026-09-18",
                "strike": 100.0,
                "option_type": "put",
                "impliedVolatility": 0.20,
                "openInterest": 10.0,
                "volume": 7.0,
                "bid": 0.0,
                "ask": 0.0,
            },
        ]
    )


def test_exposure_sign_conventions_for_matched_call_put() -> None:
    greeks = compute_greeks(
        option_chain(),
        spot=100.0,
        years_by_expiry={"2026-09-18": 30 / 365},
        rate=0.0,
    )
    call = greeks[greeks["option_type"] == "call"].iloc[0]
    put = greeks[greeks["option_type"] == "put"].iloc[0]

    assert call["gex"] > 0
    assert put["gex"] < 0
    assert call["gex"] == pytest.approx(-put["gex"], rel=1e-12)

    assert call["dex"] > 0
    assert put["dex"] < 0

    assert np.sign(call["vex"]) == -np.sign(put["vex"])
    assert call["vex"] == pytest.approx(-put["vex"], rel=1e-12)

    assert np.sign(call["chex"]) == -np.sign(put["chex"])
    assert call["chex"] == pytest.approx(-put["chex"], rel=1e-12)


def test_aggregate_by_strike_preserves_net_columns() -> None:
    greeks = compute_greeks(
        option_chain(),
        spot=100.0,
        years_by_expiry={"2026-09-18": 30 / 365},
        rate=0.0,
    )
    by_strike = aggregate_by_strike(greeks)
    row = by_strike.iloc[0]

    assert row["strike"] == 100.0
    assert row["call_oi"] == 10.0
    assert row["put_oi"] == 10.0
    assert row["call_volume"] == 5.0
    assert row["put_volume"] == 7.0
    assert row["net_gex"] == pytest.approx(row["call_gex"] + row["put_gex"])
    assert row["net_dex"] == pytest.approx(row["call_dex"] + row["put_dex"])
    assert row["net_vex"] == pytest.approx(row["call_vex"] + row["put_vex"])
    assert row["net_chex"] == pytest.approx(row["call_chex"] + row["put_chex"])


def test_aggregate_greeks_by_expiry_strike_oi_weighted_mean() -> None:
    chain = pd.DataFrame(
        [
            # expiry/strike cell with nonzero OI on both legs -> OI-weighted mean
            {"expiry": "2026-09-18", "strike": 100.0, "option_type": "call", "openInterest": 30.0, "gamma": 0.02, "delta": 0.6, "theta": -0.05, "vega": 0.10},
            {"expiry": "2026-09-18", "strike": 100.0, "option_type": "put", "openInterest": 10.0, "gamma": 0.02, "delta": -0.4, "theta": -0.04, "vega": 0.10},
            # expiry/strike cell with zero OI on both legs -> falls back to unweighted mean
            {"expiry": "2026-09-18", "strike": 105.0, "option_type": "call", "openInterest": 0.0, "gamma": 0.01, "delta": 0.3, "theta": -0.02, "vega": 0.05},
            {"expiry": "2026-09-18", "strike": 105.0, "option_type": "put", "openInterest": 0.0, "gamma": 0.01, "delta": -0.7, "theta": -0.03, "vega": 0.05},
        ]
    )
    result = aggregate_greeks_by_expiry_strike(chain)
    weighted_row = result[result["strike"] == 100.0].iloc[0]
    fallback_row = result[result["strike"] == 105.0].iloc[0]

    assert weighted_row["total_oi"] == 40.0
    assert weighted_row["avg_gamma"] == pytest.approx(0.02)
    # delta: (0.6*30 + -0.4*10) / 40 = 0.35
    assert weighted_row["avg_delta"] == pytest.approx(0.35)
    assert weighted_row["avg_theta"] == pytest.approx((-0.05 * 30 + -0.04 * 10) / 40)
    assert weighted_row["avg_vega"] == pytest.approx(0.10)

    assert fallback_row["total_oi"] == 0.0
    # zero OI on both legs -> plain (unweighted) mean, not a division-by-zero NaN
    assert fallback_row["avg_delta"] == pytest.approx((0.3 + -0.7) / 2)
    assert fallback_row["avg_gamma"] == pytest.approx(0.01)


def test_compute_expected_move_one_std_dev() -> None:
    # spot=100, ATM IV=20%, 1 trading day to expiry (~1/365 years)
    move = compute_expected_move(100.0, 20.0, 1 / 365)
    assert move == pytest.approx(100.0 * 0.20 * np.sqrt(1 / 365))
    assert move == pytest.approx(1.0468, abs=1e-3)


def test_compute_expected_move_missing_inputs_returns_none() -> None:
    assert compute_expected_move(None, 20.0, 1 / 365) is None
    assert compute_expected_move(100.0, None, 1 / 365) is None
    assert compute_expected_move(100.0, 20.0, None) is None


def test_find_expected_move_anchor_returns_none_for_empty_or_before_open() -> None:
    market_open = "2026-09-01T13:30:00+00:00"
    assert find_expected_move_anchor([], market_open, "QQQ") is None
    points = [
        {"time": "2026-09-01T13:29:59+00:00", "spot": 100.0, "atm_iv": 20.0, "expiry": "2026-09-01"}
    ]
    assert find_expected_move_anchor(points, market_open, "QQQ") is None


def test_find_expected_move_anchor_uses_first_qualifying_point() -> None:
    market_open = "2026-09-01T13:30:00+00:00"
    points = [
        {"time": "2026-09-01T13:31:00+00:00", "spot": 102.0, "atm_iv": 30.0, "expiry": "2026-09-01"},
        {"time": "2026-09-01T13:30:00+00:00", "spot": 100.0, "atm_iv": 20.0, "expiry": "2026-09-01"},
    ]
    anchor = find_expected_move_anchor(points, market_open, "QQQ")
    assert anchor is not None
    assert anchor["ticker"] == "QQQ"
    assert anchor["captured_at"].startswith("2026-09-01T13:30:00")
    assert anchor["spot"] == 100.0
    assert anchor["atm_iv"] == 20.0
    assert anchor["years_to_expiry"] == pytest.approx((6.5 * 60) / (365 * 24 * 60))
    assert anchor["upper2"] == pytest.approx(100.0 + anchor["move_abs"] * 2)
    assert anchor["lower2"] == pytest.approx(100.0 - anchor["move_abs"] * 2)
    assert anchor["upper"] == pytest.approx(100.0 + anchor["move_abs"])
    assert anchor["lower"] == pytest.approx(100.0 - anchor["move_abs"])


def test_find_expected_move_anchor_uses_snapshot_time_for_0dte() -> None:
    market_open = "2026-09-01T13:30:00+00:00"
    early = find_expected_move_anchor(
        [{"time": "2026-09-01T13:30:00+00:00", "spot": 100.0, "atm_iv": 20.0, "expiry": "2026-09-01"}],
        market_open,
        "QQQ",
    )
    late = find_expected_move_anchor(
        [{"time": "2026-09-01T19:30:00+00:00", "spot": 100.0, "atm_iv": 20.0, "expiry": "2026-09-01"}],
        market_open,
        "QQQ",
    )

    assert early is not None
    assert late is not None
    assert early["years_to_expiry"] == pytest.approx((6.5 * 60) / (365 * 24 * 60))
    assert late["years_to_expiry"] == pytest.approx((30 / (365 * 24 * 60)))
    assert early["move_abs"] > late["move_abs"]
