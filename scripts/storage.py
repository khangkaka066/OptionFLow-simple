"""Output layout for a snapshot run.

data/options/{date}/
    raw/{TICKER}_{EXPIRY}_{HHMMSS}_cboe.json      raw CBOE delayed-quotes response
    raw/{TICKER}_{EXPIRY}_{HHMMSS}_yahoo.json     yfinance calls/puts dump + spot
    {TICKER}_{EXPIRY}_{HHMMSS}_by_strike.parquet  reconciled per-strike chain w/ gex/dex
    {TICKER}_{EXPIRY}_{HHMMSS}_summary.json       GexSummary (incl. gamma_flip/delta_flip)
    {TICKER}_{EXPIRY}_{HHMMSS}_reconciliation.json  source cross-check report
    replay_index.jsonl                            append-only: one line per run

    # untimestamped "latest" copies, what the dashboard/levels scripts read by default
    {TICKER}_{EXPIRY}_summary.json
    {TICKER}_{EXPIRY}_by_strike.parquet
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

INTRADAY_BUCKET = "1min"
DAILY_IV_RANK_LOOKBACK = 20
_SKEW_CAPTURE_WINDOWS_ET = [
    ("09:30", "09:40"),
    ("12:25", "12:35"),
    ("15:50", "16:00"),
]


def _json_safe(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return value


def _upsert_parquet(path: Path, rows: pd.DataFrame, keys: list[str], write_csv: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = pd.read_parquet(path)
            rows = pd.concat([existing, rows], ignore_index=True)
        except Exception:
            pass
    rows = rows.drop_duplicates(subset=keys, keep="last")
    sort_cols = [col for col in ["snapshot_utc", "strike"] if col in rows.columns]
    if sort_cols:
        rows = rows.sort_values(sort_cols)
    rows.to_parquet(path, index=False)
    if write_csv:
        rows.to_csv(path.with_suffix(".csv"), index=False)


def market_day_dir(output_dir: Path, ticker: str, trading_date: str | None = None) -> Path:
    root = output_dir.parents[1] / "market" if output_dir.parent.name == "options" else output_dir / "market"
    date_value = trading_date or output_dir.name
    return root / f"ticker={ticker}" / f"date={date_value}"


def normalize_raw_chain(
    frame: pd.DataFrame,
    *,
    capture_ts: str,
    ticker: str,
    source: str,
    source_ts: str | None = None,
    spot: float | None = None,
) -> pd.DataFrame:
    data = frame.copy()
    rename = {
        "openInterest": "open_interest",
        "impliedVolatility": "iv",
        "cboe_oi": "open_interest",
        "cboe_iv": "iv",
        "cboe_bid": "bid",
        "cboe_ask": "ask",
        "cboe_volume": "volume",
        "lastPrice": "last",
    }
    data = data.rename(columns={old: new for old, new in rename.items() if old in data.columns})
    data["capture_ts"] = capture_ts
    data["source"] = source
    data["source_ts"] = source_ts or capture_ts
    data["ticker"] = ticker
    data["spot"] = spot
    for col in ["expiry", "strike", "option_type", "bid", "ask", "last", "volume", "open_interest", "iv"]:
        if col not in data:
            data[col] = None
    columns = [
        "capture_ts",
        "source",
        "source_ts",
        "ticker",
        "expiry",
        "strike",
        "option_type",
        "bid",
        "ask",
        "last",
        "volume",
        "open_interest",
        "iv",
        "spot",
    ]
    return data.loc[:, columns]


def append_market_table(path: Path, rows: pd.DataFrame, keys: list[str]) -> Path:
    _upsert_parquet(path, rows, keys, write_csv=False)
    return path


INTRADAY_METRICS = ["net_gex", "net_dex", "net_vex", "net_chex", "call_volume", "put_volume"]
SUMMARY_METRICS = ["spot", "avg_iv", "net_gex", "net_dex", "gamma_flip", "delta_flip"]


def build_intraday_rows(
    strike_rows: pd.DataFrame,
    summary_dict: dict,
    *,
    capture_ts: str,
    ticker: str,
    trading_date: str,
) -> list[dict]:
    """Long-format intraday rows, floored to a 1-minute bucket (last pull per bucket wins)."""
    bucket_ts = pd.Timestamp(capture_ts).floor(INTRADAY_BUCKET).isoformat()
    rows: list[dict] = []
    for row in strike_rows.to_dict(orient="records"):
        for metric in INTRADAY_METRICS:
            value = row.get(metric) if metric in row else None
            value = value if pd.notna(value) else None
            rows.append(
                {
                    "capture_ts": capture_ts,
                    "ticker": ticker,
                    "trading_date": trading_date,
                    "bucket_ts": bucket_ts,
                    "interval": "1m",
                    "strike": row.get("strike"),
                    "metric_name": metric,
                    "value": value,
                }
            )
    for metric in SUMMARY_METRICS:
        if metric in summary_dict and summary_dict.get(metric) is not None:
            rows.append(
                {
                    "capture_ts": capture_ts,
                    "ticker": ticker,
                    "trading_date": trading_date,
                    "bucket_ts": bucket_ts,
                    "interval": "1m",
                    "strike": None,
                    "metric_name": metric,
                    "value": summary_dict.get(metric),
                }
            )
    return rows


def _is_skew_capture_window(capture_ts: str) -> bool:
    """Skew snapshots are a low-cadence point-in-time capture, not a time series."""
    try:
        moment = pd.Timestamp(capture_ts)
        if moment.tzinfo is None:
            moment = moment.tz_localize("UTC")
        ny = moment.tz_convert(ZoneInfo("America/New_York"))
    except Exception:
        return True
    hhmm = ny.strftime("%H:%M")
    return any(start <= hhmm <= end for start, end in _SKEW_CAPTURE_WINDOWS_ET)


def _daily_sqlite_path(output_dir: Path, ticker: str) -> Path:
    base = market_day_dir(output_dir, ticker)
    return base.parents[1] / "session_levels_daily.sqlite"


def _ensure_daily_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_levels_daily (
            trading_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            spot REAL,
            avg_iv REAL,
            iv_rank REAL,
            gamma_flip REAL,
            delta_flip REAL,
            gamma_wall REAL,
            dex_wall REAL,
            call_resistance REAL,
            put_support REAL,
            atm_iv_t0 REAL,
            atm_iv_t0_expiry TEXT,
            atm_iv_t1 REAL,
            atm_iv_t1_expiry TEXT,
            atm_iv_t2 REAL,
            atm_iv_t2_expiry TEXT,
            updated_at TEXT,
            PRIMARY KEY (trading_date, ticker)
        )
        """
    )


def _compute_iv_rank(conn: sqlite3.Connection, ticker: str, trading_date: str, avg_iv: float | None) -> float | None:
    if avg_iv is None:
        return None
    hist = conn.execute(
        """
        SELECT avg_iv FROM session_levels_daily
        WHERE ticker = ? AND trading_date < ? AND avg_iv IS NOT NULL
        ORDER BY trading_date DESC LIMIT ?
        """,
        (ticker, trading_date, DAILY_IV_RANK_LOOKBACK),
    ).fetchall()
    hist_values = [r[0] for r in hist]
    if not hist_values:
        return None
    below_or_equal = sum(1 for v in hist_values if v <= avg_iv)
    return 100.0 * below_or_equal / len(hist_values)


def upsert_daily_session_level(output_dir: Path, *, ticker: str, trading_date: str, level_row: dict) -> Path:
    db_path = _daily_sqlite_path(output_dir, ticker)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _ensure_daily_table(conn)
        iv_rank = _compute_iv_rank(conn, ticker, trading_date, level_row.get("avg_iv"))
        conn.execute(
            """
            INSERT INTO session_levels_daily (
                trading_date, ticker, spot, avg_iv, iv_rank, gamma_flip, delta_flip,
                gamma_wall, dex_wall, call_resistance, put_support, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_date, ticker) DO UPDATE SET
                spot=excluded.spot,
                avg_iv=excluded.avg_iv,
                iv_rank=excluded.iv_rank,
                gamma_flip=excluded.gamma_flip,
                delta_flip=excluded.delta_flip,
                gamma_wall=excluded.gamma_wall,
                dex_wall=excluded.dex_wall,
                call_resistance=excluded.call_resistance,
                put_support=excluded.put_support,
                updated_at=excluded.updated_at
            """,
            (
                trading_date,
                ticker,
                level_row.get("spot"),
                level_row.get("avg_iv"),
                iv_rank,
                level_row.get("gamma_flip"),
                level_row.get("delta_flip"),
                level_row.get("gamma_wall"),
                level_row.get("dex_wall"),
                level_row.get("call_resistance"),
                level_row.get("put_support"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def update_daily_tenor_iv(output_dir: Path, *, ticker: str, trading_date: str, tenor_atm_iv: dict[str, float]) -> Path | None:
    """tenor_atm_iv: {expiry: atm_iv}, nearest-3 expiries by calendar date -> t0/t1/t2 slots."""
    if not tenor_atm_iv:
        return None
    ordered = sorted(tenor_atm_iv.items())[:3]
    db_path = _daily_sqlite_path(output_dir, ticker)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _ensure_daily_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO session_levels_daily (trading_date, ticker) VALUES (?, ?)",
            (trading_date, ticker),
        )
        set_clauses = []
        values: list = []
        for i, (expiry, atm_iv) in enumerate(ordered):
            set_clauses.append(f"atm_iv_t{i}=?, atm_iv_t{i}_expiry=?")
            values.extend([atm_iv, expiry])
        values.extend([trading_date, ticker])
        conn.execute(
            f"UPDATE session_levels_daily SET {', '.join(set_clauses)} WHERE trading_date=? AND ticker=?",
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def append_market_dataset(
    output_dir: Path,
    *,
    ticker: str,
    summary_dict: dict,
    by_strike: pd.DataFrame,
    raw_frames: list[pd.DataFrame] | None = None,
    tenor_atm_iv: dict[str, float] | None = None,
) -> dict[str, Path]:
    trading_date = summary_dict.get("effective_snapshot_date") or summary_dict.get("requested_snapshot_date") or output_dir.name
    base = market_day_dir(output_dir, ticker, trading_date)
    capture_ts = summary_dict["snapshot_utc"]
    expiry_scope = summary_dict.get("expiry") or ""
    paths: dict[str, Path] = {}

    if raw_frames:
        raw_chain = pd.concat(raw_frames, ignore_index=True)
        paths["raw_chain"] = append_market_table(
            base / "raw_chain.parquet",
            raw_chain,
            ["capture_ts", "source", "ticker", "expiry", "strike", "option_type"],
        )

    strike_rows = by_strike.copy()
    for placeholder in ["net_vex", "net_chex"]:
        if placeholder not in strike_rows:
            strike_rows[placeholder] = None
    strike_rows.insert(0, "capture_ts", capture_ts)
    strike_rows.insert(1, "ticker", ticker)
    strike_rows.insert(2, "expiry_scope", expiry_scope)
    if "expiry" not in strike_rows:
        strike_rows.insert(3, "expiry", expiry_scope)
    paths["by_strike"] = append_market_table(
        base / "by_strike.parquet",
        strike_rows,
        ["capture_ts", "ticker", "expiry_scope", "expiry", "strike"],
    )

    intraday_rows = build_intraday_rows(
        strike_rows, summary_dict, capture_ts=capture_ts, ticker=ticker, trading_date=trading_date
    )
    if intraday_rows:
        paths["intraday_metrics"] = append_market_table(
            base / "intraday_metrics.parquet",
            pd.DataFrame(intraday_rows),
            ["bucket_ts", "ticker", "metric_name", "strike"],
        )

    if _is_skew_capture_window(capture_ts):
        skew_rows = strike_rows.loc[:, [col for col in ["capture_ts", "ticker", "expiry", "strike", "call_iv", "put_iv", "iv"] if col in strike_rows.columns]].copy()
        if not skew_rows.empty:
            spot = float(summary_dict.get("spot") or 0)
            if spot > 0:
                rel = (pd.to_numeric(skew_rows["strike"], errors="coerce") / spot) - 1.0
                skew_rows["moneyness"] = pd.cut(
                    rel.abs(),
                    bins=[-0.001, 0.01, 0.05, float("inf")],
                    labels=["ATM", "NEAR", "FAR"],
                ).astype(str)
            else:
                skew_rows["moneyness"] = None
            paths["skew_snapshot"] = append_market_table(
                base / "skew_snapshot.parquet",
                skew_rows,
                ["capture_ts", "ticker", "expiry", "strike"],
            )

    level_row = {
        "trading_date": trading_date,
        "capture_ts": capture_ts,
        "ticker": ticker,
        "expiry_scope": expiry_scope,
        "spot": summary_dict.get("spot"),
        "avg_iv": summary_dict.get("avg_iv"),
        "gamma_flip": summary_dict.get("gamma_flip"),
        "delta_flip": summary_dict.get("delta_flip"),
        "gamma_wall": summary_dict.get("gamma_wall_abs"),
        "dex_wall": summary_dict.get("dex_wall_abs"),
        "call_resistance": summary_dict.get("call_resistance"),
        "put_support": summary_dict.get("put_support"),
    }
    session_path = base.parents[1] / "session_levels_intraday.parquet"
    paths["session_levels_intraday"] = append_market_table(
        session_path,
        pd.DataFrame([level_row]),
        ["trading_date", "capture_ts", "ticker", "expiry_scope"],
    )
    paths["session_levels_daily"] = upsert_daily_session_level(
        output_dir, ticker=ticker, trading_date=trading_date, level_row=level_row
    )
    if tenor_atm_iv:
        tenor_path = update_daily_tenor_iv(
            output_dir, ticker=ticker, trading_date=trading_date, tenor_atm_iv=tenor_atm_iv
        )
        if tenor_path:
            paths["session_levels_daily"] = tenor_path
    return paths


def append_history_store(
    output_dir: Path,
    ticker: str,
    expiry: str,
    ts: str,
    by_strike,
    summary_dict: dict,
    reconciliation_dict: dict,
    raw_paths: tuple[Path, Path] | None = None,
    snapshot_paths: dict[str, Path] | None = None,
) -> dict[str, Path]:
    """Upsert a normalized, backtest-friendly history store for this day/expiry."""
    history_dir = output_dir / "history"
    snapshot_utc = summary_dict["snapshot_utc"]
    snapshot_date = summary_dict.get("requested_snapshot_date") or output_dir.name

    summary_row = {key: _json_safe(value) for key, value in summary_dict.items()}
    summary_row.update(
        {
            "snapshot_date": snapshot_date,
            "timestamp": ts,
        }
    )
    if raw_paths:
        summary_row["raw_cboe_path"] = str(raw_paths[0].relative_to(output_dir))
        summary_row["raw_yahoo_path"] = str(raw_paths[1].relative_to(output_dir))
    if snapshot_paths:
        summary_row["by_strike_path"] = str(snapshot_paths["by_strike"].relative_to(output_dir))
        summary_row["summary_path"] = str(snapshot_paths["summary"].relative_to(output_dir))
        summary_row["reconciliation_path"] = str(snapshot_paths["reconciliation"].relative_to(output_dir))
    for key, value in reconciliation_dict.items():
        summary_row[f"recon_{key}"] = _json_safe(value)

    summary_history = history_dir / f"{ticker}_{expiry}_snapshots.parquet"
    _upsert_parquet(summary_history, pd.DataFrame([summary_row]), ["snapshot_utc", "ticker", "expiry"], write_csv=False)

    strike_rows = by_strike.copy()
    strike_rows.insert(0, "snapshot_date", snapshot_date)
    strike_rows.insert(0, "snapshot_utc", snapshot_utc)
    strike_rows.insert(0, "expiry", expiry)
    strike_rows.insert(0, "ticker", ticker)
    by_strike_history = history_dir / f"{ticker}_{expiry}_by_strike_history.parquet"
    _upsert_parquet(by_strike_history, strike_rows, ["snapshot_utc", "ticker", "expiry", "strike"], write_csv=False)

    return {"snapshots": summary_history, "by_strike_history": by_strike_history}


def delete_raw(raw_paths: tuple[Path, Path] | None) -> list[Path]:
    if not raw_paths:
        return []
    deleted = []
    for path in raw_paths:
        if path.exists():
            path.unlink()
            deleted.append(path)
    return deleted


def save_raw(output_dir: Path, ticker: str, expiry: str, ts: str, cboe_raw: dict, yahoo_raw: dict) -> tuple[Path, Path]:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cboe_path = raw_dir / f"{ticker}_{expiry}_{ts}_cboe.json"
    yahoo_path = raw_dir / f"{ticker}_{expiry}_{ts}_yahoo.json"
    cboe_path.write_text(json.dumps(cboe_raw, indent=2, default=str), encoding="utf-8")
    yahoo_path.write_text(json.dumps(yahoo_raw, indent=2, default=str), encoding="utf-8")
    return cboe_path, yahoo_path


def save_snapshot(
    output_dir: Path,
    ticker: str,
    expiry: str,
    ts: str,
    by_strike,
    summary_dict: dict,
    reconciliation_dict: dict,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_strike_path = output_dir / f"{ticker}_{expiry}_{ts}_by_strike.parquet"
    summary_path = output_dir / f"{ticker}_{expiry}_{ts}_summary.json"
    reconciliation_path = output_dir / f"{ticker}_{expiry}_{ts}_reconciliation.json"

    by_strike.to_parquet(by_strike_path, index=False)
    summary_path.write_text(json.dumps(summary_dict, indent=2), encoding="utf-8")
    reconciliation_path.write_text(json.dumps(reconciliation_dict, indent=2), encoding="utf-8")

    return {
        "by_strike": by_strike_path,
        "summary": summary_path,
        "reconciliation": reconciliation_path,
    }


def update_latest(output_dir: Path, ticker: str, expiry: str, by_strike, summary_dict: dict) -> dict[str, Path]:
    latest_by_strike = output_dir / f"{ticker}_{expiry}_by_strike.parquet"
    latest_summary = output_dir / f"{ticker}_{expiry}_summary.json"
    by_strike.to_parquet(latest_by_strike, index=False)
    latest_summary.write_text(json.dumps(summary_dict, indent=2), encoding="utf-8")
    return {"by_strike": latest_by_strike, "summary": latest_summary}


def append_replay_index(output_dir: Path, ticker: str, expiry: str, ts: str, snapshot_paths: dict[str, Path]) -> Path:
    index_path = output_dir / "replay_index.jsonl"
    entry = {
        "ticker": ticker,
        "expiry": expiry,
        "timestamp": ts,
        "by_strike": str(snapshot_paths["by_strike"].relative_to(output_dir)),
        "summary": str(snapshot_paths["summary"].relative_to(output_dir)),
    }
    with index_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return index_path


def read_replay_index(output_dir: Path, ticker: str | None = None, expiry: str | None = None) -> list[dict]:
    index_path = output_dir / "replay_index.jsonl"
    if not index_path.exists():
        return []
    entries = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if ticker and entry.get("ticker") != ticker.upper():
            continue
        if expiry and entry.get("expiry") != expiry:
            continue
        entries.append(entry)
    return entries
