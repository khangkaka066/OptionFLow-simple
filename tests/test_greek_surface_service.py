from __future__ import annotations

import pandas as pd

from live_dashboard.greek_surface_service import GreekSurfaceService


class DummyStore:
    pass


def test_build_surface_normalizes_expiry_by_strike_matrix():
    service = GreekSurfaceService(DummyStore(), max_strikes=10)
    rows = pd.DataFrame([
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 700, "net_gex": 10.0, "call_gex": 15.0, "put_gex": -5.0},
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 705, "net_gex": -20.0, "call_gex": 0.0, "put_gex": -20.0},
        {"ticker": "QQQ", "expiry": "2026-09-11", "strike": 700, "net_gex": 5.0, "call_gex": 6.0, "put_gex": -1.0},
        {"ticker": "QQQ", "expiry": "2026-09-11", "strike": 705, "net_gex": 0.0, "call_gex": 0.0, "put_gex": 0.0},
    ])
    summary = {"spot": 702.0, "snapshot_utc": "2026-09-04T20:15:00+00:00", "call_resistance": 705}

    payload = service._build_surface(
        "QQQ", "2026-09-04", "gex", "net", "net_gex", summary, rows, strike_range=None, dte_max=None
    )

    assert payload["strikes"] == [700.0, 705.0]
    assert payload["expiries"] == ["2026-09-04", "2026-09-11"]
    assert payload["rawMaxAbs"] == 20.0
    assert payload["values"] == [[0.5, -1.0], [0.25, 0.0]]
    assert payload["levels"]["call_resistance"] == 705


def test_build_surface_filters_strike_range():
    service = GreekSurfaceService(DummyStore(), max_strikes=10)
    rows = pd.DataFrame([
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 690, "net_dex": 100.0},
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 700, "net_dex": 200.0},
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 710, "net_dex": 300.0},
    ])

    payload = service._build_surface(
        "QQQ", "2026-09-04", "dex", "net", "net_dex", {"spot": 700}, rows, strike_range=5, dte_max=None
    )

    assert payload["strikes"] == [700.0]
    assert payload["raw_values"] == [[200.0]]


def test_build_surface_supports_raw_greek_metric():
    """The 'Greek Surface' panel (Delta/Gamma/Theta/Vega) reuses the exact same
    pivot/normalize pipeline as the dollar-exposure surfaces — it's just a
    different metric column ('avg_gamma' etc, mode='raw')."""
    service = GreekSurfaceService(DummyStore(), max_strikes=10)
    rows = pd.DataFrame([
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 700, "avg_gamma": 0.02},
        {"ticker": "QQQ", "expiry": "2026-09-04", "strike": 705, "avg_gamma": 0.05},
        {"ticker": "QQQ", "expiry": "2026-09-11", "strike": 700, "avg_gamma": 0.01},
        {"ticker": "QQQ", "expiry": "2026-09-11", "strike": 705, "avg_gamma": 0.04},
    ])
    summary = {"spot": 702.0, "snapshot_utc": "2026-09-04T20:15:00+00:00"}

    payload = service._build_surface(
        "QQQ", "2026-09-04", "gamma", "raw", "avg_gamma", summary, rows, strike_range=None, dte_max=None
    )

    assert payload["greek"] == "gamma"
    assert payload["mode"] == "raw"
    assert payload["metric"] == "avg_gamma"
    assert payload["strikes"] == [700.0, 705.0]
    assert payload["raw_values"] == [[0.02, 0.05], [0.01, 0.04]]
    assert payload["rawMaxAbs"] == 0.05
