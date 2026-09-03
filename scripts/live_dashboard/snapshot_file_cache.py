from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .intraday_file_cache import session_is_closed

CACHE_VERSION = 1


class SnapshotFileCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, *, ticker: str, snapshot_id: str, window: float) -> dict | None:
        path = self._path(ticker=ticker, snapshot_id=snapshot_id, window=window)
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
        if payload.get("snapshot_id") != snapshot_id:
            return None
        try:
            cached_window = float(payload.get("window"))
        except (TypeError, ValueError):
            return None
        if cached_window != float(window):
            return None
        state = payload.get("state")
        return state if isinstance(state, dict) else None

    def save(self, *, ticker: str, snapshot_id: str, window: float, state: dict) -> None:
        session = state.get("session")
        if not isinstance(session, dict) or not session_is_closed(session):
            return
        path = self._path(ticker=ticker, snapshot_id=snapshot_id, window=window)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": CACHE_VERSION,
            "ticker": ticker.upper(),
            "snapshot_id": snapshot_id,
            "window": float(window),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "state": state,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, default=str, allow_nan=False)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _path(self, *, ticker: str, snapshot_id: str, window: float) -> Path:
        digest = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()[:24]
        window_key = ("%g" % float(window)).replace(".", "p")
        return self.root / ticker.upper() / f"{digest}_w{window_key}.json"
