from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_VERSION = 1


class IntradayFileCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, *, ticker: str, trading_date: str, window: float) -> tuple[list[dict], list[dict]] | None:
        path = self._path(ticker=ticker, trading_date=trading_date, window=window)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return None
        if payload.get("version") != CACHE_VERSION:
            return None
        if payload.get("ticker") != ticker.upper():
            return None
        if payload.get("trading_date") != trading_date:
            return None
        try:
            cached_window = float(payload.get("window"))
        except (TypeError, ValueError):
            return None
        if cached_window != float(window):
            return None
        points = payload.get("points")
        ribbon = payload.get("gex_ribbon")
        if not isinstance(points, list) or not isinstance(ribbon, list):
            return None
        return points, ribbon

    def save(
        self,
        *,
        ticker: str,
        trading_date: str,
        window: float,
        points: list[dict],
        ribbon: list[dict],
    ) -> None:
        path = self._path(ticker=ticker, trading_date=trading_date, window=window)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": CACHE_VERSION,
            "ticker": ticker.upper(),
            "trading_date": trading_date,
            "window": float(window),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "points": points,
            "gex_ribbon": ribbon,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, allow_nan=False)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _path(self, *, ticker: str, trading_date: str, window: float) -> Path:
        window_key = ("%g" % float(window)).replace(".", "p")
        return self.root / ticker.upper() / f"{trading_date}_w{window_key}.json"


def session_is_closed(session: dict) -> bool:
    try:
        close_ts = datetime.fromisoformat(str(session["market_close_utc"]))
    except Exception:
        return False
    if close_ts.tzinfo is None:
        close_ts = close_ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= close_ts.astimezone(timezone.utc)
