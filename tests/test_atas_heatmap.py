from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen
import json

from live_dashboard.atas_heatmap import build_atas_heatmap
from live_dashboard.http_server import DashboardRequestHandler
from live_dashboard.state import LiveState


def sample_state():
    session = {"trading_date": "2026-10-19", "market_open_utc": "2026-10-19T13:30:00Z",
               "market_close_utc": "2026-10-19T20:00:00Z"}
    points = [{"time": f"2026-10-19T13:{minute}:00Z", "spot": 29000}
              for minute in ("31", "32", "33", "34", "35")]
    def ribbon(minute, expiry, effective, source):
        return {"time": f"2026-10-19T13:{minute}:00Z", "expiry": expiry,
                "effective_snapshot_date": effective, "source_timestamp": source,
                "rows": [{"strike": 29005, "net_gex": -100000},
                         {"strike": 30000, "net_gex": 900000}]}
    return {"session": session, "points": points, "gex_ribbon": [
        ribbon("31", "2026-10-19", "2026-10-19", "2026-10-19T13:30:00Z"),
        ribbon("32", "2026-10-19", "2026-10-19", "2026-10-19T13:30:00Z"),
        ribbon("33", "2026-10-20", "2026-10-19", "2026-10-19T13:30:00Z"),
        ribbon("34", "2026-10-19", "2026-10-16", "2026-10-16T19:00:00Z"),
        ribbon("35", "2026-10-19", "2026-10-19", "2026-10-16T19:00:00Z"),
    ]}


def test_only_fresh_0dte_samples_keep_each_poll_and_range():
    result = build_atas_heatmap(sample_state(), ticker="NDX", strike_range=400)
    assert result["model"] == "classic_oi_type_sign_estimate"
    assert [sample["time"] for sample in result["samples"]] == [
        "2026-10-19T13:31:00Z", "2026-10-19T13:32:00Z"]
    assert result["samples"][0]["rows"] == [{"strike": 29005.0, "net_gex": -100000.0}]


def test_http_atas_heatmap_returns_compact_ndx_series():
    state = LiveState(sample_state()["session"])
    state.secondary_ticker = "NDX"
    class Intraday:
        def load_heatmap_state(self, trading_date, ticker, window):
            assert trading_date == "2026-10-19"
            if ticker == "QQQ":
                assert window == 25.0
                state = sample_state()
                for point in state["points"]: point["spot"] = 725
                for snapshot in state["gex_ribbon"]:
                    snapshot["rows"][0]["strike"] = 730
                    snapshot["rows"][1]["strike"] = 900
                return state
            assert (ticker, window) == ("NDX", 400.0)
            return sample_state()
    class Handler(DashboardRequestHandler): pass
    Handler.state = state
    Handler.ticker = "QQQ"
    Handler.intraday_service = Intraday()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/atas/heatmap?date=2026-10-19&ticker=NDX") as response:
                body = json.load(response)
            assert body["status"] == "ok" and len(body["samples"]) == 2
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/atas/heatmap?ticker=NDX") as response:
                assert json.load(response)["trading_date"] == "2026-10-19"
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/atas/heatmap?ticker=QQQ") as response:
                qqq = json.load(response)
            assert qqq["ticker"] == "QQQ" and qqq["price_basis"] == "qqq_etf"
            assert qqq["samples"][0]["rows"] == [{"strike": 730.0, "net_gex": -100000.0}]
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_heatmap_state_does_not_require_daily_snapshot():
    from live_dashboard.intraday_service import IntradayService

    class MissingDailySnapshot:
        def load_snapshot_state(self, *args, **kwargs):
            raise AssertionError("Heatmap must not load a daily summary")

    service = IntradayService(
        session_for_trading_date=lambda day: sample_state()["session"],
        seed_session_data=lambda ticker, session, window: (
            sample_state()["points"], sample_state()["gex_ribbon"]),
        latest_snapshot_id_for_trading_day=lambda *args: (_ for _ in ()).throw(
            AssertionError("Heatmap must not resolve a daily snapshot")),
        candles_for_session=lambda *args: ([], None),
        snapshot_service=MissingDailySnapshot(),
        find_expected_move_anchor=lambda *args: None,
    )
    result = service.load_heatmap_state("2026-10-19", "NDX", 400)
    assert len(result["gex_ribbon"]) == 5
    assert build_atas_heatmap(result, ticker="NDX")["status"] == "ok"


def test_qqq_heatmap_keeps_source_strikes_without_ndx_price_mapping():
    state = sample_state()
    for point in state["points"]: point["spot"] = 725
    for snapshot in state["gex_ribbon"]:
        snapshot["rows"][0]["strike"] = 730
        snapshot["rows"][1]["strike"] = 900
    result = build_atas_heatmap(state, ticker="QQQ", strike_range=25)
    assert result["ticker"] == "QQQ" and result["price_basis"] == "qqq_etf"
    assert [row["strike"] for row in result["samples"][0]["rows"]] == [730.0]
