from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from .key_level_summary import KEY_LEVEL_DISTANCE_THRESHOLD, compute_key_level_summary
from .snapshot_file_cache import SnapshotFileCache
from .time_utils import key_level_start_utc


class SnapshotService:
    def __init__(
        self,
        *,
        list_trading_days: Callable[[str], list[dict]],
        latest_snapshot_id_for_trading_day: Callable[[str, str], str],
        parse_history_snapshot_id: Callable[[str], tuple[Any, str]],
        chart_payload_from_history: Callable[[Any, str, str, float], tuple[dict, dict, list[dict], list[dict], dict]],
        day_series_from_history: Callable[[Any, dict, str, float], tuple[list[dict], list[dict]]],
        replay_snapshots_from_history: Callable[[Any, dict, str], list[dict]],
        filter_replay_series: Callable[[list[dict], list[dict], dict, str], tuple[list[dict], list[dict]]],
        session_from_summary: Callable[[dict], dict],
        candles_for_session: Callable[[str, dict], tuple[list[dict], str | None]],
        summary_path_from_id: Callable[[str], Any],
        chart_payload_from_summary_path: Callable[[Any, str, float], tuple[dict, dict, list[dict], list[dict], dict]],
        day_series_from_summary_path: Callable[[Any, str, float], tuple[list[dict], list[dict]]],
        replay_snapshots_from_summary_path: Callable[[Any, str], list[dict]],
        skew_tenors_payload_for_summary: Callable[[str, dict], list[dict]],
        latest_history_snapshot: Callable[[str], tuple[Any, dict] | None],
        latest_summary_path: Callable[[str], Any],
        vn_tz: Any,
        file_cache: SnapshotFileCache | None = None,
        key_level_window: float = 150.0,
        seed_prev_day_eod_summary: Callable[[str, str, float], tuple[dict | None, list[dict]]] | None = None,
    ) -> None:
        self._key_level_window = key_level_window
        self._seed_prev_day_eod_summary = seed_prev_day_eod_summary
        self._list_trading_days = list_trading_days
        self._latest_snapshot_id_for_trading_day = latest_snapshot_id_for_trading_day
        self._parse_history_snapshot_id = parse_history_snapshot_id
        self._chart_payload_from_history = chart_payload_from_history
        self._day_series_from_history = day_series_from_history
        self._replay_snapshots_from_history = replay_snapshots_from_history
        self._filter_replay_series = filter_replay_series
        self._session_from_summary = session_from_summary
        self._candles_for_session = candles_for_session
        self._summary_path_from_id = summary_path_from_id
        self._chart_payload_from_summary_path = chart_payload_from_summary_path
        self._day_series_from_summary_path = day_series_from_summary_path
        self._replay_snapshots_from_summary_path = replay_snapshots_from_summary_path
        self._skew_tenors_payload_for_summary = skew_tenors_payload_for_summary
        self._latest_history_snapshot = latest_history_snapshot
        self._latest_summary_path = latest_summary_path
        self._vn_tz = vn_tz
        self._file_cache = file_cache

    def list_trading_days(self, ticker: str) -> list[dict]:
        return self._list_trading_days(ticker)

    def load_snapshot_state(self, snapshot_id: str, ticker: str, window: float, *, refresh: bool = False) -> dict:
        started = time.perf_counter()
        original_snapshot_id = snapshot_id
        if snapshot_id.startswith("day:"):
            snapshot_id = self._latest_snapshot_id_for_trading_day(snapshot_id, ticker)
        cache_status = "disabled"
        if self._file_cache is not None:
            cached = None if refresh else self._file_cache.load(ticker=ticker, snapshot_id=snapshot_id, window=window)
            cache_status = "refresh" if refresh else ("hit" if cached is not None else "miss")
            if cached is not None:
                self._log_snapshot_cache_hit(started, original_snapshot_id, snapshot_id, ticker, cache_status)
                return cached
        if snapshot_id.startswith("history:"):
            summary_history_path, snapshot_utc = self._parse_history_snapshot_id(snapshot_id)
            chart_started = time.perf_counter()
            summary, point, rows, history, gex_snapshot = self._chart_payload_from_history(
                summary_history_path, snapshot_utc, ticker, window
            )
            chart_ms = self._elapsed_ms(chart_started)
            history = [
                row for row in history
                if str(row.get("snapshot_utc") or "") <= str(summary.get("snapshot_utc") or "")
            ]
            series_started = time.perf_counter()
            points, ribbon = self._day_series_from_history(summary_history_path, summary, ticker, window)
            replay_snapshots = self._replay_snapshots_from_history(summary_history_path, summary, ticker)
            series_ms = self._elapsed_ms(series_started)
            payload_started = time.perf_counter()
            payload = self._state_payload(
                snapshot_id, ticker, summary, point, rows, history, gex_snapshot, points, ribbon, replay_snapshots
            )
            payload_ms = self._elapsed_ms(payload_started)
            if self._file_cache is not None:
                self._file_cache.save(ticker=ticker, snapshot_id=snapshot_id, window=window, state=payload)
            self._log_snapshot_timing(
                started, original_snapshot_id, snapshot_id, ticker, "history", cache_status, chart_ms, series_ms, payload_ms
            )
            return payload

        summary_path = self._summary_path_from_id(snapshot_id)
        chart_started = time.perf_counter()
        summary, point, rows, history, gex_snapshot = self._chart_payload_from_summary_path(summary_path, ticker, window)
        chart_ms = self._elapsed_ms(chart_started)
        history = [
            row for row in history
            if str(row.get("snapshot_utc") or "") <= str(summary.get("snapshot_utc") or "")
        ]
        series_started = time.perf_counter()
        points, ribbon = self._day_series_from_summary_path(summary_path, ticker, window)
        replay_snapshots = self._replay_snapshots_from_summary_path(summary_path, ticker)
        series_ms = self._elapsed_ms(series_started)
        payload_started = time.perf_counter()
        payload = self._state_payload(
            snapshot_id, ticker, summary, point, rows, history, gex_snapshot, points, ribbon, replay_snapshots
        )
        payload_ms = self._elapsed_ms(payload_started)
        if self._file_cache is not None:
            self._file_cache.save(ticker=ticker, snapshot_id=snapshot_id, window=window, state=payload)
        self._log_snapshot_timing(
            started, original_snapshot_id, snapshot_id, ticker, "summary", cache_status, chart_ms, series_ms, payload_ms
        )
        return payload

    def load_latest(self, ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
        latest_history = self._latest_history_snapshot(ticker)
        if latest_history is not None:
            summary_history_path, summary = latest_history
            return self._chart_payload_from_history(summary_history_path, summary["snapshot_utc"], ticker, window)
        return self._chart_payload_from_summary_path(self._latest_summary_path(ticker), ticker, window)

    def _state_payload(
        self,
        snapshot_id: str,
        ticker: str,
        summary: dict,
        point: dict,
        rows: list[dict],
        history: list[dict],
        gex_snapshot: dict,
        points: list[dict],
        ribbon: list[dict],
        replay_snapshots: list[dict],
    ) -> dict:
        points, ribbon = self._filter_replay_series(
            points,
            ribbon,
            self._session_from_summary(summary),
            summary["snapshot_utc"],
        )
        if not points:
            points = [point]
        if not ribbon:
            ribbon = [gex_snapshot]
        snapshot_vn = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.notna(snapshot_vn):
            summary["snapshot_vn"] = snapshot_vn.tz_convert(self._vn_tz).isoformat()
        session = self._session_from_summary(summary)
        candles, candles_error = self._candles_for_session(ticker, session)
        # Replay day D against D-1's close, then switch to the selected
        # snapshot's intraday data when price has moved beyond the same
        # threshold used by the live panel.
        key_level_summary = {"status": "pending"}
        snapshot_utc = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        market_open_utc = session.get("market_open_utc")
        level_is_active = (
            bool(market_open_utc)
            and pd.notna(snapshot_utc)
            and snapshot_utc >= pd.Timestamp(key_level_start_utc(market_open_utc))
        )
        if self._seed_prev_day_eod_summary is not None and level_is_active:
            picked_day = summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date")
            eod_summary, eod_rows = self._seed_prev_day_eod_summary(ticker, picked_day, self._key_level_window)
            live_spot = pd.to_numeric(summary.get("spot"), errors="coerce")
            eod_spot = pd.to_numeric(eod_summary.get("spot"), errors="coerce") if eod_summary else float("nan")
            use_intraday = not eod_summary or (
                pd.notna(live_spot) and pd.notna(eod_spot) and eod_spot > 0
                and abs(live_spot - eod_spot) / eod_spot > KEY_LEVEL_DISTANCE_THRESHOLD
            )
            key_level_summary = compute_key_level_summary(
                summary if use_intraday else eod_summary,
                rows if use_intraday else eod_rows,
                live_spot if pd.notna(live_spot) else None,
                basis="intraday" if use_intraday else "eod",
            )
        return {
            "points": points,
            "gex_ribbon": ribbon,
            "history": history,
            "by_strike": rows,
            "skew_by_strike": rows,
            "skew_summary": summary,
            "skew_tenors": self._skew_tenors_payload_for_summary(ticker, summary),
            "latest_summary": summary,
            "levels_summary": summary,
            "key_level_summary": key_level_summary,
            "latest_error": None,
            "running": False,
            "successes": 0,
            "failures": 0,
            "next_fetch": None,
            "session": session,
            "candles": candles,
            "candles_error": candles_error,
            "history_snapshot_id": snapshot_id,
            "replay_snapshots": replay_snapshots,
        }

    def _log_snapshot_timing(
        self,
        started: float,
        requested_id: str,
        resolved_id: str,
        ticker: str,
        mode: str,
        cache_status: str,
        chart_ms: float,
        series_ms: float,
        payload_ms: float,
    ) -> None:
        print(
            "[perf] service.snapshot "
            f"ticker={ticker} mode={mode} cache={cache_status} requested_id={requested_id} resolved_id={resolved_id} "
            f"chart_ms={chart_ms:.1f} series_ms={series_ms:.1f} "
            f"payload_ms={payload_ms:.1f} total_ms={self._elapsed_ms(started):.1f}",
            flush=True,
        )


    def _log_snapshot_cache_hit(
        self,
        started: float,
        requested_id: str,
        resolved_id: str,
        ticker: str,
        cache_status: str,
    ) -> None:
        print(
            "[perf] service.snapshot "
            f"ticker={ticker} mode=file-cache cache={cache_status} "
            f"requested_id={requested_id} resolved_id={resolved_id} total_ms={self._elapsed_ms(started):.1f}",
            flush=True,
        )

    def _elapsed_ms(self, started: float) -> float:
        return (time.perf_counter() - started) * 1000
