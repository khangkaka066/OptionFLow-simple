"""Reconcile CBOE (structural/OI) and Yahoo (spot/bid/ask/IV) option chains.

CBOE delayed quotes are treated as the authoritative source for contract
structure and open interest. Yahoo's latest-available quote is treated as the
authoritative source for spot/bid/ask/IV. Each side is cross-checked against
the other and disagreements beyond a tolerance are flagged (not dropped) so
the reconciled row is still usable downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

IV_DIFF_FLAG_PCT = 0.20  # flag a strike if |yahoo_iv - cboe_iv| / cboe_iv exceeds this
SPOT_DIFF_FLAG_PCT = 0.01  # flag spot if Yahoo vs CBOE underlying differs by more than this
YAHOO_IV_MIN = 0.01  # Yahoo returns a near-zero placeholder (e.g. 1e-05) instead of NaN when it
# cannot solve IV for a stale/no-quote (bid=ask=0) contract; treat anything below this as invalid.


@dataclass
class ReconciliationReport:
    total_strikes: int
    matched_both_sources: int
    cboe_only: int
    yahoo_only: int
    iv_flagged_count: int
    oi_fallback_count: int
    spot_yahoo: float
    spot_cboe: float | None
    spot_diff_pct: float | None
    spot_flagged: bool

    def as_dict(self) -> dict:
        return {
            "total_strikes": self.total_strikes,
            "matched_both_sources": self.matched_both_sources,
            "cboe_only": self.cboe_only,
            "yahoo_only": self.yahoo_only,
            "iv_flagged_count": self.iv_flagged_count,
            "oi_fallback_count": self.oi_fallback_count,
            "spot_yahoo": self.spot_yahoo,
            "spot_cboe": self.spot_cboe,
            "spot_diff_pct": self.spot_diff_pct,
            "spot_flagged": self.spot_flagged,
        }


def _yahoo_side(calls: pd.DataFrame, puts: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([calls, puts], ignore_index=True)
    return combined[
        ["strike", "option_type", "expiry", "openInterest", "impliedVolatility", "volume", "bid", "ask"]
    ].rename(
        columns={
            "openInterest": "yahoo_oi",
            "impliedVolatility": "yahoo_iv",
            "volume": "yahoo_volume",
            "bid": "yahoo_bid",
            "ask": "yahoo_ask",
        }
    )


def reconcile_chain(
    cboe_chain: pd.DataFrame,
    yahoo_calls: pd.DataFrame,
    yahoo_puts: pd.DataFrame,
    spot_yahoo: float,
    spot_cboe: float | None,
) -> tuple[pd.DataFrame, ReconciliationReport]:
    yahoo_side = _yahoo_side(yahoo_calls, yahoo_puts)
    cboe_side = cboe_chain[
        ["strike", "option_type", "expiry", "cboe_oi", "cboe_volume", "cboe_bid", "cboe_ask", "cboe_iv"]
    ]

    merged = pd.merge(cboe_side, yahoo_side, on=["strike", "option_type", "expiry"], how="outer", indicator=True)

    in_cboe = merged["_merge"].isin(["both", "left_only"])
    in_yahoo = merged["_merge"].isin(["both", "right_only"])

    merged["oi_source"] = np.where(merged["cboe_oi"].notna(), "cboe", "yahoo")
    merged["openInterest"] = merged["cboe_oi"].fillna(merged["yahoo_oi"]).fillna(0.0)

    yahoo_iv_valid = merged["yahoo_iv"].notna() & (merged["yahoo_iv"] >= YAHOO_IV_MIN)
    cboe_iv_valid = merged["cboe_iv"].notna() & (merged["cboe_iv"] > 0)

    merged["iv_source"] = np.select(
        [yahoo_iv_valid, cboe_iv_valid],
        ["yahoo", "cboe"],
        default=np.where(merged["yahoo_iv"].notna(), "yahoo", "cboe"),
    )
    merged["impliedVolatility"] = np.select(
        [yahoo_iv_valid, cboe_iv_valid],
        [merged["yahoo_iv"], merged["cboe_iv"]],
        default=merged["yahoo_iv"].fillna(merged["cboe_iv"]),
    )

    merged["bid"] = merged["yahoo_bid"].fillna(merged["cboe_bid"])
    merged["ask"] = merged["yahoo_ask"].fillna(merged["cboe_ask"])
    merged["volume"] = merged["yahoo_volume"].fillna(merged["cboe_volume"]).fillna(0.0)

    both = merged["yahoo_iv"].notna() & merged["cboe_iv"].notna() & (merged["cboe_iv"] > 0)
    iv_diff_pct = pd.Series(np.nan, index=merged.index)
    iv_diff_pct[both] = (merged.loc[both, "yahoo_iv"] - merged.loc[both, "cboe_iv"]).abs() / merged.loc[both, "cboe_iv"]
    merged["iv_diff_pct"] = iv_diff_pct
    merged["iv_flagged"] = iv_diff_pct > IV_DIFF_FLAG_PCT

    reconciled = merged[
        [
            "strike",
            "option_type",
            "expiry",
            "openInterest",
            "impliedVolatility",
            "volume",
            "bid",
            "ask",
            "oi_source",
            "iv_source",
            "iv_diff_pct",
            "iv_flagged",
        ]
    ].copy()
    reconciled = reconciled.sort_values(["option_type", "strike"]).reset_index(drop=True)

    spot_diff_pct = None
    spot_flagged = False
    if spot_cboe is not None and spot_cboe > 0:
        spot_diff_pct = abs(spot_yahoo - spot_cboe) / spot_cboe
        spot_flagged = spot_diff_pct > SPOT_DIFF_FLAG_PCT

    report = ReconciliationReport(
        total_strikes=len(merged),
        matched_both_sources=int((merged["_merge"] == "both").sum()),
        cboe_only=int((merged["_merge"] == "left_only").sum()),
        yahoo_only=int((merged["_merge"] == "right_only").sum()),
        iv_flagged_count=int(merged["iv_flagged"].sum()),
        oi_fallback_count=int((merged["oi_source"] == "yahoo").sum()),
        spot_yahoo=float(spot_yahoo),
        spot_cboe=spot_cboe,
        spot_diff_pct=spot_diff_pct,
        spot_flagged=spot_flagged,
    )
    return reconciled, report
