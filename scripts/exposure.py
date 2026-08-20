"""GEX/DEX exposure and key levels: Gamma Wall, Call Resistance, Put Support,
Gamma Flip, Delta Flip.

Convention (personal-research approximation, not a production dealer model):

    call GEX = +gamma * openInterest * 100 * spot^2 * 1%
    put GEX  = -gamma * openInterest * 100 * spot^2 * 1%
    dex      =  delta * openInterest * 100 * spot
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from bsm import bs_delta, bs_gamma

MULTIPLIER = 100


@dataclass
class GexSummary:
    ticker: str
    snapshot_utc: str
    requested_snapshot_date: str
    effective_snapshot_date: str
    expiry: str
    spot: float
    days_to_expiry: int
    years_to_expiry: float
    rate: float
    net_gex: float
    total_call_gex: float
    total_put_gex: float
    gamma_wall_abs: float | None
    gamma_wall_positive: float | None
    call_resistance: float | None
    put_support: float | None
    gamma_flip: float | None
    net_dex: float
    total_call_dex: float
    total_put_dex: float
    dex_wall_abs: float | None
    dex_wall_positive: float | None
    delta_flip: float | None
    top_abs_gex_levels: list[dict]
    top_abs_dex_levels: list[dict]
    avg_iv: float | None
    included_expiries: list[str] = field(default_factory=list)


def compute_greeks(
    reconciled: pd.DataFrame, spot: float, years_by_expiry: dict[str, float], rate: float
) -> pd.DataFrame:
    chain = reconciled.copy()
    years = chain["expiry"].map(years_by_expiry)
    chain["gamma"] = [
        bs_gamma(spot, strike, y, rate, iv) for strike, y, iv in zip(chain["strike"], years, chain["impliedVolatility"])
    ]
    chain["delta"] = [
        bs_delta(spot, strike, y, rate, iv, option_type)
        for strike, y, iv, option_type in zip(chain["strike"], years, chain["impliedVolatility"], chain["option_type"])
    ]
    raw_gex = chain["gamma"] * chain["openInterest"] * MULTIPLIER * spot**2 * 0.01
    chain["gex"] = np.where(chain["option_type"] == "call", raw_gex, -raw_gex)
    chain["abs_gex"] = chain["gex"].abs()
    chain["dex"] = chain["delta"] * chain["openInterest"] * MULTIPLIER * spot
    chain["abs_dex"] = chain["dex"].abs()
    return chain


def aggregate_by_strike(chain: pd.DataFrame) -> pd.DataFrame:
    calls = chain[chain["option_type"] == "call"]
    puts = chain[chain["option_type"] == "put"]
    call_by_strike = calls.groupby("strike", dropna=True).agg(
        call_gex=("gex", "sum"),
        call_dex=("dex", "sum"),
        call_oi=("openInterest", "sum"),
        call_iv=("impliedVolatility", "mean"),
    )
    put_by_strike = puts.groupby("strike", dropna=True).agg(
        put_gex=("gex", "sum"),
        put_dex=("dex", "sum"),
        put_oi=("openInterest", "sum"),
        put_iv=("impliedVolatility", "mean"),
    )
    by_strike = call_by_strike.join(put_by_strike, how="outer").sort_index()
    by_strike["iv"] = by_strike[["call_iv", "put_iv"]].mean(axis=1, skipna=True)
    fill_cols = ["call_gex", "call_dex", "call_oi", "put_gex", "put_dex", "put_oi"]
    by_strike[fill_cols] = by_strike[fill_cols].fillna(0.0)
    by_strike["net_gex"] = by_strike["call_gex"] + by_strike["put_gex"]
    by_strike["abs_net_gex"] = by_strike["net_gex"].abs()
    by_strike["net_dex"] = by_strike["call_dex"] + by_strike["put_dex"]
    by_strike["abs_net_dex"] = by_strike["net_dex"].abs()
    return by_strike.reset_index()


def compute_flip(by_strike: pd.DataFrame, column: str, spot: float) -> float | None:
    """Strike where the cumulative (strike-ascending) sum of `column` crosses zero.

    Far-OTM strikes can carry floating-point-noise-level exposure (e.g. 1e-38)
    whose sign flips spuriously; among all zero-crossings, pick the one closest
    to spot since that's the economically meaningful flip point.
    """
    data = by_strike.sort_values("strike").reset_index(drop=True)
    if data.empty:
        return None
    cum = data[column].cumsum()
    sign = np.sign(cum)
    best_flip: float | None = None
    best_distance = float("inf")
    for i in range(1, len(data)):
        prev_sign, cur_sign = sign.iloc[i - 1], sign.iloc[i]
        if prev_sign == 0 or cur_sign == 0 or prev_sign == cur_sign:
            continue
        x0, x1 = float(data["strike"].iloc[i - 1]), float(data["strike"].iloc[i])
        y0, y1 = float(cum.iloc[i - 1]), float(cum.iloc[i])
        flip = x1 if y1 == y0 else x0 + (-y0 / (y1 - y0)) * (x1 - x0)
        distance = abs(flip - spot)
        if distance < best_distance:
            best_distance = distance
            best_flip = flip
    return best_flip


def first_or_none(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[0]
    return None if pd.isna(value) else float(value)


def build_summary(
    ticker: str,
    expiry: str,
    spot: float,
    days: int,
    years: float,
    rate: float,
    chain: pd.DataFrame,
    by_strike: pd.DataFrame,
    top: int,
    requested_snapshot_day: date,
    effective_snapshot_day_value: date,
    included_expiries: list[str] | None = None,
) -> GexSummary:
    calls = chain[chain["option_type"] == "call"]
    puts = chain[chain["option_type"] == "put"]

    top_levels = (
        by_strike.sort_values("abs_net_gex", ascending=False)
        .head(top)
        .loc[:, ["strike", "net_gex", "call_gex", "put_gex", "abs_net_gex"]]
    )
    top_dex_levels = (
        by_strike.sort_values("abs_net_dex", ascending=False)
        .head(top)
        .loc[:, ["strike", "net_dex", "call_dex", "put_dex", "abs_net_dex"]]
    )

    gamma_wall_abs = first_or_none(top_levels["strike"])
    positive = by_strike[by_strike["net_gex"] > 0].sort_values("net_gex", ascending=False)
    gamma_wall_positive = first_or_none(positive["strike"])
    dex_wall_abs = first_or_none(top_dex_levels["strike"])
    dex_positive = by_strike[by_strike["net_dex"] > 0].sort_values("net_dex", ascending=False)
    dex_wall_positive = first_or_none(dex_positive["strike"])

    call_candidates = calls[calls["strike"] > spot].sort_values("gex", ascending=False)
    put_candidates = puts[puts["strike"] < spot].sort_values("gex", ascending=True)

    iv_values = chain.loc[chain["impliedVolatility"] > 0, "impliedVolatility"]
    avg_iv = float(iv_values.mean()) if not iv_values.empty else None

    return GexSummary(
        ticker=ticker,
        snapshot_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        requested_snapshot_date=requested_snapshot_day.isoformat(),
        effective_snapshot_date=effective_snapshot_day_value.isoformat(),
        expiry=expiry,
        spot=float(spot),
        days_to_expiry=int(days),
        years_to_expiry=float(years),
        rate=float(rate),
        net_gex=float(by_strike["net_gex"].sum()),
        total_call_gex=float(calls["gex"].sum()),
        total_put_gex=float(puts["gex"].sum()),
        gamma_wall_abs=gamma_wall_abs,
        gamma_wall_positive=gamma_wall_positive,
        call_resistance=first_or_none(call_candidates["strike"]),
        put_support=first_or_none(put_candidates["strike"]),
        gamma_flip=compute_flip(by_strike, "net_gex", spot),
        net_dex=float(by_strike["net_dex"].sum()),
        total_call_dex=float(calls["dex"].sum()),
        total_put_dex=float(puts["dex"].sum()),
        dex_wall_abs=dex_wall_abs,
        dex_wall_positive=dex_wall_positive,
        delta_flip=compute_flip(by_strike, "net_dex", spot),
        top_abs_gex_levels=top_levels.to_dict(orient="records"),
        top_abs_dex_levels=top_dex_levels.to_dict(orient="records"),
        avg_iv=avg_iv,
        included_expiries=included_expiries if included_expiries is not None else [expiry],
    )
