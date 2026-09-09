from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo


DEFAULT_DATABASE = "optionflow"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_IV_RANK_SESSIONS = 60
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return _clean_value(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(item) for item in value]
    return value


def _clean_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _clean_value(value) for key, value in doc.items()}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class MongoDatasetStore:
    def __init__(
        self,
        uri: str,
        *,
        database: str = DEFAULT_DATABASE,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        iv_rank_sessions: int = DEFAULT_IV_RANK_SESSIONS,
    ) -> None:
        from pymongo import MongoClient

        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[database]
        self.retention_days = max(1, int(retention_days))
        self.iv_rank_sessions = max(1, int(iv_rank_sessions))
        self._indexes_ready = False

    @classmethod
    def from_env(cls) -> MongoDatasetStore | None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return None
        return cls(
            uri,
            database=os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE).strip() or DEFAULT_DATABASE,
            retention_days=_int_env("MONGODB_DATA_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
            iv_rank_sessions=_int_env("MONGODB_IV_RANK_SESSIONS", DEFAULT_IV_RANK_SESSIONS),
        )

    def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        for name in ("snapshots", "by_strike", "raw_chain", "intraday_metrics"):
            self.db[name].create_index("expire_at", expireAfterSeconds=0)
        self.db.snapshots.create_index(
            [("snapshot_utc", 1), ("ticker", 1), ("expiry", 1)],
            unique=True,
            name="snapshot_unique",
        )
        self.db.by_strike.create_index(
            [("snapshot_utc", 1), ("ticker", 1), ("expiry_scope", 1), ("expiry", 1), ("strike", 1)],
            unique=True,
            name="by_strike_unique",
        )
        self.db.raw_chain.create_index(
            [("capture_ts", 1), ("source", 1), ("ticker", 1), ("expiry", 1), ("strike", 1), ("option_type", 1)],
            unique=True,
            name="raw_chain_unique",
        )
        self.db.intraday_metrics.create_index(
            [("bucket_ts", 1), ("ticker", 1), ("metric_name", 1), ("strike", 1)],
            unique=True,
            name="intraday_metrics_unique",
        )
        self.db.iv_rank_daily.create_index(
            [("ticker", 1), ("trading_date", 1)],
            unique=True,
            name="iv_rank_daily_unique",
        )
        self._indexes_ready = True

    def expire_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=self.retention_days)

    def upsert_snapshot(
        self,
        *,
        ticker: str,
        expiry: str,
        trading_date: str,
        summary: dict,
        reconciliation: dict | None = None,
    ) -> None:
        self.ensure_indexes()
        snapshot_utc = str(summary["snapshot_utc"])
        doc = _clean_doc(
            {
                "ticker": ticker.upper(),
                "expiry": expiry,
                "trading_date": trading_date,
                "snapshot_utc": snapshot_utc,
                "summary": summary,
                "reconciliation": reconciliation or {},
                "updated_at": datetime.now(timezone.utc),
                "expire_at": self.expire_at(),
            }
        )
        self.db.snapshots.update_one(
            {"snapshot_utc": snapshot_utc, "ticker": ticker.upper(), "expiry": expiry},
            {"$set": doc},
            upsert=True,
        )

    def upsert_market_dataset(
        self,
        *,
        ticker: str,
        trading_date: str,
        expiry_scope: str,
        summary: dict,
        by_strike: pd.DataFrame,
        intraday_rows: list[dict],
        raw_frames: list[pd.DataFrame] | None = None,
        level_row: dict | None = None,
    ) -> None:
        self.ensure_indexes()
        expire_at = self.expire_at()
        capture_ts = str(summary["snapshot_utc"])
        ticker_key = ticker.upper()

        if not by_strike.empty:
            self._bulk_replace(
                self.db.by_strike,
                by_strike,
                extra={
                    "capture_ts": capture_ts,
                    "snapshot_utc": capture_ts,
                    "ticker": ticker_key,
                    "trading_date": trading_date,
                    "expiry_scope": expiry_scope,
                    "expire_at": expire_at,
                },
                keys=["snapshot_utc", "ticker", "expiry_scope", "expiry", "strike"],
            )

        if intraday_rows:
            self._bulk_replace(
                self.db.intraday_metrics,
                pd.DataFrame(intraday_rows),
                extra={"expire_at": expire_at},
                keys=["bucket_ts", "ticker", "metric_name", "strike"],
            )

        if raw_frames:
            raw_chain = pd.concat(raw_frames, ignore_index=True)
            if not raw_chain.empty:
                self._bulk_replace(
                    self.db.raw_chain,
                    raw_chain,
                    extra={"expire_at": expire_at},
                    keys=["capture_ts", "source", "ticker", "expiry", "strike", "option_type"],
                )

        if level_row:
            self.upsert_iv_rank_daily(ticker=ticker_key, trading_date=trading_date, level_row=level_row)

    def upsert_iv_rank_daily(self, *, ticker: str, trading_date: str, level_row: dict) -> None:
        self.ensure_indexes()
        ticker_key = ticker.upper()
        avg_iv = _clean_value(level_row.get("avg_iv"))
        iv_rank = self._compute_iv_rank(ticker_key, trading_date, avg_iv)
        doc = _clean_doc(
            {
                **level_row,
                "ticker": ticker_key,
                "trading_date": trading_date,
                "iv_rank": iv_rank,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.db.iv_rank_daily.update_one(
            {"ticker": ticker_key, "trading_date": trading_date},
            {"$set": doc},
            upsert=True,
        )
        self.prune_iv_rank_daily(ticker_key)

    def update_daily_tenor_iv(self, *, ticker: str, trading_date: str, tenor_atm_iv: dict[str, float]) -> None:
        if not tenor_atm_iv:
            return
        self.ensure_indexes()
        update: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        for index, (expiry, atm_iv) in enumerate(sorted(tenor_atm_iv.items())[:3]):
            update[f"atm_iv_t{index}"] = _clean_value(atm_iv)
            update[f"atm_iv_t{index}_expiry"] = expiry
        self.db.iv_rank_daily.update_one(
            {"ticker": ticker.upper(), "trading_date": trading_date},
            {"$set": _clean_doc(update), "$setOnInsert": {"ticker": ticker.upper(), "trading_date": trading_date}},
            upsert=True,
        )
        self.prune_iv_rank_daily(ticker.upper())

    def load_iv_rank_history(self, ticker: str, *, limit: int | None = None) -> list[dict]:
        self.ensure_indexes()
        row_limit = limit or self.iv_rank_sessions
        docs = list(
            self.db.iv_rank_daily.find({"ticker": ticker.upper()}, {"_id": 0})
            .sort("trading_date", -1)
            .limit(row_limit)
        )
        rows = []
        for doc in reversed(docs):
            capture_ts = doc.get("capture_ts")
            snapshot = pd.to_datetime(capture_ts, errors="coerce", utc=True)
            snapshot_utc = snapshot.isoformat() if pd.notna(snapshot) else str(capture_ts or "")
            snapshot_vn = snapshot.tz_convert(VN_TZ).isoformat() if pd.notna(snapshot) else snapshot_utc
            avg_iv = doc.get("avg_iv")
            try:
                avg_iv_pct = float(avg_iv) * 100.0 if avg_iv is not None and float(avg_iv) <= 1 else float(avg_iv)
            except (TypeError, ValueError):
                avg_iv_pct = None
            rows.append(
                _clean_doc(
                    {
                        "date": doc.get("trading_date"),
                        "ticker": ticker.upper(),
                        "snapshot_utc": snapshot_utc,
                        "snapshot_vn": snapshot_vn,
                        "spot": doc.get("spot"),
                        "atm_iv_pct": avg_iv_pct,
                        "avg_iv_pct": avg_iv_pct,
                        "iv_rank_pct": doc.get("iv_rank"),
                        "iv_source": "mongo_iv_rank_daily",
                    }
                )
            )
        return rows

    def _compute_iv_rank(self, ticker: str, trading_date: str, avg_iv: float | None) -> float | None:
        if avg_iv is None:
            return None
        rows = list(
            self.db.iv_rank_daily.find(
                {"ticker": ticker, "trading_date": {"$lt": trading_date}, "avg_iv": {"$ne": None}},
                {"avg_iv": 1, "_id": 0},
            )
            .sort("trading_date", -1)
            .limit(self.iv_rank_sessions)
        )
        values = [row.get("avg_iv") for row in rows if row.get("avg_iv") is not None]
        if not values:
            return None
        below_or_equal = sum(1 for value in values if value <= avg_iv)
        return 100.0 * below_or_equal / len(values)

    def prune_iv_rank_daily(self, ticker: str) -> None:
        keep = list(
            self.db.iv_rank_daily.find({"ticker": ticker}, {"_id": 1})
            .sort("trading_date", -1)
            .limit(self.iv_rank_sessions)
        )
        keep_ids = [item["_id"] for item in keep]
        if keep_ids:
            self.db.iv_rank_daily.delete_many({"ticker": ticker, "_id": {"$nin": keep_ids}})

    def _bulk_replace(self, collection, frame: pd.DataFrame, *, extra: dict, keys: list[str]) -> None:
        from pymongo import ReplaceOne

        operations = []
        for row in frame.to_dict(orient="records"):
            doc = _clean_doc({**row, **extra, "updated_at": datetime.now(timezone.utc)})
            filter_doc = {key: doc.get(key) for key in keys}
            operations.append(ReplaceOne(filter_doc, doc, upsert=True))
        if operations:
            collection.bulk_write(operations, ordered=False)
