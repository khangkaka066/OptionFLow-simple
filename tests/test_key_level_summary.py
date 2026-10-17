from __future__ import annotations

import pytest

from live_dashboard.key_level_summary import compute_key_level_summary
from live_dashboard.snapshot_service import SnapshotService


def _by_strike_row(strike, net_dex=0.0, call_oi=0.0, put_oi=0.0, iv=0.15):
    return {"strike": strike, "net_dex": net_dex, "call_oi": call_oi, "put_oi": put_oi, "iv": iv}


def test_gamma_regime_uses_net_gex_sign_without_assuming_flip_direction():
    by_strike = [_by_strike_row(s) for s in (700, 710, 720, 730, 740)]
    summary = {"spot": 720.0, "net_gex": 1_000_000.0, "gamma_flip": 725.0, "net_dex": 1.0, "net_vex": 1.0}

    # The cumulative flip may cross in either direction, so price alone
    # cannot tell which side has positive aggregate gamma.
    below = compute_key_level_summary(summary, by_strike, live_spot=700.0, basis="eod")
    assert below["gamma_regime"] == "positive"

    # Spot (730) above the flip -> positive regime.
    above = compute_key_level_summary(summary, by_strike, live_spot=730.0, basis="eod")
    assert above["gamma_regime"] == "positive"


def test_gamma_regime_falls_back_to_net_gex_sign_without_a_flip():
    summary = {"spot": 720.0, "net_gex": -5.0, "gamma_flip": None, "net_dex": 1.0, "net_vex": 1.0}
    result = compute_key_level_summary(summary, [], live_spot=720.0, basis="eod")
    assert result["gamma_regime"] == "negative"


def test_vanna_and_dex_regime_still_use_aggregate_sign():
    summary = {"spot": 720.0, "net_gex": 1.0, "gamma_flip": None, "net_dex": -1.0, "net_vex": -1.0}
    result = compute_key_level_summary(summary, [], live_spot=720.0, basis="eod")
    assert result["dex_regime"] == "negative"
    assert result["vanna_regime"] == "negative"


def test_key_level_ranks_high_confidence_candidate_over_far_low_confidence_ones():
    # Per-strike net_dex changes sign between 715 and 720, where OI is high.
    by_strike = [
        _by_strike_row(700, net_dex=-10.0, call_oi=100, put_oi=100),
        _by_strike_row(715, net_dex=-5.0, call_oi=100, put_oi=100),
        _by_strike_row(720, net_dex=20.0, call_oi=500, put_oi=500),
        _by_strike_row(730, net_dex=4.0, call_oi=100, put_oi=100),
    ]
    # GEX-wall fields point far away, at low-OI strikes -> low confidence,
    # so the nearby high-confidence DEX flip should still win the top spot,
    # even though GEX walls are back in the candidate pool.
    summary = {
        "spot": 718.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
        "gamma_flip": None,
        "call_resistance": 900.0, "put_support": 100.0, "gamma_wall_abs": 950.0,
    }
    result = compute_key_level_summary(summary, by_strike, live_spot=718.0, basis="eod")
    key_level = result["key_level"]
    assert key_level is not None
    assert key_level["field"] == "delta_sign_flip"
    assert 715.0 < key_level["price"] < 720.0
    assert key_level["confidence"] == "high"


def test_key_level_prefers_nearby_gex_wall_over_far_dex_flip():
    # Mirrors the real scenario that prompted this: a nearby GEX wall with
    # high OI should outrank a DEX flip that's far from spot with low OI.
    by_strike = [
        _by_strike_row(736, net_dex=1.0, call_oi=200, put_oi=6000, iv=0.16),
        _by_strike_row(740, net_dex=2.0, call_oi=3000, put_oi=3000, iv=0.18),
        _by_strike_row(745, net_dex=3.0, call_oi=4000, put_oi=200, iv=0.19),
        _by_strike_row(694, net_dex=-4000.0, call_oi=50, put_oi=50, iv=0.30),
    ]
    summary = {
        "spot": 741.1, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0, "gamma_flip": None,
        "call_resistance": 745.0, "put_support": 736.0, "gamma_wall_abs": 736.0,
    }
    result = compute_key_level_summary(summary, by_strike, live_spot=741.1, basis="eod")
    key_levels = result["key_levels"]
    assert len(key_levels) >= 2
    top = key_levels[0]
    assert top["price"] in (736.0, 745.0, 740.0)
    assert all(abs(lvl["price"] - 741.1) < 20 for lvl in key_levels[:2])


def test_key_level_dedupes_coincident_strikes_across_sources():
    # Gamma Wall and the top-OI pick both resolve to strike 736 -> one merged
    # entry with a combined label, not two separate ones.
    by_strike = [
        _by_strike_row(730, net_dex=1.0, call_oi=100, put_oi=100),
        _by_strike_row(736, net_dex=1.0, call_oi=200, put_oi=8000, iv=0.16),
        _by_strike_row(745, net_dex=1.0, call_oi=300, put_oi=300),
    ]
    summary = {
        "spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0, "gamma_flip": None,
        "gamma_wall_abs": 736.0,
    }
    result = compute_key_level_summary(summary, by_strike, live_spot=740.0, basis="eod")
    key_levels = result["key_levels"]
    prices_at_736 = [lvl for lvl in key_levels if lvl["price"] == 736.0]
    assert len(prices_at_736) == 1
    assert "Gamma Wall" in prices_at_736[0]["label"]
    assert "High OI" in prices_at_736[0]["label"]


def test_key_level_none_when_no_dex_flip_exists():
    by_strike = [_by_strike_row(700, net_dex=5.0), _by_strike_row(710, net_dex=6.0)]
    summary = {"spot": 705.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0, "gamma_flip": None}
    result = compute_key_level_summary(summary, by_strike, live_spot=705.0, basis="eod")
    assert result["key_level"] is None


def test_trigger_uses_per_strike_dex_sign_change_not_noted_flips():
    by_strike = [
        _by_strike_row(739, net_dex=20, call_oi=100, iv=0.2),
        _by_strike_row(740, net_dex=10, call_oi=100, iv=0.2),
        _by_strike_row(741, net_dex=-10, call_oi=1000, iv=0.2),
    ]
    summary = {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
               "gamma_flip": 790.0, "delta_flip": 500.0}
    result = compute_key_level_summary(summary, by_strike, live_spot=740.0, basis="eod")
    trigger = result["trigger_level"]
    assert trigger is not None
    assert trigger["field"] == "delta_sign_flip"
    assert trigger["price"] == pytest.approx(740.5)
    assert trigger["from_sign"] == "positive"
    assert trigger["to_sign"] == "negative"
    assert trigger["evidence_status"] == "supported"
    assert trigger["iv_relative_pct"] == pytest.approx(0.0)
    assert trigger["iv_context"] == "near_atm"


def test_trigger_reports_iv_relative_to_atm():
    rows = [
        _by_strike_row(739, net_dex=10, call_oi=100, iv=0.2),
        _by_strike_row(740, net_dex=10, call_oi=100, iv=0.2),
        _by_strike_row(741, net_dex=-10, call_oi=1000, iv=0.3),
    ]
    summary = {"spot": 740.0, "net_gex": -1.0, "net_dex": 1.0, "net_vex": 1.0}
    result = compute_key_level_summary(summary, rows, live_spot=740.0, basis="eod")
    trigger = result["trigger_level"]
    assert trigger["evidence_status"] == "supported"
    assert trigger["iv"] == pytest.approx(0.3)
    assert trigger["atm_iv"] == pytest.approx(0.2)
    assert trigger["iv_relative_pct"] == pytest.approx(50.0)
    assert trigger["iv_context"] == "elevated"


def test_trigger_prefers_nearby_flip_with_oi_and_iv_over_closer_weak_flip():
    rows = [
        _by_strike_row(738, net_dex=10, call_oi=100),
        _by_strike_row(739, net_dex=10, call_oi=100),
        _by_strike_row(740, net_dex=-10, call_oi=100),
        _by_strike_row(741, net_dex=-10, call_oi=100),
        _by_strike_row(742, net_dex=10, call_oi=1000),
        _by_strike_row(743, net_dex=10, call_oi=100),
    ]
    summary = {"spot": 739.9, "net_gex": -1.0, "net_dex": 1.0, "net_vex": 1.0}
    result = compute_key_level_summary(summary, rows, live_spot=739.9, basis="eod")
    trigger = result["trigger_level"]
    assert trigger["price"] == pytest.approx(741.5)
    assert trigger["from_sign"] == "negative"
    assert trigger["to_sign"] == "positive"
    assert trigger["oi_strike"] == 742.0
    assert trigger["evidence_status"] == "supported"


def test_tiny_dex_sign_change_does_not_outrank_meaningful_flip():
    rows = [
        _by_strike_row(738, net_dex=10, call_oi=100),
        _by_strike_row(739, net_dex=10, call_oi=100),
        _by_strike_row(740, net_dex=-0.1, call_oi=1000),
        _by_strike_row(741, net_dex=-10, call_oi=100),
        _by_strike_row(742, net_dex=-10, call_oi=100),
        _by_strike_row(743, net_dex=10, call_oi=1000),
        _by_strike_row(744, net_dex=10, call_oi=100),
    ]
    summary = {"spot": 739.9, "net_gex": -1.0, "net_dex": 1.0, "net_vex": 1.0}
    result = compute_key_level_summary(summary, rows, live_spot=739.9, basis="eod")
    assert result["trigger_level"]["price"] == pytest.approx(742.5)


def test_trigger_with_missing_iv_is_visible_but_not_used_for_plan():
    rows = [
        _by_strike_row(739, net_dex=-10, call_oi=100, iv=0.2),
        _by_strike_row(740, net_dex=-10, call_oi=100, iv=0.2),
        _by_strike_row(741, net_dex=10, call_oi=1000, iv=None),
    ]
    summary = {"spot": 740.0, "net_gex": -1.0, "net_dex": 1.0, "net_vex": 1.0,
               "put_support": 739.0}
    result = compute_key_level_summary(summary, rows, live_spot=740.0, basis="eod")
    assert result["trigger_level"]["evidence_status"] == "missing_iv"
    assert result["plan"]["status"] == "insufficient"


def test_zero_dex_strike_between_opposite_signs_is_a_flip():
    rows = [
        _by_strike_row(740, net_dex=10, call_oi=100),
        _by_strike_row(741, net_dex=0, call_oi=100),
        _by_strike_row(742, net_dex=-10, call_oi=1000),
    ]
    summary = {"spot": 741.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0}
    result = compute_key_level_summary(summary, rows, live_spot=741.0, basis="eod")
    assert result["trigger_level"]["price"] == pytest.approx(741.0)


def test_pending_status_without_summary():
    assert compute_key_level_summary(None, [], live_spot=None, basis="eod") == {"status": "pending"}


def test_no_live_spot_only_returns_reference_plan():
    rows = [_by_strike_row(s, call_oi=1000 + s) for s in (735, 740, 745)]
    summary = {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
               "put_support": 735.0, "call_resistance": 745.0}
    result = compute_key_level_summary(summary, rows, live_spot=None, basis="eod")
    assert result["plan"]["status"] == "reference"
    assert result["confidence_label"] == "Chỉ là mốc tham chiếu"
    assert "size" not in result["confidence_label"].lower()


def test_far_wall_cannot_borrow_oi_or_merge_with_boundary_strike():
    rows = [_by_strike_row(735, call_oi=100), _by_strike_row(740, call_oi=500),
            _by_strike_row(745, call_oi=1000)]
    summary = {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
               "call_resistance": 900.0, "put_support": 735.0}
    result = compute_key_level_summary(summary, rows, live_spot=740.0, basis="eod")
    assert all(level["price"] != 900.0 for level in result["key_levels"])
    assert any(level["price"] == 735.0 for level in result["key_levels"])


def test_range_plan_requires_live_spot_inside_valid_boundaries():
    rows = [_by_strike_row(735, net_dex=-10, call_oi=100),
            _by_strike_row(740, net_dex=-10, call_oi=100),
            _by_strike_row(745, net_dex=20, call_oi=1000)]
    summary = {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
               "put_support": 735.0, "call_resistance": 745.0}
    result = compute_key_level_summary(summary, rows, live_spot=740.0, basis="eod")
    assert result["plan"]["setup"] == "Range có điều kiện"
    assert "735.00" in result["plan"]["condition"]
    assert "745.00" in result["plan"]["condition"]
    assert "Delta Flip" in result["plan"]["condition"]
    outside = compute_key_level_summary(summary, rows, live_spot=746.0, basis="intraday")
    assert outside["plan"]["setup"] != "Range có điều kiện"


def test_stale_eod_plan_requires_intraday_refresh():
    rows = [_by_strike_row(s, call_oi=1000 + s) for s in (735, 740, 745)]
    summary = {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
               "put_support": 735.0, "call_resistance": 745.0}
    result = compute_key_level_summary(summary, rows, live_spot=750.0, basis="eod")
    assert result["plan"]["status"] == "stale"


def test_high_oi_without_a_structural_level_is_not_a_plan():
    rows = [_by_strike_row(735, call_oi=100), _by_strike_row(740, call_oi=100),
            _by_strike_row(745, call_oi=1000)]
    summary = {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0}
    result = compute_key_level_summary(summary, rows, live_spot=740.0, basis="eod")
    assert result["key_levels"][0]["field"] == "high_oi"
    assert result["plan"]["status"] == "insufficient"


def test_historical_snapshot_switches_basis_at_same_distance_as_live_panel():
    service = object.__new__(SnapshotService)
    service._filter_replay_series = lambda points, ribbon, session, snapshot: (points, ribbon)
    service._session_from_summary = lambda summary: {
        "trading_date": "2026-09-25",
        "market_open_utc": "2026-09-25T13:30:00+00:00",
    }
    service._candles_for_session = lambda ticker, session: ([], None)
    service._skew_tenors_payload_for_summary = lambda ticker, summary: []
    service._seed_prev_day_eod_summary = lambda ticker, day, window: (
        {"spot": 740.0, "net_gex": 1.0, "net_dex": 1.0, "net_vex": 1.0,
         "put_support": 735.0, "call_resistance": 745.0},
        [_by_strike_row(s, call_oi=1000 + s) for s in (735, 740, 745)],
    )
    service._key_level_window = 150.0
    service._vn_tz = "UTC"
    rows = [_by_strike_row(740, net_dex=-10, call_oi=100),
            _by_strike_row(745, net_dex=-10, call_oi=100),
            _by_strike_row(750, net_dex=20, call_oi=1000)]

    def payload(spot, snapshot_utc="2026-09-25T20:00:00+00:00"):
        summary = {"spot": spot, "net_gex": -1.0, "net_dex": 1.0, "net_vex": 1.0,
                   "gamma_flip": 745.0, "put_support": 745.0, "effective_snapshot_date": "2026-09-25",
                   "snapshot_utc": snapshot_utc}
        return service._state_payload("history:test", "QQQ", summary, {}, rows, [], {}, [{}], [{}], [])

    assert payload(741.0, "2026-09-25T13:14:59+00:00")["key_level_summary"] == {"status": "pending"}
    assert payload(741.0, "2026-09-25T13:15:00+00:00")["key_level_summary"]["basis"] == "eod"
    assert payload(741.0)["key_level_summary"]["basis"] == "eod"
    moved = payload(750.0)["key_level_summary"]
    assert moved["basis"] == "intraday"
    assert moved["plan"]["status"] == "watch"
