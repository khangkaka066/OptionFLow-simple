"""Collect the server-rendered public Xynth Flow table for the dashboard."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

NY = ZoneInfo("America/New_York")
URL = "https://xynth.com/flow?ticker={ticker}"
TIME_FORMAT = "%m/%d/%y %H:%M:%S"
CONTRACT = re.compile(r"\$([\d,.]+)\s+(CALL|PUT)\b")


def _number(value: str) -> float | None:
    try:
        return float(value.replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def parse_flow_html(html: str, ticker: str) -> list[dict]:
    table = BeautifulSoup(html, "html.parser").select_one("table.flow-table")
    if table is None:
        raise ValueError("Xynth public flow table is missing")
    headings = [th.get_text(" ", strip=True) for th in table.select("thead th")]
    required = {"Time (ET)", "Ticker", "Side", "Contract", "Expiry", "Stock", "Size", "Premium", "Volume", "OI", "IV %"}
    if not required.issubset(headings):
        raise ValueError("Xynth public flow columns changed")
    rows = []
    for tr in table.select("tbody tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(cells) != len(headings):
            continue
        item = dict(zip(headings, cells))
        if item["Ticker"].upper() != ticker:
            continue
        try:
            event_ny = datetime.strptime(item["Time (ET)"], TIME_FORMAT).replace(tzinfo=NY)
        except ValueError:
            continue
        contract = CONTRACT.search(item["Contract"])
        if not contract:
            continue
        rows.append({
            "time": event_ny.astimezone(timezone.utc).isoformat(),
            "time_et": item["Time (ET)"],
            "trading_date": event_ny.date().isoformat(),
            "ticker": ticker,
            "side": item["Side"],
            "contract": item["Contract"],
            "strike": _number(contract.group(1)),
            "cp": "C" if contract.group(2) == "CALL" else "P",
            "expiry": item["Expiry"],
            "spot": _number(item["Stock"]),
            "size": _number(item["Size"]),
            "premium": item["Premium"],
            "volume": _number(item["Volume"]),
            "open_interest": _number(item["OI"]),
            "iv_pct": _number(item["IV %"]),
            "flags": item.get("Flags", ""),
            "tags": item.get("Tags", ""),
        })
    return rows


def is_same_day_expiry(row: dict) -> bool:
    """Use each print's ET date, not the page's DTE value or today's date."""
    try:
        expiry = datetime.strptime(str(row["expiry"]), "%m/%d/%y").date()
        trade_day = datetime.fromisoformat(str(row["trading_date"])).date()
    except (KeyError, TypeError, ValueError):
        return False
    return expiry == trade_day


class XynthFlowService:
    def __init__(self, root: Path, ticker: str = "QQQ") -> None:
        self.root = Path(root)
        self.ticker = ticker.upper()
        self.lock = threading.Lock()
        self.last_checked_at: str | None = None
        self.last_error: str | None = None
        self._load_latest()

    def _path(self, day: str) -> Path:
        return self.root / self.ticker / f"{day}.json"

    def _load_latest(self) -> None:
        files = sorted((self.root / self.ticker).glob("????-??-??.json"))
        self.rows = []
        self.trading_date = None
        for path in reversed(files):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                valid_rows = [row for row in loaded.get("rows", []) if is_same_day_expiry(row)]
                if valid_rows:
                    self.rows = valid_rows
                    self.trading_date = loaded["trading_date"]
                    break
            except (OSError, ValueError, KeyError):
                continue

    def refresh(self) -> dict:
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            response = requests.get(URL.format(ticker=self.ticker), timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            incoming = [row for row in parse_flow_html(response.text, self.ticker) if is_same_day_expiry(row)]
            # An overnight or pre-open print does not start the new regular session.
            incoming = [row for row in incoming if datetime.fromisoformat(row["time"]).astimezone(NY).time().isoformat() >= "09:30:00"]
            if not incoming:
                raise ValueError("No QQQ 0DTE rows in public HTML")
            newest_day = max(row["trading_date"] for row in incoming)
            incoming = [row for row in incoming if row["trading_date"] == newest_day]
            with self.lock:
                self.last_checked_at = checked_at
                self.last_error = None
                if self.trading_date is None or newest_day >= self.trading_date:
                    existing = [row for row in self.rows if is_same_day_expiry(row)] if newest_day == self.trading_date else []
                    keys = {(row["time"], row["contract"], row["side"], row["size"], row["premium"], row["volume"]) for row in existing}
                    merged = list(existing)
                    for row in incoming:
                        key = (row["time"], row["contract"], row["side"], row["size"], row["premium"], row["volume"])
                        if key not in keys:
                            merged.append(row)
                            keys.add(key)
                    merged.sort(key=lambda row: row["time"], reverse=True)
                    self.rows = merged
                    self.trading_date = newest_day
                    path = self._path(newest_day)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"trading_date": newest_day, "rows": merged}, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            with self.lock:
                self.last_checked_at = checked_at
                self.last_error = str(exc)
        return self.snapshot()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "source": "Xynth public HTML",
                "source_url": URL.format(ticker=self.ticker),
                "ticker": self.ticker,
                "trading_date": self.trading_date,
                "last_checked_at": self.last_checked_at,
                "error": self.last_error,
                "partial": True,
                "rows": [row for row in self.rows if is_same_day_expiry(row)],
            }
