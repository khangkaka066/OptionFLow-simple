#!/usr/bin/env python3
"""Local realtime dashboard server for intraday Volatility Flow.

The browser does not reload. A background worker collects snapshots on a
cadence, while the page polls `/api/state` and updates Plotly charts in place.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from exposure import nearest_atm_iv
from live_dashboard.data_store import DataStore
from live_dashboard.http_server import configure_handler
from live_dashboard.intraday_file_cache import IntradayFileCache
from live_dashboard.intraday_service import IntradayService
from live_dashboard.market_data import (
    alpaca_candles_collector,
    apply_futures_basis,
    candles_for_session,
    fetch_day_high_low,
)
from live_dashboard.serialization import clean_records, clean_value
from live_dashboard.snapshot_file_cache import SnapshotFileCache
from live_dashboard.snapshot_service import SnapshotService
from live_dashboard.state import LiveState
from live_dashboard.time_utils import (
    DEFAULT_COLLECT_START_OFFSET_MIN,
    NY_TZ,
    VN_TZ,
    collection_start_utc,
    market_session_utc,
    session_for_trading_date,
    set_collect_start_offset_min,
)
from render_gex_interactive import build_tenor_curves, load_multi_tenor_skew
from sources import yahoo

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
RUN_DASHBOARD_SCRIPT = SCRIPT_ROOT / "run_gex_dashboard.py"
DATA_ROOT = PROJECT_ROOT / "data" / "options"
DATA_STORE = DataStore(DATA_ROOT, ny_tz=NY_TZ, vn_tz=VN_TZ)
IV_RANK_HISTORY_PATH = DATA_ROOT / "iv_rank_history.csv"
INTRADAY_CACHE_ROOT = PROJECT_ROOT / "data" / "cache" / "intraday"
SNAPSHOT_CACHE_ROOT = PROJECT_ROOT / "data" / "cache" / "snapshot"


def skew_tenors_payload(ticker: str, spot: float, effective_day: str) -> list[dict]:
    """Multi-expiry skew curves for the live Volatility Skew panel, mirroring
    render_gex_interactive.build_volatility_skew_chart's multi-tenor branch."""
    try:
        raw, capture_ts = load_multi_tenor_skew(DATA_ROOT, ticker)
        if raw.empty:
            return []
        # DTE must be measured from when this multi-tenor snapshot was actually
        # captured, not from the single-expiry poll's effective_snapshot_date -
        # that date reflects Yahoo's last-trade staleness (e.g. pinned to Friday
        # over a weekend/pre-open) while the multi-tenor expiries are always
        # real calendar dates, so using the stale day here silently zeroed out
        # every tenor.
        dte_reference_day = str(capture_ts)[:10] if capture_ts else effective_day
        tenors = build_tenor_curves(raw, spot, dte_reference_day)
    except Exception:
        return []
    payload = []
    for tenor in tenors:
        payload.append(
            {
                "expiry": tenor["expiry"],
                "dte": tenor["dte"],
                "atm_iv": clean_value(tenor["atm_iv"]),
                "color": tenor["color"],
                "call": {
                    "strike": [clean_value(v) for v in tenor["call"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["call"]["iv"].tolist()],
                },
                "put": {
                    "strike": [clean_value(v) for v in tenor["put"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["put"]["iv"].tolist()],
                },
                "iv": {
                    "strike": [clean_value(v) for v in tenor["iv"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["iv"]["iv"].tolist()],
                },
            }
        )
    return payload


def skew_tenors_payload_for_summary(ticker: str, summary: dict) -> list[dict]:
    """Return multi-tenor skew curves aligned to a historical snapshot day.

    The live helper intentionally reads the latest multi-expiry capture. For
    replay/EOD panels we need the latest multi-expiry capture from the same NY
    trading day and not newer than the selected EOD snapshot when possible.
    """
    try:
        spot = float(summary.get("spot") or 0.0)
    except (TypeError, ValueError):
        return []
    if not np.isfinite(spot) or spot <= 0:
        return []
    snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
    if pd.isna(snapshot):
        return skew_tenors_payload(
            ticker,
            spot,
            summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or "",
        )
    trading_day = snapshot.tz_convert(NY_TZ).date().isoformat()
    raw_path = DATA_ROOT.parent / "market" / f"ticker={ticker.upper()}" / f"date={trading_day}" / "raw_chain.parquet"
    if not raw_path.exists():
        return skew_tenors_payload(
            ticker,
            spot,
            summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or trading_day,
        )
    try:
        raw = pd.read_parquet(
            raw_path,
            columns=["capture_ts", "source", "expiry", "strike", "option_type", "iv", "open_interest", "volume"],
        )
    except Exception:
        return []
    if raw.empty or "capture_ts" not in raw or "expiry" not in raw:
        return []
    raw["_capture_ts"] = pd.to_datetime(raw["capture_ts"], errors="coerce", utc=True)
    raw = raw[raw["_capture_ts"].notna()].copy()
    raw = raw[raw["_capture_ts"] <= snapshot]
    if raw.empty:
        return []
    expiry_counts = raw.groupby("capture_ts")["expiry"].nunique()
    multi_tenor_ts = expiry_counts[expiry_counts >= 2]
    capture_ts = multi_tenor_ts.index.max() if not multi_tenor_ts.empty else raw["capture_ts"].max()
    selected = raw[raw["capture_ts"].astype(str) == str(capture_ts)].drop(columns=["_capture_ts"], errors="ignore")
    if selected.empty:
        return []
    try:
        tenors = build_tenor_curves(selected, spot, str(capture_ts)[:10])
    except Exception:
        return []
    payload = []
    for tenor in tenors:
        payload.append(
            {
                "expiry": tenor["expiry"],
                "dte": tenor["dte"],
                "atm_iv": clean_value(tenor["atm_iv"]),
                "color": tenor["color"],
                "call": {
                    "strike": [clean_value(v) for v in tenor["call"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["call"]["iv"].tolist()],
                },
                "put": {
                    "strike": [clean_value(v) for v in tenor["put"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["put"]["iv"].tolist()],
                },
                "iv": {
                    "strike": [clean_value(v) for v in tenor["iv"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["iv"]["iv"].tolist()],
                },
            }
        )
    return payload


def atm_iv_from_yahoo_raw(day_dir: Path, ticker: str, spot: float) -> float | None:
    if not np.isfinite(spot):
        return None
    raw_files = sorted((day_dir / "raw").glob(f"{ticker.upper()}_*_yahoo.json"))
    if not raw_files:
        return None
    try:
        data = json.loads(raw_files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None
    candidates: list[tuple[float, float]] = []
    for side in ("calls", "puts"):
        for row in data.get(side, []):
            try:
                strike = float(row.get("strike"))
                iv = float(row.get("impliedVolatility"))
            except (TypeError, ValueError):
                continue
            if np.isfinite(strike) and np.isfinite(iv) and 0.01 <= iv <= 3.0:
                candidates.append((abs(strike - spot), iv))
    if not candidates:
        return None
    nearest_dist = min(dist for dist, _iv in candidates)
    nearest = [iv for dist, iv in candidates if abs(dist - nearest_dist) < 1e-9]
    return float(np.mean(nearest)) if nearest else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local realtime QQQ Volatility Flow dashboard.")
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument(
        "--secondary-ticker",
        default="NDX",
        help="Second ticker whose levels are shown in the Levels Export panel's second row. "
        "Empty string disables the second row.",
    )
    parser.add_argument(
        "--secondary-futures-ticker",
        default="NQ1!",
        help="Futures ticker whose live price is used to compute the basis added to the "
        "secondary ticker's exported levels (e.g. NQ1! for NDX), correcting for the cash/"
        "futures spread when those levels are pasted onto a futures chart. "
        "Empty string disables basis adjustment.",
    )
    parser.add_argument("--expiry", default=None)
    parser.add_argument("--duration-minutes", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument(
        "--collect-start-offset-min",
        type=float,
        default=DEFAULT_COLLECT_START_OFFSET_MIN,
        help="Minutes before NY market open (9:30 ET) that Heat Tracker starts collecting. "
        "Anchored to NY time so it self-adjusts across US DST changes.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--window", type=float, default=14)
    parser.add_argument("--rate", type=float, default=0.04)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--no-github-pull",
        action="store_true",
        help="Skip the startup git pull that syncs GitHub Actions history back to this machine.",
    )
    return parser.parse_args()


def pull_github_updates_on_startup(enabled: bool = True) -> None:
    if not enabled:
        print("GitHub sync skipped (--no-github-pull).", flush=True)
        return
    if not (PROJECT_ROOT / ".git").exists():
        print("GitHub sync skipped: this folder is not a git repo.", flush=True)
        return
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except Exception as exc:
        print(f"GitHub sync warning: could not run git pull ({exc}).", flush=True)
        return
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode == 0:
        first_line = output.splitlines()[0] if output else "Already up to date."
        print(f"GitHub sync ok: {first_line}", flush=True)
    else:
        print("GitHub sync warning: git pull --ff-only failed; continuing with local data.", flush=True)
        if output:
            print(output, flush=True)


def latest_summary_path(ticker: str) -> Path:
    return DATA_STORE.latest_summary_path(ticker)


def history_summary_paths(ticker: str) -> list[Path]:
    return DATA_STORE.history_summary_paths(ticker)


def parse_history_json(value):
    return DATA_STORE.parse_history_json(value)


def summary_from_history_row(row: dict) -> dict:
    return DATA_STORE.summary_from_history_row(row)


def history_by_strike_path(summary_history_path: Path) -> Path:
    return DATA_STORE.history_by_strike_path(summary_history_path)


def history_snapshot_id(summary_history_path: Path, snapshot_utc: str) -> str:
    return DATA_STORE.history_snapshot_id(summary_history_path, snapshot_utc)


def parse_history_snapshot_id(snapshot_id: str) -> tuple[Path, str]:
    return DATA_STORE.parse_history_snapshot_id(snapshot_id)


def latest_history_snapshot(ticker: str) -> tuple[Path, dict] | None:
    return DATA_STORE.latest_history_snapshot(ticker)


def volume_totals(rows: pd.DataFrame | list[dict]) -> tuple[float, float]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return 0.0, 0.0
    call_volume = pd.to_numeric(frame.get("call_volume", 0), errors="coerce").fillna(0).sum()
    put_volume = pd.to_numeric(frame.get("put_volume", 0), errors="coerce").fillna(0).sum()
    return float(call_volume), float(put_volume)


def chain_payload(rows: list[dict]) -> list[dict]:
    """Compact per-strike call/put IV+volume for client-side moneyness filtering."""
    out: list[dict] = []
    for row in rows:
        try:
            strike = float(row.get("strike"))
        except (TypeError, ValueError):
            continue
        if not np.isfinite(strike):
            continue

        def pct(key: str) -> float | None:
            try:
                val = float(row.get(key))
            except (TypeError, ValueError):
                return None
            return val * 100 if np.isfinite(val) else None

        def vol(key: str) -> float:
            try:
                val = float(row.get(key))
            except (TypeError, ValueError):
                return 0.0
            return val if np.isfinite(val) else 0.0

        def price(key: str) -> float | None:
            try:
                val = float(row.get(key))
            except (TypeError, ValueError):
                return None
            return val if np.isfinite(val) and val > 0 else None

        out.append({
            "k": strike, "ci": pct("call_iv"), "pi": pct("put_iv"),
            "cv": vol("call_volume"), "pv": vol("put_volume"),
            "cm": price("call_mid"), "pm": price("put_mid"),
        })
    return out


def build_iv_rank_history_rows(ticker: str) -> list[dict]:
    rows_by_day: dict[str, dict] = {}
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_summary.json")):
        if len(path.stem.split("_")) != 3:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot):
            continue
        spot = clean_value(summary.get("spot"))
        iv_value = None
        iv_source = None
        by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
        if by_strike_path.exists() and spot is not None:
            try:
                by_strike = pd.read_parquet(by_strike_path)
                atm_pct = nearest_atm_iv(by_strike, float(spot))
                if atm_pct is not None:
                    iv_value = atm_pct / 100
                    iv_source = "by_strike_atm"
            except Exception:
                pass
        if iv_value is None and spot is not None:
            iv_value = atm_iv_from_yahoo_raw(path.parent, ticker, float(spot))
            if iv_value is not None:
                iv_source = "raw_yahoo_atm"
        if iv_value is None:
            summary_iv = summary.get("avg_iv")
            if summary_iv is not None and np.isfinite(float(summary_iv)) and float(summary_iv) <= 0.8:
                iv_value = float(summary_iv)
                iv_source = "summary_avg_iv"
        avg_iv_pct = clean_value(iv_value * 100 if iv_value is not None else None)
        if spot is None or avg_iv_pct is None:
            continue
        day_key = snapshot.date().isoformat()
        row = {
            "date": day_key,
            "ticker": ticker.upper(),
            "snapshot_utc": snapshot.isoformat(),
            "snapshot_vn": snapshot.tz_convert("Asia/Ho_Chi_Minh").isoformat(),
            "spot": spot,
            "atm_iv_pct": avg_iv_pct,
            "avg_iv_pct": avg_iv_pct,
            "iv_source": iv_source,
        }
        previous = rows_by_day.get(day_key)
        if previous is None or row["snapshot_utc"] > previous["snapshot_utc"]:
            rows_by_day[day_key] = row
    rows = sorted(rows_by_day.values(), key=lambda row: row["snapshot_utc"])
    iv_values: list[float] = []
    for row in rows:
        iv_values.append(float(row["avg_iv_pct"]))
        window = iv_values[-60:]
        # A min-max rank needs at least 2 points to define a range at all.
        # It's noisy with few sessions (matches quantdecay's own behavior,
        # which doesn't gate on a minimum either) but still meaningful.
        if len(window) < 2:
            row["iv_rank_pct"] = None
            continue
        low = min(window)
        high = max(window)
        row["iv_rank_pct"] = ((iv_values[-1] - low) / (high - low) * 100.0) if high > low else None
    return clean_records(rows)


def write_iv_rank_history_csv(rows: list[dict]) -> None:
    if not rows:
        return
    columns = ["date", "ticker", "snapshot_utc", "snapshot_vn", "spot", "atm_iv_pct", "iv_rank_pct", "iv_source"]
    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df:
            df[col] = None
    IV_RANK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.loc[:, columns].to_csv(IV_RANK_HISTORY_PATH, index=False)


def load_history(ticker: str) -> list[dict]:
    rows = build_iv_rank_history_rows(ticker)
    write_iv_rank_history_csv(rows)
    return rows[-60:]


def seed_session_data(ticker: str, session: dict, window: float) -> tuple[list[dict], list[dict]]:
    """Load already-collected snapshots for today's configured collection window
    off disk, so Volatility Flow and the GEX ribbon show the session so far instead of
    resetting empty every time the server restarts (state normally only lives in RAM).
    Files are grouped by New York trading date, not their local output folder,
    because an overnight VN session can span more than one local folder."""
    open_ts = pd.Timestamp(session["collection_start_utc"])
    close_ts = pd.Timestamp(session["market_close_utc"])
    points: list[dict] = []
    ribbon: list[dict] = []
    seen_times: set[str] = set()
    for summary_history_path in history_summary_paths(ticker):
        try:
            summaries = pd.read_parquet(summary_history_path)
        except Exception:
            continue
        if summaries.empty or "ticker" not in summaries or "snapshot_utc" not in summaries:
            continue
        by_strike_path = history_by_strike_path(summary_history_path)
        if not by_strike_path.exists():
            continue
        try:
            by_strike_rows = pd.read_parquet(by_strike_path)
        except Exception:
            continue
        summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
        summaries = summaries[
            summaries["_snapshot_ts"].notna()
            & (summaries["_snapshot_ts"] >= open_ts)
            & (summaries["_snapshot_ts"] <= close_ts)
            & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
        ].sort_values("snapshot_utc")
        for row in summaries.to_dict(orient="records"):
            summary = summary_from_history_row(row)
            time_key = str(summary.get("snapshot_utc") or "")
            if not time_key or time_key in seen_times:
                continue
            try:
                records, gex_snapshot = rows_for_history_snapshot(
                    summary_history_path, summary, window, source_rows=by_strike_rows
                )
            except Exception:
                continue
            seen_times.add(time_key)
            call_volume, put_volume = volume_totals(records)
            points.append({
                "time": time_key,
                "atm_iv": clean_value(nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(float(summary["spot"])),
                "call_volume": clean_value(call_volume),
                "put_volume": clean_value(put_volume),
                "chain": chain_payload(records),
                "expiry": clean_value(summary.get("expiry")),
            })
            ribbon.append(gex_snapshot)
    if points:
        points.sort(key=lambda point: point["time"])
        ribbon.sort(key=lambda snap: snap["time"])
        return points, ribbon

    points: list[dict] = []
    ribbon: list[dict] = []
    seen_times: set[str] = set()
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:  # skip the canonical "latest" file (3 parts)
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot) or snapshot < open_ts or snapshot > close_ts:
            continue
        if snapshot.tz_convert(NY_TZ).date().isoformat() != session["trading_date"]:
            continue
        time_key = summary.get("snapshot_utc")
        if time_key in seen_times:
            continue
        seen_times.add(time_key)
        spot = float(summary["spot"])
        atm_iv = None
        call_volume = 0.0
        put_volume = 0.0
        chain: list[dict] = []
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            call_volume, put_volume = volume_totals(chart_rows)
            chain = chain_payload(chart_rows.to_dict(orient="records"))
            ribbon.append(gex_snapshot_from_chart_rows(time_key, chart_rows))
        except Exception:
            pass
        points.append({
            "time": time_key,
            "atm_iv": clean_value(atm_iv),
            "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
            "spot": clean_value(spot),
            "call_volume": clean_value(call_volume),
            "put_volume": clean_value(put_volume),
            "chain": chain,
            "expiry": clean_value(summary.get("expiry")),
        })
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def seed_prev_day_eod_summary(ticker: str, today_trading_date: str) -> dict | None:
    """Return the last recorded snapshot strictly before today's trading
    date - the previous completed session's actual closing chain.

    Levels Export is meant to always show a true EOD reference: on
    2026-08-27 it shows the 2026-08-26 close, all day, regardless of time -
    never an early poll of *today* recomputed with Yahoo's live, moving
    premarket spot. Falls back through JSON summaries on disk if no
    Parquet history exists yet for the ticker.
    """
    latest_history = latest_history_snapshot(ticker)
    if latest_history is not None:
        summary_history_path, _summary = latest_history
        summaries = pd.read_parquet(summary_history_path)
        summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
        summaries = summaries[summaries["_snapshot_ts"].notna()].copy()
        summaries["_ny_date"] = summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str)
        summaries = summaries[summaries["_ny_date"] < today_trading_date].sort_values("snapshot_utc")
        if not summaries.empty:
            return summary_from_history_row(summaries.iloc[-1].to_dict())
    candidates: list[tuple[pd.Timestamp, dict]] = []
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot):
            continue
        if snapshot.tz_convert(NY_TZ).date().isoformat() >= today_trading_date:
            continue
        candidates.append((snapshot, summary))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def seed_locked_snapshot(
    ticker: str, session: dict, lock_ts: pd.Timestamp, window: float
) -> tuple[dict | None, list[dict], list[dict]]:
    """Return (summary, by_strike rows, iv-rank history) for the last snapshot
    of today strictly before lock_ts.

    DEX/GEX/VEX/CHEX, OI by Strike, OIxIV by Strike and IV Rank must show
    true EOD data until 09:00 NY, and Volatility Skew until 09:30 NY - but
    Yahoo returns a live `preMarketPrice` for any poll made before the open,
    so a naive "latest poll" would already recompute exposure/skew off a
    moving premarket spot before the intended cutoff. Seeding from the last
    pre-cutoff snapshot on disk avoids that leak. Returns (None, [], []) if
    no such snapshot exists yet today (e.g. server started before any poll
    has landed) - the caller keeps its empty/placeholder state in that case.
    """
    latest_history = latest_history_snapshot(ticker)
    if latest_history is None:
        return None, [], []
    summary_history_path, _summary = latest_history
    try:
        summaries = pd.read_parquet(summary_history_path)
    except Exception:
        return None, [], []
    summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
    summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
    summaries = summaries[
        summaries["_snapshot_ts"].notna()
        & (summaries["_snapshot_ts"] < lock_ts)
        & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
    ].sort_values("snapshot_utc")
    if summaries.empty:
        return None, [], []
    summary = summary_from_history_row(summaries.iloc[-1].to_dict())
    try:
        rows, _gex_snapshot = rows_for_history_snapshot(summary_history_path, summary, window)
    except Exception:
        rows = []
    return summary, rows, load_history(ticker)


def gex_snapshot_from_chart_rows(time_key: str, chart_rows: pd.DataFrame) -> dict:
    keep_cols = [
        "strike",
        "net_gex",
        "net_dex",
        "net_vex",
        "net_chex",
    ]
    rows = chart_rows.sort_values("strike")[[col for col in keep_cols if col in chart_rows.columns]].copy()
    records = rows.replace([np.inf, -np.inf], np.nan).where(pd.notna(rows), None).to_dict(orient="records")
    return {"time": time_key, "rows": clean_records(records)}


def session_from_summary(summary: dict) -> dict:
    snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
    if pd.isna(snapshot):
        session = market_session_utc()
    else:
        trading_date = snapshot.tz_convert(NY_TZ).date()
        open_ny = pd.Timestamp.combine(trading_date, dt_time(9, 30)).tz_localize(NY_TZ)
        close_ny = pd.Timestamp.combine(trading_date, dt_time(16, 0)).tz_localize(NY_TZ)
        session = {
            "trading_date": trading_date.isoformat(),
            "market_open_utc": open_ny.tz_convert("UTC").isoformat(),
            "market_close_utc": close_ny.tz_convert("UTC").isoformat(),
        }
    session["collection_start_utc"] = collection_start_utc(session["market_open_utc"])
    return session


def snapshot_id_for_path(path: Path) -> str:
    return DATA_STORE.snapshot_id_for_path(path)


def summary_path_from_id(snapshot_id: str) -> Path:
    return DATA_STORE.summary_path_from_id(snapshot_id)


def snapshot_label(path: Path, summary: dict) -> str:
    return DATA_STORE.snapshot_label(path, summary)


def list_history_choices(ticker: str) -> list[dict]:
    return DATA_STORE.list_history_choices(ticker)


def list_trading_days(ticker: str) -> list[dict]:
    return DATA_STORE.list_trading_days(ticker)


def latest_snapshot_id_for_trading_day(day_id: str, ticker: str) -> str:
    return DATA_STORE.latest_snapshot_id_for_trading_day(day_id, ticker)


def rows_for_history_snapshot(
    summary_history_path: Path,
    summary: dict,
    window: float,
    source_rows: pd.DataFrame | None = None,
) -> tuple[list[dict], dict]:
    if source_rows is None:
        by_strike_path = history_by_strike_path(summary_history_path)
        if not by_strike_path.exists():
            raise FileNotFoundError(f"Missing history by-strike store: {by_strike_path}")
        rows = pd.read_parquet(by_strike_path)
    else:
        rows = source_rows
    snapshot_utc = summary.get("snapshot_utc")
    rows = rows[
        (rows["snapshot_utc"].astype(str) == str(snapshot_utc))
        & (rows["ticker"].astype(str).str.upper() == str(summary.get("ticker", "")).upper())
        & (rows["expiry"].astype(str) == str(summary.get("expiry")))
    ].copy()
    spot = float(summary["spot"])
    chart_rows = rows[(rows["strike"] >= spot - window) & (rows["strike"] <= spot + window)].copy()
    if chart_rows.empty:
        chart_rows = rows.sort_values("abs_net_gex", ascending=False).head(40)
    keep_cols = [
        "strike",
        "net_gex",
        "call_gex",
        "put_gex",
        "net_dex",
        "call_dex",
        "put_dex",
        "net_vex",
        "call_vex",
        "put_vex",
        "net_chex",
        "call_chex",
        "put_chex",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "iv",
        "call_iv",
        "put_iv",
        "call_mid",
        "put_mid",
    ]
    for col in keep_cols:
        if col not in chart_rows.columns:
            chart_rows[col] = np.nan
    chart_rows = chart_rows.sort_values("strike")[[col for col in keep_cols if col in chart_rows.columns]].copy()
    chart_rows["iv_pct"] = chart_rows["iv"] * 100
    chart_rows["call_iv_pct"] = chart_rows["call_iv"] * 100
    chart_rows["put_iv_pct"] = chart_rows["put_iv"] * 100
    records = chart_rows.replace([np.inf, -np.inf], np.nan).where(pd.notna(chart_rows), None).to_dict(orient="records")
    return clean_records(records), gex_snapshot_from_chart_rows(snapshot_utc, chart_rows)


def chart_payload_from_history(summary_history_path: Path, snapshot_utc: str, ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
    rows = pd.read_parquet(summary_history_path)
    rows = rows[
        (rows["snapshot_utc"].astype(str) == str(snapshot_utc))
        & (rows["ticker"].astype(str).str.upper() == ticker.upper())
    ]
    if rows.empty:
        raise FileNotFoundError("history snapshot row not found")
    summary = summary_from_history_row(rows.iloc[-1].to_dict())
    point = {
        "time": summary.get("snapshot_utc"),
        "atm_iv": None,
        "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
        "spot": clean_value(float(summary["spot"])),
    }
    records, gex_snapshot = rows_for_history_snapshot(summary_history_path, summary, window)
    call_volume, put_volume = volume_totals(records)
    point["atm_iv"] = nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))
    point["call_volume"] = clean_value(call_volume)
    point["put_volume"] = clean_value(put_volume)
    point["chain"] = chain_payload(records)
    point["expiry"] = clean_value(summary.get("expiry"))
    return summary, point, records, load_history(ticker), gex_snapshot


def chart_payload_from_summary_path(
    summary_path: Path, ticker: str, window: float
) -> tuple[dict, dict, list[dict], list[dict], dict]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    by_strike_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))
    by_strike = pd.read_parquet(by_strike_path)
    spot = float(summary["spot"])
    atm_iv = nearest_atm_iv(by_strike, spot)
    time_key = summary.get("snapshot_utc")
    point = {
        "time": time_key,
        "atm_iv": clean_value(atm_iv),
        "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
        "spot": clean_value(spot),
    }
    chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)].copy()
    if chart_rows.empty:
        chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
    keep_cols = [
        "strike",
        "net_gex",
        "call_gex",
        "put_gex",
        "net_dex",
        "call_dex",
        "put_dex",
        "net_vex",
        "call_vex",
        "put_vex",
        "net_chex",
        "call_chex",
        "put_chex",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "iv",
        "call_iv",
        "put_iv",
        "call_mid",
        "put_mid",
    ]
    for col in keep_cols:
        if col not in chart_rows.columns:
            chart_rows[col] = np.nan
    rows = chart_rows.sort_values("strike")[keep_cols].copy()
    rows["iv_pct"] = rows["iv"] * 100
    rows["call_iv_pct"] = rows["call_iv"] * 100
    rows["put_iv_pct"] = rows["put_iv"] * 100
    call_volume, put_volume = volume_totals(rows)
    point["call_volume"] = clean_value(call_volume)
    point["put_volume"] = clean_value(put_volume)
    records = rows.replace([np.inf, -np.inf], np.nan).where(pd.notna(rows), None).to_dict(orient="records")
    point["chain"] = chain_payload(records)
    point["expiry"] = clean_value(summary.get("expiry"))
    gex_snapshot = gex_snapshot_from_chart_rows(time_key, chart_rows)
    return summary, point, clean_records(records), load_history(ticker), gex_snapshot


def day_series_from_summary_path(summary_path: Path, ticker: str, window: float) -> tuple[list[dict], list[dict]]:
    points: list[dict] = []
    ribbon: list[dict] = []
    seen_times: set[str] = set()
    selected_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    selected_session = session_from_summary(selected_summary)
    for path in sorted(summary_path.parent.glob(f"{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot) or snapshot.tz_convert(NY_TZ).date().isoformat() != selected_session["trading_date"]:
            continue
        time_key = summary.get("snapshot_utc")
        if not time_key or time_key in seen_times:
            continue
        seen_times.add(time_key)
        spot = float(summary["spot"])
        atm_iv = None
        call_volume = 0.0
        put_volume = 0.0
        chain: list[dict] = []
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            call_volume, put_volume = volume_totals(chart_rows)
            chain = chain_payload(chart_rows.to_dict(orient="records"))
            ribbon.append(gex_snapshot_from_chart_rows(time_key, chart_rows))
        except Exception:
            pass
        points.append(
            {
                "time": time_key,
                "atm_iv": clean_value(atm_iv),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(spot),
                "call_volume": clean_value(call_volume),
                "put_volume": clean_value(put_volume),
                "chain": chain,
                "expiry": clean_value(summary.get("expiry")),
            }
        )
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def filter_replay_series(points: list[dict], ribbon: list[dict], session: dict, selected_utc: str) -> tuple[list[dict], list[dict]]:
    start_ts = pd.Timestamp(session["collection_start_utc"])
    close_ts = pd.Timestamp(session["market_close_utc"])
    end_ts = pd.to_datetime(selected_utc, errors="coerce", utc=True)
    if pd.isna(end_ts):
        return points, ribbon
    end_ts = min(end_ts, close_ts)

    def in_window(value: str | None) -> bool:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
        return pd.notna(ts) and start_ts <= ts <= end_ts

    return (
        [point for point in points if in_window(point.get("time"))],
        [snap for snap in ribbon if in_window(snap.get("time"))],
    )


def replay_snapshots_from_history(summary_history_path: Path, selected_summary: dict, ticker: str) -> list[dict]:
    summaries = pd.read_parquet(summary_history_path)
    session = session_from_summary(selected_summary)
    start_ts = pd.Timestamp(session["collection_start_utc"])
    summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
    summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
    summaries = summaries[
        summaries["_snapshot_ts"].notna()
        & (summaries["_snapshot_ts"] >= start_ts)
        & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
    ].sort_values("snapshot_utc")
    out = []
    seen: set[str] = set()
    for row in summaries.to_dict(orient="records"):
        summary = summary_from_history_row(row)
        snapshot_utc = summary.get("snapshot_utc")
        if not snapshot_utc or snapshot_utc in seen:
            continue
        seen.add(snapshot_utc)
        out.append(
            {
                "id": history_snapshot_id(summary_history_path, snapshot_utc),
                "snapshot_utc": snapshot_utc,
                "label": snapshot_label(summary_history_path, summary),
            }
        )
    return out


def replay_snapshots_from_summary_path(summary_path: Path, ticker: str) -> list[dict]:
    selected_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    session = session_from_summary(selected_summary)
    start_ts = pd.Timestamp(session["collection_start_utc"])
    out = []
    seen: set[str] = set()
    for path in sorted(summary_path.parent.glob(f"{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if (
            pd.isna(snapshot)
            or snapshot < start_ts
            or snapshot.tz_convert(NY_TZ).date().isoformat() != session["trading_date"]
        ):
            continue
        snapshot_utc = summary.get("snapshot_utc")
        if not snapshot_utc or snapshot_utc in seen:
            continue
        seen.add(snapshot_utc)
        out.append(
            {
                "id": snapshot_id_for_path(path),
                "snapshot_utc": snapshot_utc,
                "label": snapshot_label(path, summary),
            }
        )
    return sorted(out, key=lambda item: item["snapshot_utc"])


def day_series_from_history(summary_history_path: Path, selected_summary: dict, ticker: str, window: float) -> tuple[list[dict], list[dict]]:
    summaries = pd.read_parquet(summary_history_path)
    selected_session = session_from_summary(selected_summary)
    start_ts = pd.Timestamp(selected_session["collection_start_utc"])
    summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
    summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
    summaries = summaries[
        summaries["_snapshot_ts"].notna()
        & (summaries["_snapshot_ts"] >= start_ts)
        & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == selected_session["trading_date"])
    ].sort_values("snapshot_utc")
    by_strike_path = history_by_strike_path(summary_history_path)
    by_strike_rows = pd.read_parquet(by_strike_path) if by_strike_path.exists() else None
    rows_by_snapshot: dict[str, pd.DataFrame] = {}
    if by_strike_rows is not None and not by_strike_rows.empty:
        by_strike_rows = by_strike_rows[
            (by_strike_rows["ticker"].astype(str).str.upper() == ticker.upper())
            & (by_strike_rows["expiry"].astype(str) == str(selected_summary.get("expiry")))
        ].copy()
        for time_key, frame in by_strike_rows.groupby(by_strike_rows["snapshot_utc"].astype(str), sort=False):
            rows_by_snapshot[str(time_key)] = frame
    points: list[dict] = []
    ribbon: list[dict] = []
    for row in summaries.to_dict(orient="records"):
        summary = summary_from_history_row(row)
        time_key = summary.get("snapshot_utc")
        if not time_key:
            continue
        records, gex_snapshot = rows_for_history_snapshot(
            summary_history_path,
            summary,
            window,
            rows_by_snapshot.get(str(time_key), by_strike_rows),
        )
        call_volume, put_volume = volume_totals(records)
        points.append(
            {
                "time": time_key,
                "atm_iv": clean_value(nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(float(summary["spot"])),
                "call_volume": clean_value(call_volume),
                "put_volume": clean_value(put_volume),
                "chain": chain_payload(records),
                "expiry": clean_value(summary.get("expiry")),
            }
        )
        ribbon.append(gex_snapshot)
    return points, ribbon



def load_latest(ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
    summary_path = latest_summary_path(ticker)
    summary, point, rows, history, gex_snapshot = chart_payload_from_summary_path(summary_path, ticker, window)
    return summary, point, rows, history, gex_snapshot


TENOR_REFRESH_EVERY_N_PULLS = 5
TENOR_REFRESH_HORIZON_DAYS = 10


def run_snapshot(args: argparse.Namespace, *, fetch_tenor: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(RUN_DASHBOARD_SCRIPT),
        "--ticker",
        args.ticker,
        "--window",
        str(args.window),
        "--rate",
        str(args.rate),
        "--top",
        str(args.top),
        "--output-root",
        str(DATA_ROOT),
        "--interactive-output",
        str(PROJECT_ROOT / "dashboard.html"),
        "--no-open",
    ]
    if args.expiry:
        cmd += ["--expiry", args.expiry]
    if fetch_tenor:
        cmd += ["--all-expiries", "--expiry-horizon-days", str(TENOR_REFRESH_HORIZON_DAYS)]
    return subprocess.run(cmd, cwd=PROJECT_ROOT, check=False, text=True, capture_output=True)


def collector(args: argparse.Namespace, state: LiveState, snapshot_service: SnapshotService | None = None) -> None:
    deadline = None
    if args.duration_minutes is not None:
        deadline = time.monotonic() + max(0, args.duration_minutes) * 60
    next_run = time.monotonic()
    pull_count = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            break
        with state.lock:
            already_locked = state.session_locked
        if already_locked:
            time.sleep(60)
            continue
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_fetch_dt = datetime.now() + timedelta(seconds=args.interval_seconds)
        with state.lock:
            state.next_fetch = next_fetch_dt.isoformat()

        pull_count += 1
        fetch_tenor = pull_count % TENOR_REFRESH_EVERY_N_PULLS == 0
        result = run_snapshot(args, fetch_tenor=fetch_tenor)
        with state.lock:
            if result.returncode != 0:
                state.failures += 1
                tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
                state.latest_error = tail[0] if tail else f"exit {result.returncode}"
            else:
                try:
                    summary, point, rows, history, gex_snapshot = load_latest(args.ticker, args.window)
                    point_ts = pd.to_datetime(point.get("time"), errors="coerce", utc=True)
                    collect_start = pd.Timestamp(state.session["collection_start_utc"])
                    market_close = pd.Timestamp(state.session["market_close_utc"])
                    if pd.notna(point_ts) and point_ts < market_close:
                        if collect_start <= point_ts:
                            if not any(p.get("time") == point.get("time") for p in state.points):
                                state.points.append(point)
                            if not any(r.get("time") == gex_snapshot.get("time") for r in state.gex_ribbon):
                                state.gex_ribbon.append(gex_snapshot)
                    elif pd.notna(point_ts) and not state.session_locked:
                        if not state.points:
                            state.points = [point]
                        if not state.gex_ribbon:
                            state.gex_ribbon = [gex_snapshot]
                        state.session_locked = True
                    elif (
                        pd.isna(point_ts)
                        and not state.session_locked
                        and pd.Timestamp.now(tz="UTC") >= market_close
                    ):
                        state.session_locked = True
                    if state.levels_summary is not None:
                        day_low, day_high = fetch_day_high_low(args.ticker)
                        if day_low is not None:
                            state.levels_summary["one_day_min"] = day_low
                        if day_high is not None:
                            state.levels_summary["one_day_max"] = day_high
                    market_open = pd.Timestamp(state.session["market_open_utc"])
                    if pd.notna(point_ts) and point_ts >= collect_start:
                        state.latest_summary = summary
                        state.by_strike = rows
                        state.history = history
                    if pd.notna(point_ts) and point_ts >= market_open:
                        state.skew_summary = summary
                        state.skew_by_strike = rows
                        state.skew_tenors = skew_tenors_payload(
                            args.ticker,
                            float(summary.get("spot") or 0.0),
                            summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or "",
                        )
                    state.latest_error = None
                    state.successes += 1
                except Exception as exc:
                    state.failures += 1
                    state.latest_error = str(exc)
        next_run += args.interval_seconds
    with state.lock:
        state.running = False
        state.next_fetch = None


def levels_collector(
    ticker: str,
    base_args: argparse.Namespace,
    state: LiveState,
    futures_ticker: str = "",
) -> None:
    """Lightweight sibling of collector(): keeps polling `ticker` so its local
    Parquet history keeps growing (needed so a *future* day's Levels Export
    can seed from today's actual close), while the displayed
    levels_summary_secondary stays frozen at the previous session's EOD
    (seeded once in main(), including its one-time NQ1! futures basis) with
    only its live 1D Min/Max refreshed in place on top of that frozen base."""
    secondary_args = copy.copy(base_args)
    secondary_args.ticker = ticker
    secondary_args.expiry = None
    next_run = time.monotonic()
    while True:
        with state.lock:
            if not state.running:
                break
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        run_snapshot(secondary_args, fetch_tenor=False)
        with state.lock:
            summary = state.levels_summary_secondary
        if summary is not None:
            day_low, day_high = fetch_day_high_low(ticker)
            if day_low is not None:
                summary["one_day_min"] = day_low
            if day_high is not None:
                summary["one_day_max"] = day_high
        next_run += base_args.interval_seconds



def main() -> None:
    args = parse_args()
    pull_github_updates_on_startup(not args.no_github_pull)
    set_collect_start_offset_min(args.collect_start_offset_min)
    state = LiveState(market_session_utc())
    state.session["collection_start_utc"] = collection_start_utc(state.session["market_open_utc"])
    state.points, state.gex_ribbon = seed_session_data(args.ticker, state.session, args.window)
    state.levels_summary = seed_prev_day_eod_summary(args.ticker, state.session["trading_date"])
    state.levels_locked = True
    collect_start_ts = pd.Timestamp(state.session["collection_start_utc"])
    market_open_ts = pd.Timestamp(state.session["market_open_utc"])
    state.latest_summary, state.by_strike, state.history = seed_locked_snapshot(
        args.ticker, state.session, collect_start_ts, args.window
    )
    state.skew_summary, state.skew_by_strike, _skew_history = seed_locked_snapshot(
        args.ticker, state.session, market_open_ts, args.window
    )
    if state.skew_summary:
        state.skew_tenors = skew_tenors_payload_for_summary(args.ticker, state.skew_summary)
    state.secondary_ticker = args.secondary_ticker.upper() if args.secondary_ticker else ""
    state.secondary_futures_ticker = (args.secondary_futures_ticker or "").upper()
    if state.secondary_ticker:
        state.levels_summary_secondary = seed_prev_day_eod_summary(
            state.secondary_ticker, state.session["trading_date"]
        )
        state.levels_locked_secondary = True
        apply_futures_basis(state.levels_summary_secondary, state.secondary_futures_ticker)
    snapshot_service = SnapshotService(
        list_trading_days=list_trading_days,
        latest_snapshot_id_for_trading_day=latest_snapshot_id_for_trading_day,
        parse_history_snapshot_id=parse_history_snapshot_id,
        chart_payload_from_history=chart_payload_from_history,
        day_series_from_history=day_series_from_history,
        replay_snapshots_from_history=replay_snapshots_from_history,
        filter_replay_series=filter_replay_series,
        session_from_summary=session_from_summary,
        candles_for_session=candles_for_session,
        summary_path_from_id=summary_path_from_id,
        chart_payload_from_summary_path=chart_payload_from_summary_path,
        day_series_from_summary_path=day_series_from_summary_path,
        replay_snapshots_from_summary_path=replay_snapshots_from_summary_path,
        skew_tenors_payload_for_summary=skew_tenors_payload_for_summary,
        latest_history_snapshot=latest_history_snapshot,
        latest_summary_path=latest_summary_path,
        vn_tz=VN_TZ,
        file_cache=SnapshotFileCache(SNAPSHOT_CACHE_ROOT),
    )

    intraday_service = IntradayService(
        session_for_trading_date=session_for_trading_date,
        seed_session_data=seed_session_data,
        latest_snapshot_id_for_trading_day=latest_snapshot_id_for_trading_day,
        candles_for_session=candles_for_session,
        snapshot_service=snapshot_service,
        file_cache=IntradayFileCache(INTRADAY_CACHE_ROOT),
    )

    def apply_secondary_basis_for_request(payload: dict, ticker: str) -> None:
        if (
            state.secondary_ticker
            and ticker.upper() == state.secondary_ticker.upper()
            and state.secondary_futures_ticker
        ):
            apply_futures_basis(payload.get("levels_summary"), state.secondary_futures_ticker)
            apply_futures_basis(payload.get("latest_summary"), state.secondary_futures_ticker)

    handler_class = configure_handler(
        state=state,
        ticker=args.ticker.upper(),
        window=args.window,
        snapshot_service=snapshot_service,
        intraday_service=intraday_service,
        apply_secondary_basis=apply_secondary_basis_for_request,
    )
    worker = threading.Thread(target=collector, args=(args, state, snapshot_service), daemon=True)
    worker.start()
    if state.secondary_ticker:
        levels_worker = threading.Thread(
            target=levels_collector,
            args=(state.secondary_ticker, args, state),
            kwargs={"futures_ticker": state.secondary_futures_ticker},
            daemon=True,
        )
        levels_worker.start()
    candles_worker = threading.Thread(
        target=alpaca_candles_collector,
        args=(args.ticker, state),
        kwargs={"market_session_utc": market_session_utc},
        daemon=True,
    )
    candles_worker.start()

    server = ThreadingHTTPServer((args.host, args.port), handler_class)
    url = f"http://{args.host}:{args.port}"
    print(f"Live dashboard: {url}", flush=True)
    duration = "until stopped" if args.duration_minutes is None else f"for {args.duration_minutes} minutes"
    print(f"Collecting {args.ticker.upper()} every {args.interval_seconds}s {duration}.", flush=True)
    collect_start_vn = pd.Timestamp(state.session["collection_start_utc"]).tz_convert(VN_TZ).strftime("%H:%M")
    print(
        f"Volatility Flow + Heat Tracker start at {collect_start_vn} Vietnam time "
        f"({args.collect_start_offset_min:g} min before NY open).",
        flush=True,
    )
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with state.lock:
            state.running = False
        server.server_close()


if __name__ == "__main__":
    main()
