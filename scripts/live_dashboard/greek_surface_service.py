from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .cache import ResponseCache
from .serialization import clean_value

from bsm import years_to_expiry
from exposure import (
    aggregate_by_expiry_strike,
    aggregate_by_strike,
    aggregate_greeks_by_expiry_strike,
    compute_flip,
    compute_greeks,
    first_or_none,
)


METRIC_COLUMNS = {
    "gex": {"net": "net_gex", "call": "call_gex", "put": "put_gex"},
    "dex": {"net": "net_dex", "call": "call_dex", "put": "put_dex"},
    "vex": {"net": "net_vex", "call": "call_vex", "put": "put_vex"},
    "chex": {"net": "net_chex", "call": "call_chex", "put": "put_chex"},
    # Raw per-contract Greek surfaces (quantdecay's "Greek Surface" panel): a
    # single OI-weighted value per (expiry, strike), not a dollar exposure —
    # so there's only one "mode", not net/call/put.
    "delta": {"raw": "avg_delta"},
    "gamma": {"raw": "avg_gamma"},
    "theta": {"raw": "avg_theta"},
    "vega": {"raw": "avg_vega"},
}

RAW_CHAIN_RATE = 0.04
RAW_CHAIN_COLUMNS = [
    "capture_ts", "source", "expiry", "strike", "option_type",
    "bid", "ask", "open_interest", "volume", "iv", "spot",
]


class GreekSurfaceService:
    def __init__(
        self,
        data_store,
        *,
        cache: ResponseCache | None = None,
        cache_ttl_seconds: float = 60.0,
        max_strikes: int = 96,
    ) -> None:
        self.data_store = data_store
        self.cache = cache or ResponseCache(max_entries=64)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_strikes = max_strikes

    def load_surface(
        self,
        *,
        ticker: str,
        trading_date: str | None,
        greek: str = "gex",
        mode: str = "net",
        strike_range: float | None = None,
        dte_max: int | None = None,
        refresh: bool = False,
    ) -> dict:
        ticker_key = ticker.upper()
        greek_key = greek.lower()
        mode_key = mode.lower()
        if greek_key not in METRIC_COLUMNS:
            raise ValueError(f"unsupported greek: {greek}")
        if mode_key not in METRIC_COLUMNS[greek_key]:
            raise ValueError(f"unsupported surface mode: {mode}")
        metric_col = METRIC_COLUMNS[greek_key][mode_key]
        date_key = trading_date or "latest"
        cache_key = (ticker_key, date_key, greek_key, mode_key, strike_range, dte_max)
        if not refresh:
            cached = self.cache.get(cache_key, self.cache_ttl_seconds)
            if cached is not None:
                return cached

        started = time.perf_counter()
        summary, rows = self._load_rows(ticker_key, trading_date)
        if rows.empty:
            raise FileNotFoundError(f"No by-strike rows found for {ticker_key} {trading_date or 'latest'}")
        if metric_col not in rows.columns:
            raise ValueError(f"{metric_col} is not available in this dataset")

        surface = self._build_surface(
            ticker_key,
            trading_date,
            greek_key,
            mode_key,
            metric_col,
            summary,
            rows,
            strike_range=strike_range,
            dte_max=dte_max,
        )
        surface["load_ms"] = round((time.perf_counter() - started) * 1000, 1)
        self.cache.set(cache_key, surface)
        return surface

    def _load_rows(self, ticker: str, trading_date: str | None) -> tuple[dict, pd.DataFrame]:
        # Prefer `raw_chain.parquet` (genuine per-expiry, per-strike quotes) so the
        # surface shows every real expiry. The `*_summary.json`/`*_by_strike.parquet`
        # tier below is pre-aggregated per-day (its "live" file even collapses ALL
        # expiries into one row) and can't produce a real strike x expiry surface.
        from_raw_chain = self._load_from_raw_chain(ticker, trading_date)
        if from_raw_chain is not None:
            return from_raw_chain
        if trading_date:
            snapshot_id = self.data_store.latest_snapshot_id_for_trading_day("day:" + trading_date, ticker)
            if snapshot_id.startswith("history:"):
                summary_path, snapshot_utc = self.data_store.parse_history_snapshot_id(snapshot_id)
                summaries = pd.read_parquet(summary_path)
                selected = summaries[
                    (summaries["ticker"].astype(str).str.upper() == ticker)
                    & (summaries["snapshot_utc"].astype(str) == str(snapshot_utc))
                ]
                if selected.empty:
                    raise FileNotFoundError("surface summary row not found")
                summary = self.data_store.summary_from_history_row(selected.iloc[-1].to_dict())
                by_strike_path = self.data_store.history_by_strike_path(summary_path)
                rows = pd.read_parquet(by_strike_path)
                rows = rows[
                    (rows["ticker"].astype(str).str.upper() == ticker)
                    & (rows["snapshot_utc"].astype(str) == str(snapshot_utc))
                ].copy()
                return summary, rows
            summary_path = self.data_store.summary_path_from_id(snapshot_id)
            return self._load_summary_file_rows(summary_path, ticker)
        return self._load_summary_file_rows(self.data_store.latest_summary_path(ticker), ticker)

    def _load_summary_file_rows(self, summary_path: Path, ticker: str) -> tuple[dict, pd.DataFrame]:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        rows_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))
        rows = pd.read_parquet(rows_path).copy()
        if "ticker" not in rows.columns:
            rows["ticker"] = ticker
        if "expiry" not in rows.columns:
            rows["expiry"] = summary.get("expiry") or ""
        if "snapshot_utc" not in rows.columns:
            rows["snapshot_utc"] = summary.get("snapshot_utc") or ""
        return summary, rows[rows["ticker"].astype(str).str.upper() == ticker].copy()

    def _raw_chain_path(self, ticker: str, trading_date: str) -> Path:
        market_root = self.data_store.root.parent / "market" / f"ticker={ticker.upper()}"
        return market_root / f"date={trading_date}" / "raw_chain.parquet"

    def _latest_raw_chain_date(self, ticker: str) -> str | None:
        market_root = self.data_store.root.parent / "market" / f"ticker={ticker.upper()}"
        if not market_root.exists():
            return None
        candidates = sorted(
            (path for path in market_root.glob("date=*") if (path / "raw_chain.parquet").exists()),
            reverse=True,
        )
        if not candidates:
            return None
        return candidates[0].name.split("=", 1)[1]

    def _load_from_raw_chain(self, ticker: str, trading_date: str | None) -> tuple[dict, pd.DataFrame] | None:
        resolved_date = trading_date or self._latest_raw_chain_date(ticker)
        if not resolved_date:
            return None
        raw_path = self._raw_chain_path(ticker, resolved_date)
        if not raw_path.exists():
            return None
        try:
            raw = pd.read_parquet(raw_path, columns=RAW_CHAIN_COLUMNS)
        except Exception:
            return None
        raw["_capture_ts"] = pd.to_datetime(raw["capture_ts"], errors="coerce", utc=True)
        raw = raw[raw["_capture_ts"].notna()].copy()
        if raw.empty:
            return None

        # Pick the latest capture_ts that actually spans multiple expiries (a real
        # chain snapshot), falling back to the plain latest capture_ts otherwise —
        # mirrors `skew_tenors_payload_for_summary` in live_server.py.
        expiry_counts = raw.groupby("capture_ts")["expiry"].nunique()
        multi_tenor_ts = expiry_counts[expiry_counts >= 2]
        capture_ts = multi_tenor_ts.index.max() if not multi_tenor_ts.empty else raw["capture_ts"].max()
        selected = raw[raw["capture_ts"].astype(str) == str(capture_ts)].drop(columns=["_capture_ts"])
        if selected.empty:
            return None

        # Dedupe overlapping sources (yahoo preferred over cboe) per contract, same
        # rule used for the tenor curves in render_gex_interactive.py.
        selected = selected.copy()
        selected["source_rank"] = np.where(selected["source"] == "yahoo", 0, 1)
        selected = selected.sort_values(["expiry", "strike", "option_type", "source_rank"])
        selected = selected.drop_duplicates(subset=["expiry", "strike", "option_type"], keep="first")
        selected = selected.drop(columns=["source_rank", "source"])

        selected = selected.rename(columns={"open_interest": "openInterest", "iv": "impliedVolatility"})
        selected["strike"] = pd.to_numeric(selected["strike"], errors="coerce")
        selected["openInterest"] = pd.to_numeric(selected["openInterest"], errors="coerce").fillna(0.0)
        selected["volume"] = pd.to_numeric(selected["volume"], errors="coerce").fillna(0.0)
        selected = selected.dropna(subset=["strike", "expiry", "option_type"])
        if selected.empty:
            return None

        spot_values = pd.to_numeric(selected["spot"], errors="coerce").dropna()
        spot = float(spot_values.iloc[-1]) if not spot_values.empty else None
        if spot is None:
            return None

        snapshot_utc = pd.Timestamp(capture_ts).tz_convert("UTC")
        effective_day = snapshot_utc.tz_convert(self.data_store.ny_tz).date()
        expiries = sorted(selected["expiry"].dropna().astype(str).unique())
        years_by_expiry = {}
        for expiry in expiries:
            _, years = years_to_expiry(expiry, effective_day)
            years_by_expiry[expiry] = years

        chain = compute_greeks(selected, spot, years_by_expiry, RAW_CHAIN_RATE)
        by_expiry_strike = aggregate_by_expiry_strike(chain)
        raw_greeks = aggregate_greeks_by_expiry_strike(chain)
        by_expiry_strike = by_expiry_strike.merge(
            raw_greeks.drop(columns=["total_oi"]), on=["expiry", "strike"], how="left"
        )
        by_expiry_strike["ticker"] = ticker
        by_expiry_strike["snapshot_utc"] = snapshot_utc.isoformat()

        summary = {
            "ticker": ticker,
            "spot": spot,
            "snapshot_utc": snapshot_utc.isoformat(),
            "requested_snapshot_date": resolved_date,
            "effective_snapshot_date": effective_day.isoformat(),
        }
        summary.update(self._pooled_levels(chain, spot))
        return summary, by_expiry_strike

    def _pooled_levels(self, chain: pd.DataFrame, spot: float) -> dict:
        """Wall/flip reference levels pooled across every expiry in `chain`, same
        convention the pre-existing 'ALL' snapshot used for these fields."""
        by_strike = aggregate_by_strike(chain)
        calls = chain[chain["option_type"] == "call"]
        puts = chain[chain["option_type"] == "put"]
        call_candidates = calls[calls["strike"] > spot].sort_values("gex", ascending=False)
        put_candidates = puts[puts["strike"] < spot].sort_values("gex", ascending=True)
        gamma_positive = by_strike[by_strike["net_gex"] > 0].sort_values("net_gex", ascending=False)
        return {
            "call_resistance": first_or_none(call_candidates["strike"]),
            "put_support": first_or_none(put_candidates["strike"]),
            "gamma_wall_abs": first_or_none(by_strike.sort_values("abs_net_gex", ascending=False)["strike"]),
            "gamma_wall_positive": first_or_none(gamma_positive["strike"]),
            "gamma_flip": compute_flip(by_strike, "net_gex", spot),
            "delta_flip": compute_flip(by_strike, "net_dex", spot),
            "vanna_wall_abs": first_or_none(by_strike.sort_values("abs_net_vex", ascending=False)["strike"]),
            "charm_wall_abs": first_or_none(by_strike.sort_values("abs_net_chex", ascending=False)["strike"]),
        }

    def _build_surface(
        self,
        ticker: str,
        trading_date: str | None,
        greek: str,
        mode: str,
        metric_col: str,
        summary: dict,
        rows: pd.DataFrame,
        *,
        strike_range: float | None,
        dte_max: int | None,
    ) -> dict:
        data = rows.copy()
        data["strike"] = pd.to_numeric(data["strike"], errors="coerce")
        data[metric_col] = pd.to_numeric(data[metric_col], errors="coerce")
        data = data.replace([math.inf, -math.inf], pd.NA).dropna(subset=["strike", metric_col])
        if data.empty:
            raise FileNotFoundError("surface rows are empty after cleaning")
        if "expiry" not in data.columns:
            data["expiry"] = summary.get("expiry") or ""
        data["expiry"] = data["expiry"].astype(str)
        spot = self._spot(summary, data)
        if strike_range is not None and spot is not None:
            data = data[(data["strike"] >= spot - strike_range) & (data["strike"] <= spot + strike_range)]
        if data.empty:
            raise FileNotFoundError("no strikes inside selected range")
        expiries = sorted(data["expiry"].dropna().astype(str).unique(), key=self._expiry_sort_key)
        if dte_max is not None:
            expiries = [exp for exp in expiries if self._dte(exp, trading_date or summary.get("effective_snapshot_date")) <= dte_max]
            data = data[data["expiry"].isin(expiries)]
        if not expiries or data.empty:
            raise FileNotFoundError("no expiries inside selected DTE range")
        strikes = sorted(float(v) for v in data["strike"].dropna().unique())
        strikes = self._trim_strikes(strikes, spot)
        data = data[data["strike"].isin(strikes)]
        grouped = data.groupby(["expiry", "strike"], as_index=False)[metric_col].sum()
        pivot = grouped.pivot(index="expiry", columns="strike", values=metric_col).reindex(index=expiries, columns=strikes)
        raw_values = pivot.fillna(0.0).astype(float).values.tolist()
        max_abs = max((abs(v) for row in raw_values for v in row), default=0.0)
        normalized = [[(v / max_abs if max_abs else 0.0) for v in row] for row in raw_values]
        top_cells = self._top_cells(expiries, strikes, raw_values)
        levels = self._levels(summary)
        return {
            "type": "greek_surface",
            "ticker": ticker,
            "date": trading_date or summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date"),
            "snapshot_utc": clean_value(summary.get("snapshot_utc")),
            "greek": greek,
            "mode": mode,
            "metric": metric_col,
            "spot": clean_value(spot),
            "strikes": [clean_value(v) for v in strikes],
            "expiries": expiries,
            "dte": [self._dte(exp, trading_date or summary.get("effective_snapshot_date")) for exp in expiries],
            "values": [[clean_value(v) for v in row] for row in normalized],
            "raw_values": [[clean_value(v) for v in row] for row in raw_values],
            "rawMaxAbs": clean_value(max_abs),
            "top_cells": top_cells,
            "levels": levels,
            "source": "history" if trading_date else "latest",
        }

    def _trim_strikes(self, strikes: list[float], spot: float | None) -> list[float]:
        if len(strikes) <= self.max_strikes:
            return strikes
        center = spot if spot is not None else strikes[len(strikes) // 2]
        ranked = sorted(strikes, key=lambda value: (abs(value - center), value))[: self.max_strikes]
        return sorted(ranked)

    def _top_cells(self, expiries: list[str], strikes: list[float], rows: list[list[float]], limit: int = 12) -> list[dict]:
        cells = []
        for row_idx, expiry in enumerate(expiries):
            for col_idx, strike in enumerate(strikes):
                value = rows[row_idx][col_idx]
                if not value:
                    continue
                cells.append({"expiry": expiry, "strike": strike, "value": value, "abs_value": abs(value)})
        cells.sort(key=lambda cell: cell["abs_value"], reverse=True)
        return [
            {"expiry": cell["expiry"], "strike": clean_value(cell["strike"]), "value": clean_value(cell["value"])}
            for cell in cells[:limit]
        ]

    def _levels(self, summary: dict) -> dict:
        keys = (
            "call_resistance",
            "put_support",
            "gamma_wall_abs",
            "gamma_wall_positive",
            "gamma_flip",
            "delta_flip",
            "vanna_wall_abs",
            "charm_wall_abs",
        )
        return {key: clean_value(summary.get(key)) for key in keys if summary.get(key) is not None}

    def _spot(self, summary: dict, rows: pd.DataFrame) -> float | None:
        try:
            spot = float(summary.get("spot"))
            if math.isfinite(spot):
                return spot
        except (TypeError, ValueError):
            pass
        strikes = pd.to_numeric(rows.get("strike"), errors="coerce").dropna().sort_values()
        return float(strikes.median()) if not strikes.empty else None

    def _dte(self, expiry: str, trading_date: str | None) -> int | None:
        try:
            if not trading_date:
                return None
            return max(0, int((pd.Timestamp(expiry).date() - pd.Timestamp(trading_date).date()).days))
        except Exception:
            return None

    def _expiry_sort_key(self, expiry: str) -> tuple[int, str]:
        try:
            return (0, pd.Timestamp(expiry).date().isoformat())
        except Exception:
            return (1, expiry)
