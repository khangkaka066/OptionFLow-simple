from __future__ import annotations

import time
from collections.abc import Hashable


class ResponseCache:
    def __init__(self, *, max_entries: int = 64) -> None:
        self.max_entries = max_entries
        self._items: dict[Hashable, tuple[float, bytes]] = {}

    def get(self, key: Hashable, ttl_seconds: float) -> bytes | None:
        cached = self._items.get(key)
        if cached is None:
            return None
        cached_at, data = cached
        if time.monotonic() - cached_at > ttl_seconds:
            self._items.pop(key, None)
            return None
        return data

    def set(self, key: Hashable, data: bytes) -> None:
        if len(self._items) >= self.max_entries:
            self._items.pop(next(iter(self._items)), None)
        self._items[key] = (time.monotonic(), data)

    def delete(self, key: Hashable) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()
