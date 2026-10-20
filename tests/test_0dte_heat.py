from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from daily_qqq_snapshot import snapshot_time_to_expiry
from live_server import gex_snapshot_from_chart_rows


def test_0dte_gamma_clock_uses_each_valuation_time() -> None:
    trading_day = date(2026, 10, 19)
    open_t = snapshot_time_to_expiry("2026-10-19", trading_day, datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc))
    afternoon_t = snapshot_time_to_expiry("2026-10-19", trading_day, datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc))
    assert open_t == pytest.approx((0, 390 / (365 * 24 * 60)))
    assert afternoon_t == pytest.approx((0, 60 / (365 * 24 * 60)))


def test_heat_snapshot_uses_valuation_time_and_exposes_source_age() -> None:
    rows = pd.DataFrame([{"strike": 29000.0, "net_gex": 123.0}])
    summary = {
        "expiry": "2026-10-19",
        "effective_snapshot_date": "2026-10-19",
        "source_timestamp": "2026-10-19T13:31:20.123456Z",
    }
    snapshot = gex_snapshot_from_chart_rows("2026-10-19T13:33:00Z", rows, summary)
    assert snapshot["time"] == "2026-10-19T13:33:00Z"
    assert snapshot["expiry"] == "2026-10-19"
    assert snapshot["source_timestamp"] == "2026-10-19T13:31:20.123456Z"
    assert snapshot["rows"] == [{"strike": 29000.0, "net_gex": 123.0}]
