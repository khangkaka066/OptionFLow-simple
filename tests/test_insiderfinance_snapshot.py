from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import pandas as pd
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import daily_qqq_snapshot as snapshot
from sources import cboe, insiderfinance
from live_dashboard.data_store import DataStore
from live_dashboard.greek_surface_service import GreekSurfaceService
from render_gex_interactive import build_tenor_curves


def sample_chain(ticker="QQQ"):
    return insiderfinance.parse_chain({
        "ticker": ticker,
        "spot": 500.0,
        # Already September 24 in Vietnam, still September 23 in New York.
        "timestamp": "2026-09-24T00:23:00+00:00",
        "isStale": False,
        "options": [
            {"strike": 500, "expireYear": 2026, "expireMonth": month,
             "expireDay": day, "cp": cp, "openInterest": 100,
             "impliedVol": 0.2, "bid": 4.0, "ask": 4.1}
            for month, day in [(9, 22), (9, 23), (9, 25), (10, 16)]
            for cp in ["C", "P"]
        ],
    }, ticker)


def sample_volume(ticker="QQQ"):
    return {
        "timestamp": "2026-09-23 20:20:00",
        "data": {"current_price": 999.0, "options": [
            {"option": f"{ticker}2609{day:02d}{cp}00500000", "volume": day * 10 + offset,
             "open_interest": 9999, "iv": 0.9, "bid": 20, "ask": 21}
            for day in [23, 25] for cp, offset in [("C", 1), ("P", 2)]
        ]},
    }


class InsiderFinanceSnapshotTests(unittest.TestCase):
    def test_api_keeps_contracts_with_zero_greeks(self):
        raw = sample_chain().raw
        raw["options"][0].update({"gamma": 0, "delta": 0})
        response = Mock()
        response.json.return_value = raw
        with patch.object(insiderfinance.requests, "get", return_value=response) as get:
            with patch.object(insiderfinance, "fetch_page_raw", side_effect=AssertionError("unexpected HTML fallback")):
                data = insiderfinance.fetch_chain("qqq")
        self.assertEqual(len(data.chain), len(raw["options"]))
        self.assertEqual(data.raw["fetch_source"], "api")
        self.assertEqual(get.call_args.args[0], insiderfinance.INSIDERFINANCE_API_URL.format(ticker="QQQ"))

    def test_api_retries_then_marks_html_fallback(self):
        with patch.object(insiderfinance.requests, "get", side_effect=requests.Timeout("timeout")) as get:
            with patch.object(insiderfinance.time, "sleep"), patch.object(insiderfinance, "fetch_page_raw", return_value=sample_chain().raw):
                data = insiderfinance.fetch_chain("QQQ")
        self.assertEqual(get.call_count, insiderfinance.IF_FETCH_RETRIES)
        self.assertEqual(data.raw["fetch_source"], "html_fallback")

    def test_wrong_ticker_response_is_rejected(self):
        response = Mock()
        response.json.return_value = sample_chain("NDX").raw | {"ticker": "NDX"}
        with patch.object(insiderfinance.requests, "get", return_value=response), patch.object(insiderfinance.time, "sleep"):
            with patch.object(insiderfinance, "fetch_page_raw", return_value=sample_chain().raw):
                self.assertEqual(insiderfinance.fetch_raw("QQQ")["fetch_source"], "html_fallback")

    def test_zero_horizon_includes_far_expiries(self):
        _, expiries = insiderfinance.snapshot_chain(sample_chain(), date(2026, 9, 23), all_expiries=True, horizon_days=0)
        self.assertEqual(expiries, ["2026-09-23", "2026-09-25", "2026-10-16"])

    def test_horizon_preserves_contracts_and_missing_volume(self):
        chain, expiries = insiderfinance.snapshot_chain(
            sample_chain(), date(2026, 9, 23), all_expiries=True, horizon_days=10,
        )
        self.assertEqual(expiries, ["2026-09-23", "2026-09-25"])
        self.assertEqual(len(chain), 4)
        self.assertTrue((chain["openInterest"] == 100).all())
        self.assertTrue(chain["volume"].isna().all())

    def test_unavailable_expiry_fails(self):
        with self.assertRaisesRegex(ValueError, "no available expiries"):
            insiderfinance.snapshot_chain(sample_chain(), date(2026, 9, 23), expiry="2026-09-24")

    def test_volume_merge_preserves_primary_fields_and_zero(self):
        primary, _ = insiderfinance.snapshot_chain(sample_chain(), date(2026, 9, 23), all_expiries=True)
        raw = sample_volume()
        raw["data"]["options"][0]["volume"] = 0
        raw["data"]["options"].append({"option": "QQQ260923C00600000", "volume": 999})
        merged = cboe.supplement_volume(primary, cboe.parse_chain(raw, "QQQ").chain)
        pd.testing.assert_frame_equal(
            merged.drop(columns=["volume", "volume_source"]),
            primary.drop(columns=["volume", "volume_source"]),
        )
        self.assertEqual(merged["volume"].iloc[0], 0)
        self.assertEqual(merged["volume_source"].iloc[0], "cboe")
        self.assertEqual(merged["volume"].iloc[1], 232)
        self.assertEqual(merged["volume"].iloc[2], 251)
        self.assertTrue(merged.loc[merged.expiry == "2026-10-16", "volume"].isna().all())

    def test_ambiguous_or_invalid_volumes_stay_missing(self):
        primary, _ = insiderfinance.snapshot_chain(sample_chain("NDX"), date(2026, 9, 23), all_expiries=True)
        raw = sample_volume("NDX")
        raw["data"]["options"].append({"option": "NDXP260923C00500000", "volume": 500})
        for option, bad_value in zip(raw["data"]["options"][1:4], [-1, float("inf"), "bad"]):
            option["volume"] = bad_value
        merged = cboe.supplement_volume(primary, cboe.parse_chain(raw, "NDX").chain)
        self.assertEqual(len(merged), len(primary))
        self.assertTrue(merged["volume"].isna().all())
        self.assertEqual(set(merged["volume_source"]), {"unavailable"})

    def test_quote_supplement_only_fills_missing_fields(self):
        primary, _ = insiderfinance.snapshot_chain(sample_chain(), date(2026, 9, 23), all_expiries=True)
        primary.loc[0, ["impliedVolatility", "bid", "ask"]] = [0, 0, 4.1]
        primary.loc[2, ["impliedVolatility", "bid", "ask"]] = [float("nan"), 5, 4]
        raw = sample_volume()
        raw["data"]["options"][2]["iv"] = -1
        raw["data"]["options"][2]["bid"] = float("inf")
        merged = cboe.supplement_quotes(primary, cboe.parse_chain(raw, "QQQ").chain)
        self.assertEqual(merged.loc[0, "impliedVolatility"], 0.9)
        self.assertEqual(merged.loc[0, ["bid", "ask"]].tolist(), [20, 21])
        self.assertEqual(merged.loc[0, "quote_source"], "cboe")
        self.assertEqual(merged.loc[1, ["impliedVolatility", "bid", "ask"]].tolist(), [0.2, 4, 4.1])
        self.assertTrue(pd.isna(merged.loc[2, "impliedVolatility"]))
        self.assertEqual(merged.loc[2, "quote_source"], "unavailable")
        self.assertEqual(merged.loc[2, "iv_source"], "unavailable")
        pd.testing.assert_series_equal(merged.openInterest, primary.openInterest)

    def test_ambiguous_ndx_quotes_are_not_used(self):
        primary, _ = insiderfinance.snapshot_chain(sample_chain("NDX"), date(2026, 9, 23))
        primary.loc[0, ["impliedVolatility", "bid", "ask"]] = 0
        raw = sample_volume("NDX")
        raw["data"]["options"].append({**raw["data"]["options"][0], "option": "NDXP260923C00500000"})
        merged = cboe.supplement_quotes(primary, cboe.parse_chain(raw, "NDX").chain)
        self.assertEqual(merged.loc[0, "impliedVolatility"], 0)
        self.assertEqual(merged.loc[0, "quote_source"], "unavailable")

    def test_eod_outputs_keep_insiderfinance_chain_and_add_cboe_volume(self):
        for ticker, extra, expiry, volume_available in [
            ("QQQ", [], "2026-09-23", True),
            ("NDX", [], "2026-09-23", True),
            ("QQQ", ["--all-expiries", "--expiry-horizon-days", "10"], "ALL", True),
            ("QQQ", [], "2026-09-23", False),
        ]:
            with self.subTest(ticker=ticker, expiry=expiry, volume_available=volume_available), tempfile.TemporaryDirectory() as tmp:
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(sys, "argv", [
                        "snapshot", "--source", "insiderfinance", "--ticker", ticker,
                        "--snapshot-date", "2026-09-24", "--output-root", tmp,
                    ] + extra))
                    stack.enter_context(patch.object(insiderfinance, "fetch_chain", return_value=sample_chain(ticker)))
                    stack.enter_context(patch.object(
                        cboe, "fetch_raw", return_value=sample_volume(ticker),
                        side_effect=None if volume_available else RuntimeError("volume feed offline"),
                    ))
                    for provider, method in [(snapshot.yahoo, "fetch_chain"),
                                             (snapshot.yahoo, "fetch_multi_chain"), (insiderfinance, "fetch_spot")]:
                        stack.enter_context(patch.object(provider, method, side_effect=AssertionError("unexpected source request")))
                    stack.enter_context(patch.object(snapshot, "run_cleanup"))
                    stack.enter_context(patch.object(snapshot.storage, "_mongo_store", return_value=None))
                    market = stack.enter_context(patch.object(
                        snapshot.storage, "append_market_dataset", wraps=snapshot.storage.append_market_dataset,
                    ))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    snapshot.main()
                summary = json.loads((Path(tmp) / "2026-09-24" / f"{ticker}_{expiry}_summary.json").read_text())
                self.assertEqual(summary["chain_source"], "insiderfinance")
                self.assertEqual(summary["spot"], 500.0)
                self.assertEqual(summary["effective_snapshot_date"], "2026-09-23")
                self.assertEqual(summary["volume_source"], "cboe" if volume_available else "unavailable")
                self.assertEqual(summary["volume_cboe_count"], (4 if expiry == "ALL" else 2) if volume_available else 0)
                by_strike = pd.read_parquet(Path(tmp) / "2026-09-24" / f"{ticker}_{expiry}_by_strike.parquet")
                self.assertEqual(by_strike["call_volume"].sum(), (482 if expiry == "ALL" else 231) if volume_available else 0)
                self.assertEqual(by_strike["put_volume"].sum(), (484 if expiry == "ALL" else 232) if volume_available else 0)
                history = pd.read_parquet(Path(tmp) / "2026-09-24" / "history" / f"{ticker}_{expiry}_snapshots.parquet")
                self.assertIn("raw_insiderfinance_path", history.columns)
                self.assertNotIn("raw_yahoo_path", history.columns)
                frames = market.call_args.kwargs["raw_frames"]
                self.assertEqual(len(frames), 1)
                self.assertEqual(set(frames[0]["source"]), {"insiderfinance_reconciled"})
                self.assertEqual(set(frames[0]["volume_source"]), {"cboe" if volume_available else "unavailable"})
                if volume_available:
                    self.assertIn("raw_cboe_path", history.columns)
                    self.assertEqual(summary["volume_timestamp"], "2026-09-23 20:20:00")
                else:
                    self.assertEqual(summary["volume_error"], "volume feed offline")
                if expiry == "ALL":
                    self.assertEqual(summary["included_expiries"], ["2026-09-23", "2026-09-25"])
                    self.assertEqual(len(market.call_args.kwargs["tenor_atm_iv"]), 2)

    def test_skew_and_surface_prefer_reconciled_chain(self):
        primary, _ = insiderfinance.snapshot_chain(sample_chain(), date(2026, 9, 23), all_expiries=True)
        primary = cboe.supplement_quotes(primary, cboe.parse_chain(sample_volume(), "QQQ").chain)
        stamp = "2026-09-24T06:00:00+00:00"
        raw = snapshot.storage.normalize_raw_chain(
            primary, capture_ts=stamp, ticker="QQQ", source="insiderfinance_reconciled", spot=500,
            source_ts="2026-09-23T19:00:00+00:00",
        )
        # Adversarial ordering: older secondary rows must never win selection.
        secondary = raw.copy()
        secondary["source"] = "yahoo"
        secondary["spot"] = 999
        secondary["iv"] = 0.8
        secondary["open_interest"] = 9999
        combined = pd.concat([secondary, raw], ignore_index=True)
        curves = build_tenor_curves(combined, 500, "2026-09-23")
        self.assertEqual(len(curves), 2)
        self.assertTrue(all(abs(curve["atm_iv"] - 0.2) < 0.001 for curve in curves))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "market/ticker=QQQ/date=2026-09-23/raw_chain.parquet"
            p.parent.mkdir(parents=True)
            combined.to_parquet(p, index=False)
            store = DataStore(root / "options", ny_tz=ZoneInfo("America/New_York"), vn_tz=ZoneInfo("Asia/Ho_Chi_Minh"))
            service = GreekSurfaceService(store)
            summary, rows = service._load_from_raw_chain("QQQ", "2026-09-23")
            self.assertEqual(summary["spot"], 500)
            self.assertEqual(summary["effective_snapshot_date"], "2026-09-23")
            self.assertEqual(rows["expiry"].nunique(), 3)
            self.assertTrue((rows["call_oi"] == 100).all())
            self.assertTrue((rows["put_oi"] == 100).all())


if __name__ == "__main__":
    unittest.main()
