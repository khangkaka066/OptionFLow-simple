"""Read the public InsiderFinance option-flow sample for the Net Drift panel."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ENDPOINT = "https://gql.insiderfinance.io/v1/graphql"
PAGE_SIZE = 20  # The public GraphQL role caps each response at 20 rows.
MAX_PUBLIC_TRADES = 10000
QUERY = """query NetDriftPublicFlow($ticker: String!, $tradeDate: date!, $offset: Int!) {
  free_ticker_option_flow(
    where: {ticker: {_eq: $ticker}, date: {_eq: $tradeDate}}
    order_by: [{createdAt: desc}, {id: desc}]
    limit: 20
    offset: $offset
  ) {
    id cp date exchangeTimestamp expireDay expireMonth expireYear
    premium price size spot strike tradeSide
  }
}"""
LATEST_QUERY = """query NetDriftLatestPublicFlow($ticker: String!) {
  free_ticker_option_flow(
    where: {ticker: {_eq: $ticker}}
    order_by: {createdAt: desc}
    limit: 20
  ) {
    id cp date exchangeTimestamp expireDay expireMonth expireYear
    premium price size spot strike tradeSide
  }
}"""
COUNT_QUERY = """query NetDriftPublicCount($ticker: String!, $tradeDate: date!) {
  free_ticker_option_flow_aggregate(
    where: {ticker: {_eq: $ticker}, date: {_eq: $tradeDate}}
  ) { aggregate { count } }
}"""
NY = ZoneInfo("America/New_York")


def _load_page(ticker: str, trade_date: str, offset: int) -> list[dict]:
    variables = {"ticker": ticker, "tradeDate": trade_date, "offset": offset}
    for attempt in range(3):
        try:
            return _graphql(QUERY, variables).get("free_ticker_option_flow") or []
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.4 * (attempt + 1))
    return []


def _graphql(query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(ENDPOINT, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; QuantResearch/1.0)",
    })
    with urlopen(request, timeout=12) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise ValueError(payload["errors"][0].get("message", "InsiderFinance query failed"))
    return payload["data"]


def _normalize(trade: dict) -> dict:
    event = datetime.fromtimestamp(int(trade["exchangeTimestamp"]) / 1e9, timezone.utc)
    expiry = "{:04d}-{:02d}-{:02d}".format(
        trade["expireYear"], trade["expireMonth"], trade["expireDay"]
    )
    return {
        "id": trade["id"], "time": event.isoformat(),
        "time_ny": event.astimezone(NY).isoformat(),
        "date": trade["date"], "cp": trade["cp"],
        "expiry": expiry, "premium": trade["premium"],
        "trade_side": trade["tradeSide"], "size": trade["size"],
        "price": trade["price"], "spot": trade["spot"],
        "strike": trade["strike"],
    }


class NetDriftService:
    def load(self, ticker: str, trade_date: str | None = None) -> dict:
        ticker = ticker.strip().upper()
        if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", ticker):
            raise ValueError("Invalid ticker")
        if trade_date:
            try:
                if date.fromisoformat(trade_date).isoformat() != trade_date:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Date must be YYYY-MM-DD") from exc
            actual_date = trade_date
        else:
            data = _graphql(LATEST_QUERY, {"ticker": ticker})
            latest_rows = data.get("free_ticker_option_flow") or []
            actual_date = latest_rows[0]["date"] if latest_rows else None

        total_count = None
        raw_rows: list[dict] = []
        if actual_date:
            count_data = _graphql(COUNT_QUERY, {"ticker": ticker, "tradeDate": actual_date})
            total_count = int(count_data["free_ticker_option_flow_aggregate"]["aggregate"]["count"])
            offsets = range(0, min(total_count, MAX_PUBLIC_TRADES), PAGE_SIZE)
            # Each page uses the ordinary public pagination API. Keep concurrency
            # modest so a session refresh does not flood the provider.
            with ThreadPoolExecutor(max_workers=4) as pool:
                for page in pool.map(lambda offset: _load_page(ticker, actual_date, offset), offsets):
                    raw_rows.extend(page)

        seen: set[str] = set()
        trades = []
        for row in raw_rows:
            if row.get("date") != actual_date:
                continue
            trade_id = str(row.get("id"))
            if trade_id in seen:
                continue
            seen.add(trade_id)
            trades.append(_normalize(row))
        trades.sort(key=lambda row: (row["time"], str(row["id"])))
        sides = sorted({str(row["trade_side"] or "").upper() for row in trades})

        return {
            "source": "InsiderFinance public free option flow",
            "source_url": f"https://www.insiderfinance.io/ticker-flow/{ticker}",
            "ticker": ticker,
            "date": actual_date,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "returned_count": len(trades),
            "session_count": total_count,
            "partial": total_count is None or len(trades) < total_count,
            "trade_sides": sides,
            "page_size": PAGE_SIZE,
            "trades": trades,
        }
