from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import pandas as pd

from .serialization import clean_value


class DataStore:
    def __init__(self, root: Path, *, ny_tz: Any, vn_tz: Any, cache_root: Path | None = None) -> None:
        self.root = root
        self.ny_tz = ny_tz
        self.vn_tz = vn_tz
        self.cache_root = cache_root if cache_root is not None else root.parent / "cache" / "data_store"
        self._trading_days_cache: dict[str, tuple[int, list[dict]]] = {}

    def latest_summary_path(self, ticker: str) -> Path:
        matches = [
            path
            for path in self.root.glob(f"*/{ticker.upper()}_*_summary.json")
            if len(path.stem.split("_")) == 3
        ]
        if not matches:
            raise FileNotFoundError(f"No latest summary found for {ticker}")
        return max(matches, key=lambda path: path.stat().st_mtime)

    def history_summary_paths(self, ticker: str) -> list[Path]:
        return sorted(self.root.glob(f"*/history/{ticker.upper()}_*_snapshots.parquet"))

    def parse_history_json(self, value):
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text or text[0] not in "[{":
            return value
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value

    def summary_from_history_row(self, row: dict) -> dict:
        return {
            key: clean_value(self.parse_history_json(value))
            for key, value in row.items()
            if not key.startswith("recon_")
        }

    def history_by_strike_path(self, summary_history_path: Path) -> Path:
        return summary_history_path.with_name(
            summary_history_path.name.replace("_snapshots.parquet", "_by_strike_history.parquet")
        )

    def history_snapshot_id(self, summary_history_path: Path, snapshot_utc: str) -> str:
        return "history:" + summary_history_path.relative_to(self.root).as_posix() + "#" + quote(snapshot_utc, safe="")

    def parse_history_snapshot_id(self, snapshot_id: str) -> tuple[Path, str]:
        payload = snapshot_id.removeprefix("history:")
        rel_path, encoded_ts = payload.rsplit("#", 1)
        candidate = (self.root / unquote(rel_path)).resolve()
        root = self.root.resolve()
        if (
            root not in candidate.parents
            or candidate.suffix != ".parquet"
            or not candidate.name.endswith("_snapshots.parquet")
        ):
            raise ValueError("invalid history snapshot id")
        if not candidate.exists():
            raise FileNotFoundError("history snapshot not found")
        return candidate, unquote(encoded_ts)

    def latest_history_snapshot(self, ticker: str) -> tuple[Path, dict] | None:
        best: tuple[pd.Timestamp, Path, dict] | None = None
        for path in self.history_summary_paths(ticker):
            try:
                rows = pd.read_parquet(path)
            except Exception:
                continue
            if rows.empty:
                continue
            rows = rows[rows["ticker"].astype(str).str.upper() == ticker.upper()]
            if rows.empty:
                continue
            rows = rows.sort_values("snapshot_utc")
            row = rows.iloc[-1].to_dict()
            snapshot = pd.to_datetime(row.get("snapshot_utc"), errors="coerce", utc=True)
            if pd.isna(snapshot):
                continue
            if best is None or snapshot > best[0]:
                best = (snapshot, path, row)
        if best is None:
            return None
        return best[1], self.summary_from_history_row(best[2])

    def snapshot_id_for_path(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def summary_path_from_id(self, snapshot_id: str) -> Path:
        decoded = unquote(snapshot_id)
        candidate = (self.root / decoded).resolve()
        root = self.root.resolve()
        if root not in candidate.parents or candidate.suffix != ".json" or not candidate.name.endswith("_summary.json"):
            raise ValueError("invalid snapshot id")
        if not candidate.exists():
            raise FileNotFoundError("snapshot not found")
        return candidate

    def snapshot_label(self, path: Path, summary: dict) -> str:
        expiry = summary.get("expiry") or path.stem.split("_")[1]
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.notna(snapshot):
            label_time = snapshot.tz_convert(self.vn_tz).strftime("%Y-%m-%d %H:%M VN")
        else:
            label_time = path.parent.name
        label_type = "Daily" if len(path.stem.split("_")) == 3 else "Snapshot"
        return f"{label_type} · {label_time} · {expiry}"

    def list_history_choices(self, ticker: str) -> list[dict]:
        choices = []
        seen_ids: set[str] = set()
        seen_keys: set[tuple[str, str]] = set()
        for path in self.history_summary_paths(ticker):
            try:
                rows = pd.read_parquet(path)
            except Exception:
                continue
            for row in rows.sort_values("snapshot_utc", ascending=False).to_dict(orient="records"):
                summary = self.summary_from_history_row(row)
                snapshot_utc = summary.get("snapshot_utc")
                if not snapshot_utc:
                    continue
                item_id = self.history_snapshot_id(path, snapshot_utc)
                if item_id in seen_ids:
                    continue
                key = (str(snapshot_utc), str(summary.get("expiry") or ""))
                if key in seen_keys:
                    continue
                seen_ids.add(item_id)
                seen_keys.add(key)
                parsed_snapshot = pd.to_datetime(snapshot_utc, errors="coerce", utc=True)
                label_time = (
                    parsed_snapshot.tz_convert(self.vn_tz).strftime("%Y-%m-%d %H:%M VN")
                    if pd.notna(parsed_snapshot)
                    else str(snapshot_utc)
                )
                choices.append(
                    {
                        "id": item_id,
                        "label": "Snapshot · " + label_time + " · " + str(summary.get("expiry") or ""),
                        "snapshot_utc": snapshot_utc,
                        "expiry": summary.get("expiry"),
                    }
                )
        for path in sorted(
            self.root.glob(f"*/{ticker.upper()}_*_summary.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            item_id = self.snapshot_id_for_path(path)
            if item_id in seen_ids:
                continue
            key = (str(summary.get("snapshot_utc") or ""), str(summary.get("expiry") or ""))
            if key in seen_keys:
                continue
            seen_ids.add(item_id)
            seen_keys.add(key)
            choices.append(
                {
                    "id": item_id,
                    "label": self.snapshot_label(path, summary),
                    "snapshot_utc": summary.get("snapshot_utc"),
                    "expiry": summary.get("expiry"),
                }
            )
        return sorted(choices, key=lambda item: item.get("snapshot_utc") or "", reverse=True)[:2000]

    def list_trading_days(self, ticker: str) -> list[dict]:
        ticker_key = ticker.upper()
        fingerprint = self._trading_days_source_fingerprint(ticker_key)
        cached = self._trading_days_cache.get(ticker_key)
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])
        cached_days = self._load_trading_days_cache(ticker_key, fingerprint)
        if cached_days is not None:
            self._trading_days_cache[ticker_key] = (fingerprint, cached_days)
            return copy.deepcopy(cached_days)
        days = self._build_trading_days(ticker_key)
        self._trading_days_cache[ticker_key] = (fingerprint, days)
        self._save_trading_days_cache(ticker_key, fingerprint, days)
        return copy.deepcopy(days)

    def _build_trading_days(self, ticker: str) -> list[dict]:
        days: dict[str, dict] = {}
        for path in self.history_summary_paths(ticker):
            try:
                rows = pd.read_parquet(path)
            except Exception:
                continue
            if rows.empty or "snapshot_utc" not in rows:
                continue
            rows = rows[rows["ticker"].astype(str).str.upper() == ticker].copy()
            rows["_snapshot_ts"] = pd.to_datetime(rows["snapshot_utc"], errors="coerce", utc=True)
            rows = rows[rows["_snapshot_ts"].notna()].sort_values("snapshot_utc")
            for trading_day, group in rows.groupby(rows["_snapshot_ts"].dt.tz_convert(self.ny_tz).dt.date.astype(str)):
                if group.empty:
                    continue
                latest = group.iloc[-1].to_dict()
                current = days.get(trading_day)
                if current is None:
                    days[trading_day] = {
                        "id": "day:" + trading_day,
                        "label": trading_day,
                        "trading_date": trading_day,
                        "snapshot_count": 0,
                        "latest_snapshot_utc": latest.get("snapshot_utc"),
                        "latest_snapshot_id": self.history_snapshot_id(path, str(latest.get("snapshot_utc"))),
                        "expiry": latest.get("expiry"),
                        "expiries": set(),
                    }
                    current = days[trading_day]
                current["snapshot_count"] += int(group["snapshot_utc"].nunique())
                for expiry in group.get("expiry", pd.Series(dtype=object)).dropna().astype(str).unique():
                    current["expiries"].add(expiry)
                if str(latest.get("snapshot_utc") or "") > str(current.get("latest_snapshot_utc") or ""):
                    current["latest_snapshot_utc"] = latest.get("snapshot_utc")
                    current["latest_snapshot_id"] = self.history_snapshot_id(path, str(latest.get("snapshot_utc")))
                    current["expiry"] = latest.get("expiry")
        for day_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            if day_dir.name in days:
                continue
            candidates = []
            for path in sorted(day_dir.glob(f"{ticker}_*_summary.json"), key=lambda p: p.stat().st_mtime):
                try:
                    summary = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
                if pd.isna(snapshot):
                    continue
                candidates.append((snapshot, path, summary))
            if not candidates:
                continue
            snapshot, path, summary = max(candidates, key=lambda item: item[0])
            trading_day = snapshot.tz_convert(self.ny_tz).date().isoformat()
            if trading_day in days:
                continue
            expiry = summary.get("expiry")
            days[trading_day] = {
                "id": "day:" + trading_day,
                "label": trading_day,
                "trading_date": trading_day,
                "snapshot_count": len(candidates),
                "latest_snapshot_utc": summary.get("snapshot_utc"),
                "latest_snapshot_id": self.snapshot_id_for_path(path),
                "expiry": expiry,
                "expiries": {str(expiry)} if expiry else set(),
            }
        out = []
        for day in days.values():
            expiries = sorted(day.pop("expiries"))
            expiry_label = ", ".join(expiries[:2]) + ("..." if len(expiries) > 2 else "")
            day["label"] = f"{day['trading_date']} · {day['snapshot_count']} mốc"
            if expiry_label:
                day["label"] += f" · {expiry_label}"
            out.append(day)
        return sorted(out, key=lambda item: item["trading_date"], reverse=True)

    def _trading_days_source_fingerprint(self, ticker: str) -> int:
        mtimes: list[int] = []
        history_paths = list(self.history_summary_paths(ticker))
        candidates = list(history_paths)
        if history_paths:
            candidates.extend(
                path
                for path in self.root.glob(f"*/{ticker}_*_summary.json")
                if len(path.stem.split("_")) == 3
            )
        else:
            candidates.extend(self.root.glob(f"*/{ticker}_*_summary.json"))
        for path in candidates:
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:
                continue
        return max(mtimes, default=0)

    def _trading_days_cache_path(self, ticker: str) -> Path:
        return self.cache_root / f"{ticker}_trading_days.json"

    def _load_trading_days_cache(self, ticker: str, fingerprint: int) -> list[dict] | None:
        payload = self._load_trading_days_cache_payload(ticker)
        if payload is None:
            return None
        if int(payload.get("source_fingerprint") or -1) != fingerprint:
            return None
        days = payload.get("days")
        return days if isinstance(days, list) else None

    def _load_trading_days_cache_unvalidated(self, ticker: str) -> list[dict] | None:
        payload = self._load_trading_days_cache_payload(ticker)
        if payload is None:
            return None
        days = payload.get("days")
        return days if isinstance(days, list) else None

    def _load_trading_days_cache_payload(self, ticker: str) -> dict | None:
        path = self._trading_days_cache_path(ticker)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return None
        if payload.get("version") != 1 or payload.get("ticker") != ticker:
            return None
        return payload

    def _save_trading_days_cache(self, ticker: str, fingerprint: int, days: list[dict]) -> None:
        path = self._trading_days_cache_path(ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "ticker": ticker,
            "source_fingerprint": fingerprint,
            "days": days,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, default=str, allow_nan=False)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def latest_snapshot_id_for_trading_day(self, day_id: str, ticker: str) -> str:
        trading_day = day_id.removeprefix("day:")
        ticker_key = ticker.upper()
        if self._is_past_trading_day(trading_day):
            for item in self._load_trading_days_cache_unvalidated(ticker_key) or []:
                if item.get("trading_date") == trading_day and item.get("latest_snapshot_id"):
                    return str(item["latest_snapshot_id"])
        for item in self.list_trading_days(ticker_key):
            if item.get("trading_date") == trading_day and item.get("latest_snapshot_id"):
                return str(item["latest_snapshot_id"])
        raise FileNotFoundError(f"No snapshots found for trading day {trading_day}")

    def _is_past_trading_day(self, trading_day: str) -> bool:
        try:
            today = pd.Timestamp.now(tz="UTC").tz_convert(self.ny_tz).date().isoformat()
        except Exception:
            return False
        return trading_day < today
