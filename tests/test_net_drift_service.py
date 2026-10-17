from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live_dashboard import net_drift_service


def test_public_flow_pages_all_rows_and_normalizes_trades(monkeypatch):
    calls = []
    timestamp = int(datetime(2026, 10, 1, 14, 35, tzinfo=timezone.utc).timestamp() * 1e9)
    monkeypatch.setattr(net_drift_service, "PAGE_SIZE", 2)

    def trade(identifier):
        return {
            "id": identifier, "cp": "Call", "date": "2026-10-01",
            "exchangeTimestamp": timestamp + identifier, "expireDay": 1, "expireMonth": 10,
            "expireYear": 2026, "premium": 12000, "price": 1.2,
            "size": 100, "spot": 600, "strike": 605, "tradeSide": "ASK",
        }

    def fake_graphql(query, variables):
        calls.append(variables)
        if "aggregate" in query:
            return {"free_ticker_option_flow_aggregate": {"aggregate": {"count": 3}}}
        return {"free_ticker_option_flow": [trade(i) for i in ([3, 2] if variables["offset"] == 0 else [1])]}

    monkeypatch.setattr(net_drift_service, "_graphql", fake_graphql)
    result = net_drift_service.NetDriftService().load("qqq", "2026-10-01")

    assert result["ticker"] == "QQQ"
    assert result["returned_count"] == 3
    assert result["session_count"] == 3
    assert result["partial"] is False
    assert result["trade_sides"] == ["ASK"]
    assert result["trades"][0]["expiry"] == "2026-10-01"
    assert result["trades"][0]["time_ny"].startswith("2026-10-01T10:35")
    assert {call["offset"] for call in calls if "offset" in call} == {0, 2}


@pytest.mark.parametrize("ticker,day", [("QQQ!", "2026-10-01"), ("QQQ", "01/10/2026")])
def test_public_flow_rejects_invalid_input_without_request(ticker, day, monkeypatch):
    monkeypatch.setattr(net_drift_service, "_graphql", lambda *_: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        net_drift_service.NetDriftService().load(ticker, day)
