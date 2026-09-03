from __future__ import annotations

import time
from collections.abc import Callable

from .intraday_file_cache import IntradayFileCache, session_is_closed
from .snapshot_service import SnapshotService


class IntradayService:
    def __init__(
        self,
        *,
        session_for_trading_date: Callable[[str], dict],
        seed_session_data: Callable[[str, dict, float], tuple[list[dict], list[dict]]],
        latest_snapshot_id_for_trading_day: Callable[[str, str], str],
        candles_for_session: Callable[[str, dict], tuple[list[dict], str | None]],
        snapshot_service: SnapshotService,
        file_cache: IntradayFileCache | None = None,
    ) -> None:
        self._session_for_trading_date = session_for_trading_date
        self._seed_session_data = seed_session_data
        self._latest_snapshot_id_for_trading_day = latest_snapshot_id_for_trading_day
        self._candles_for_session = candles_for_session
        self._snapshot_service = snapshot_service
        self._file_cache = file_cache

    def load_state(self, trading_date: str, ticker: str, window: float, *, refresh: bool = False) -> dict:
        """Full intraday replay for live-only panels on a selected NY trading date."""
        started = time.perf_counter()
        session = self._session_for_trading_date(trading_date)
        session_ms = self._elapsed_ms(started)
        can_use_file_cache = self._file_cache is not None and session_is_closed(session)
        seed_cache_status = "disabled"
        cached_seed = None
        if can_use_file_cache and not refresh:
            cached_seed = self._file_cache.load(
                ticker=ticker, trading_date=session["trading_date"], window=window
            )
            seed_cache_status = "hit" if cached_seed is not None else "miss"
        if cached_seed is None:
            points, ribbon = self._seed_session_data(ticker, session, window)
            if can_use_file_cache:
                self._file_cache.save(
                    ticker=ticker,
                    trading_date=session["trading_date"],
                    window=window,
                    points=points,
                    ribbon=ribbon,
                )
                if refresh:
                    seed_cache_status = "refresh"
        else:
            points, ribbon = cached_seed
        seed_ms = self._elapsed_ms(started) - session_ms
        snapshot_id = self._latest_snapshot_id_for_trading_day("day:" + session["trading_date"], ticker)
        snapshot_id_ms = self._elapsed_ms(started) - session_ms - seed_ms
        payload = self._snapshot_service.load_snapshot_state(snapshot_id, ticker, window, refresh=refresh)
        snapshot_ms = self._elapsed_ms(started) - session_ms - seed_ms - snapshot_id_ms
        payload["points"] = points or payload.get("points", [])
        payload["gex_ribbon"] = ribbon or payload.get("gex_ribbon", [])
        payload["session"] = session
        if "candles" not in payload:
            candles, candles_error = self._candles_for_session(ticker, session)
            payload["candles"] = candles
            payload["candles_error"] = candles_error
        payload["running"] = False
        payload["next_fetch"] = None
        total_ms = self._elapsed_ms(started)
        print(
            "[perf] service.intraday "
            f"ticker={ticker} date={trading_date} points={len(points)} ribbon={len(ribbon)} "
            f"seed_cache={seed_cache_status} session_ms={session_ms:.1f} seed_ms={seed_ms:.1f} "
            f"snapshot_id_ms={snapshot_id_ms:.1f} snapshot_ms={snapshot_ms:.1f} total_ms={total_ms:.1f}",
            flush=True,
        )
        return payload

    def _elapsed_ms(self, started: float) -> float:
        return (time.perf_counter() - started) * 1000
