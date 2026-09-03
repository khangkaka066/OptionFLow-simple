from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from exposure import aggregate_by_strike, compute_greeks


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
