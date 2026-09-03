from live_dashboard import market_data


def test_apply_futures_basis_mutates_summary_when_basis_available(monkeypatch):
    monkeypatch.setattr(market_data, "fetch_futures_basis", lambda ticker, spot, snapshot_utc=None: 12.5)
    summary = {"spot": 100.0, "snapshot_utc": "2026-09-01T20:15:00Z"}

    result = market_data.apply_futures_basis(summary, "NQ1!")

    assert result is summary
    assert summary["futures_basis"] == 12.5
    assert summary["futures_ticker"] == "NQ1!"
    assert summary["futures_tick_size"] == 0.25


def test_apply_futures_basis_leaves_summary_unchanged_without_ticker(monkeypatch):
    calls = []
    monkeypatch.setattr(market_data, "fetch_futures_basis", lambda *args, **kwargs: calls.append(args))
    summary = {"spot": 100.0}

    result = market_data.apply_futures_basis(summary, "")

    assert result is summary
    assert summary == {"spot": 100.0}
    assert calls == []
