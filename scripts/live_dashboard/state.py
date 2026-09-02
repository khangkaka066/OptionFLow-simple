from __future__ import annotations

import threading


class LiveState:
    def __init__(self, session: dict) -> None:
        self.lock = threading.Lock()
        self.points: list[dict] = []
        self.gex_ribbon: list[dict] = []
        self.session_locked: bool = False
        self.history: list[dict] = []
        self.by_strike: list[dict] = []
        self.skew_tenors: list[dict] = []
        self.skew_summary: dict | None = None
        self.skew_by_strike: list[dict] = []
        self.latest_summary: dict | None = None
        self.levels_summary: dict | None = None
        self.levels_locked: bool = False
        self.levels_summary_secondary: dict | None = None
        self.levels_locked_secondary: bool = False
        self.secondary_ticker: str = ""
        self.secondary_futures_ticker: str = ""
        self.candles: list[dict] = []
        self.candles_error: str | None = None
        self.latest_error: str | None = None
        self.running = True
        self.successes = 0
        self.failures = 0
        self.next_fetch: str | None = None
        self.session: dict = session

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "points": self.points,
                "gex_ribbon": self.gex_ribbon,
                "session_locked": self.session_locked,
                "history": self.history,
                "by_strike": self.by_strike,
                "skew_tenors": self.skew_tenors,
                "skew_summary": self.skew_summary,
                "skew_by_strike": self.skew_by_strike,
                "latest_summary": self.latest_summary,
                "levels_summary": self.levels_summary,
                "levels_locked": self.levels_locked,
                "levels_summary_secondary": self.levels_summary_secondary,
                "levels_locked_secondary": self.levels_locked_secondary,
                "secondary_ticker": self.secondary_ticker,
                "secondary_futures_ticker": self.secondary_futures_ticker,
                "candles": self.candles,
                "candles_error": self.candles_error,
                "latest_error": self.latest_error,
                "running": self.running,
                "successes": self.successes,
                "failures": self.failures,
                "next_fetch": self.next_fetch,
                "session": self.session,
            }
