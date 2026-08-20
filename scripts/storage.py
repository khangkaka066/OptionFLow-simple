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
from pathlib import Path


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
