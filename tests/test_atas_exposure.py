from datetime import datetime, timezone
from copy import deepcopy
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen
import json

import pytest

from live_dashboard.atas_exposure import build_atas_exposure, fetch_qqq_close_1559
from live_dashboard.http_server import DashboardRequestHandler
from live_dashboard.state import LiveState


NOW = datetime(2026, 10, 16, 14, 42, tzinfo=timezone.utc)


def sample():
    return {
        "latest_summary": {"ticker": "QQQ", "snapshot_utc": "2026-10-16T14:41:30+00:00",
            "expiry": "2026-10-16", "included_expiries": ["2026-10-16"], "spot": 721,
            "net_gex": -300, "gamma_wall_abs": 722, "gamma_flip": None,
            "call_resistance": 725, "put_support": 720},
        "by_strike": [{"strike": 722, "net_gex": -60}, {"strike": 720, "net_gex": 10},
                      {"strike": 721, "net_gex": float("nan")}, {"strike": None, "net_gex": 30}],
        "session": {"market_open_utc": "2026-10-16T13:30:00+00:00",
                    "market_close_utc": "2026-10-16T20:00:00+00:00"},
    }


def test_signed_rows_totals_and_wall_are_not_a_flip():
    data = sample()
    original = deepcopy(data["by_strike"])
    result = build_atas_exposure(data, ticker="QQQ", now=NOW)
    assert [r["strike"] for r in result["rows"]] == [720, 721, 722]
    assert result["rows"][1]["net_gex"] is None
    assert result["rows"][2]["net_gex"] == -60
    assert result["net_totals"]["net_gex"] == -300
    assert result["row_totals"]["net_gex"] == -50
    assert result["levels"]["gamma_wall_abs"] == 722
    assert result["levels"]["gamma_flip"] is None
    assert result["model"] == "classic_oi_type_sign_estimate"
    assert result["status"] == "updating" and not result["stale"]
    assert data["by_strike"][0] == original[0]
    json.dumps(result, allow_nan=False)


def test_eod_is_explicit_never_silent_fallback():
    data = sample()
    data["levels_summary"] = data.pop("latest_summary")
    data["levels_by_strike"] = data.pop("by_strike")
    with pytest.raises(LookupError):
        build_atas_exposure(data, ticker="QQQ", now=NOW)
    assert build_atas_exposure(data, ticker="QQQ", basis="eod", now=NOW)["status"] == "eod"


def test_stale_and_off_session_data_never_claims_live():
    data = sample()
    data["latest_summary"]["source_is_stale"] = True
    assert build_atas_exposure(data, ticker="QQQ", now=NOW)["stale"]
    data["latest_summary"]["source_is_stale"] = False
    later = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
    result = build_atas_exposure(data, ticker="QQQ", now=later)
    assert result["status"] == "session_closed" and result["stale"]
    data["latest_summary"]["snapshot_utc"] = "not a timestamp"
    assert build_atas_exposure(data, ticker="QQQ", now=NOW)["stale"]


@pytest.mark.parametrize("change", ["ticker", "basis", "duplicate"])
def test_invalid_request_or_data_is_rejected(change):
    data = sample()
    if change == "duplicate": data["by_strike"].append({"strike": 722, "net_gex": 10})
    with pytest.raises(ValueError):
        build_atas_exposure(data, ticker="NDX" if change == "ticker" else "QQQ",
                            basis="unknown" if change == "basis" else "intraday", now=NOW)


def test_http_endpoint_uses_live_state_updates_without_cache():
    state = LiveState(sample()["session"])
    state.latest_summary = sample()["latest_summary"]
    state.by_strike = sample()["by_strike"]
    class Handler(DashboardRequestHandler): pass
    Handler.state = state
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/api/atas/exposure") as response:
                assert json.load(response)["rows"][-1]["net_gex"] == -60
            with state.lock:
                state.by_strike = [{"strike": 722, "net_gex": 123}]
            with urlopen(base + "/api/atas/exposure") as response:
                assert json.load(response)["rows"][0]["net_gex"] == 123
            with pytest.raises(HTTPError) as exc:
                urlopen(base + "/api/atas/exposure?ticker=NDX")
            assert exc.value.code == 400
            with pytest.raises(HTTPError) as exc:
                urlopen(base + "/api/atas/reference?date=not-a-date")
            assert exc.value.code == 400
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_reference_requires_exact_1559_bar(monkeypatch):
    import pandas as pd
    from sources import yahoo
    class Ticker:
        def history(self, **kwargs):
            return pd.DataFrame({"Close": [721.25]}, index=pd.DatetimeIndex(["2026-10-16T19:59:00Z"]))
    class Yf:
        def Ticker(self, symbol): return Ticker()
    monkeypatch.setattr(yahoo, "import_yfinance", lambda: Yf())
    result = fetch_qqq_close_1559("2026-10-16")
    assert result["close"] == 721.25
    assert result["bar_start_utc"] == "2026-10-16T19:59:00+00:00"
    def missing(**kwargs):
        return pd.DataFrame({"Close": [721]}, index=pd.DatetimeIndex(["2026-10-16T19:58:00Z"]))
    monkeypatch.setattr(Ticker, "history", lambda self, **kwargs: missing(**kwargs))
    with pytest.raises(LookupError): fetch_qqq_close_1559("2026-10-16")

def test_safe_launcher_propagates_preservation_to_collector(monkeypatch):
    import subprocess
    import live_server
    captured = {}
    def run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(live_server.subprocess, "run", run)
    live_server._run_snapshot_subprocess(["test"], preserve_existing_data=True)
    assert captured["env"]["QUANT_PRESERVE_EXISTING_DATA"] == "1"
    live_server._run_snapshot_subprocess(["test"])
    assert captured["env"] is None
