#!/usr/bin/env python3
"""DuckDB query layer for the normalized data/market parquet dataset."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import duckdb
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKET_ROOT = PROJECT_ROOT / "data" / "market"


class MarketQuery:
    def __init__(self, market_root: Path = DEFAULT_MARKET_ROOT) -> None:
        self.market_root = market_root
        self.con = duckdb.connect(database=":memory:")
        self._register_views()

    def _glob(self, table: str) -> str:
        return str(self.market_root / "ticker=*" / "date=*" / f"{table}.parquet")

    @staticmethod
    def _sql_string(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _register_views(self) -> None:
        views = {
            "raw_chain": self._glob("raw_chain"),
            "by_strike": self._glob("by_strike"),
            "intraday_metrics": self._glob("intraday_metrics"),
            "skew_snapshot": self._glob("skew_snapshot"),
        }
        for view, path in views.items():
            self.con.execute(f"create or replace view {view} as select * from read_parquet({self._sql_string(path)})")
        session_path = self.market_root / "session_levels_intraday.parquet"
        self.con.execute(f"create or replace view session_levels as select * from read_parquet({self._sql_string(str(session_path))})")

    def sql(self, query: str, params: list | None = None):
        return self.con.execute(query, params or []).df()

    def trading_days(self, ticker: str):
        return self.sql(
            """
            select trading_date, ticker, count(*) as snapshots, max(capture_ts) as latest_capture_ts
            from session_levels
            where ticker = ?
            group by trading_date, ticker
            order by trading_date desc
            """,
            [ticker.upper()],
        )

    def session_levels(self, ticker: str, trading_date: str | None = None):
        where = "where ticker = ?"
        params = [ticker.upper()]
        if trading_date:
            where += " and trading_date = ?"
            params.append(trading_date)
        return self.sql(
            f"""
            select *
            from session_levels
            {where}
            order by capture_ts
            """,
            params,
        )

    def session_levels_daily(self, ticker: str, trading_date: str | None = None) -> pd.DataFrame:
        """One row/day, read directly from the SQLite daily rollup (spec: SQLite for this small table)."""
        db_path = self.market_root / "session_levels_daily.sqlite"
        if not db_path.exists():
            return pd.DataFrame()
        conn = sqlite3.connect(db_path)
        try:
            where = "where ticker = ?"
            params: list = [ticker.upper()]
            if trading_date:
                where += " and trading_date = ?"
                params.append(trading_date)
            return pd.read_sql_query(
                f"select * from session_levels_daily {where} order by trading_date",
                conn,
                params=params,
            )
        finally:
            conn.close()

    def heat_tracker(self, ticker: str, trading_date: str, metric: str = "net_gex"):
        return self.sql(
            """
            select capture_ts, ticker, strike, value
            from intraday_metrics
            where ticker = ?
              and trading_date = ?
              and metric_name = ?
              and strike is not null
            order by capture_ts, strike
            """,
            [ticker.upper(), trading_date, metric],
        )

    def volatility_flow(self, ticker: str, trading_date: str):
        return self.sql(
            """
            with spot_iv as (
              select
                capture_ts,
                max(case when metric_name = 'spot' then value end) as spot,
                max(case when metric_name = 'avg_iv' then value * 100 end) as avg_iv_pct
              from intraday_metrics
              where ticker = ? and trading_date = ? and strike is null
              group by capture_ts
            ),
            flow as (
              select
                capture_ts,
                sum(case when metric_name = 'call_volume' then value else 0 end) as call_volume,
                sum(case when metric_name = 'put_volume' then value else 0 end) as put_volume
              from intraday_metrics
              where ticker = ? and trading_date = ? and strike is not null
              group by capture_ts
            )
            select
              s.capture_ts,
              s.spot,
              s.avg_iv_pct,
              f.call_volume,
              f.put_volume,
              sum(coalesce(f.call_volume, 0) - coalesce(f.put_volume, 0))
                over (order by s.capture_ts rows between unbounded preceding and current row) as cumulative_call_minus_put
            from spot_iv s
            left join flow f using (capture_ts)
            order by s.capture_ts
            """,
            [ticker.upper(), trading_date, ticker.upper(), trading_date],
        )

    def by_strike_snapshot(self, ticker: str, capture_ts: str):
        return self.sql(
            """
            select *
            from by_strike
            where ticker = ? and capture_ts = ?
            order by strike
            """,
            [ticker.upper(), capture_ts],
        )

    def skew(self, ticker: str, capture_ts: str):
        return self.sql(
            """
            select *
            from skew_snapshot
            where ticker = ? and capture_ts = ?
            order by strike
            """,
            [ticker.upper(), capture_ts],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query data/market parquet files through DuckDB.")
    parser.add_argument("command", choices=["days", "levels", "heat", "flow", "by-strike", "skew", "sql"])
    parser.add_argument("--market-root", type=Path, default=DEFAULT_MARKET_ROOT)
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--date", default=None)
    parser.add_argument("--metric", default="net_gex")
    parser.add_argument("--capture-ts", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mq = MarketQuery(args.market_root)
    if args.command == "days":
        df = mq.trading_days(args.ticker)
    elif args.command == "levels":
        df = mq.session_levels(args.ticker, args.date)
    elif args.command == "heat":
        if not args.date:
            raise SystemExit("--date is required for heat")
        df = mq.heat_tracker(args.ticker, args.date, args.metric)
    elif args.command == "flow":
        if not args.date:
            raise SystemExit("--date is required for flow")
        df = mq.volatility_flow(args.ticker, args.date)
    elif args.command == "by-strike":
        if not args.capture_ts:
            raise SystemExit("--capture-ts is required for by-strike")
        df = mq.by_strike_snapshot(args.ticker, args.capture_ts)
    elif args.command == "skew":
        if not args.capture_ts:
            raise SystemExit("--capture-ts is required for skew")
        df = mq.skew(args.ticker, args.capture_ts)
    else:
        if not args.query:
            raise SystemExit("--query is required for sql")
        df = mq.sql(args.query)
    print(df.head(args.limit).to_string(index=False))


if __name__ == "__main__":
    main()
