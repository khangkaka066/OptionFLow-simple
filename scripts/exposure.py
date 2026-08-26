"""GEX/DEX/VEX/CHEX exposure and key levels: Gamma/Vanna/Charm Wall, Call
Resistance, Put Support, Gamma/Delta/Vanna/Charm Flip.

Convention (personal-research approximation, not a production dealer model):

    call GEX  = +gamma * openInterest * 100 * spot^2 * 1%
    put GEX   = -gamma * openInterest * 100 * spot^2 * 1%
    dex       =  delta * openInterest * 100 * spot
    call VEX  = +vanna * openInterest * 100 * spot * 1%      ($ per 1% IV move)
    put VEX   = -vanna * openInterest * 100 * spot * 1%
    call CHEX = +(charm / 365) * openInterest * 100 * spot   ($ delta decay per day)
    put CHEX  = -(charm / 365) * openInterest * 100 * spot

Vanna/Charm, like Gamma, are type-independent (same value for calls and
puts under q=0), so VEX/CHEX use the same explicit sign-flip-by-option-
type heuristic as GEX rather than relying on a naturally signed Greek.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from bsm import bs_charm, bs_delta, bs_gamma, bs_vanna, implied_volatility_from_price

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
    net_vex: float
    total_call_vex: float
    total_put_vex: float
    vanna_wall_abs: float | None
    vanna_wall_positive: float | None
    vanna_flip: float | None
    net_chex: float
    total_call_chex: float
    total_put_chex: float
    charm_wall_abs: float | None
    charm_wall_positive: float | None
    charm_flip: float | None
    top_abs_gex_levels: list[dict]
    top_abs_dex_levels: list[dict]
    avg_iv: float | None
    included_expiries: list[str] = field(default_factory=list)


def compute_greeks(
    reconciled: pd.DataFrame, spot: float, years_by_expiry: dict[str, float], rate: float
) -> pd.DataFrame:
    chain = reconciled.copy()
    years = chain["expiry"].map(years_by_expiry)
    chain["input_impliedVolatility"] = pd.to_numeric(chain["impliedVolatility"], errors="coerce")
    chain["strike"] = pd.to_numeric(chain["strike"], errors="coerce")
    chain["bid"] = pd.to_numeric(chain.get("bid", np.nan), errors="coerce")
    chain["ask"] = pd.to_numeric(chain.get("ask", np.nan), errors="coerce")
    raw_mid = np.where((chain["bid"] > 0) & (chain["ask"] > chain["bid"]), (chain["bid"] + chain["ask"]) / 2.0, np.nan)
    chain["raw_mid"] = raw_mid
    chain["spread_pct"] = (chain["ask"] - chain["bid"]) / chain["raw_mid"]
    moneyness = (chain["strike"] / float(spot) - 1.0).abs()
    max_spread_pct = np.select(
        [moneyness <= 0.02, moneyness <= 0.08],
        [0.15, 0.25],
        default=0.35,
    )
    chain["quote_quality_ok"] = (
        (chain["bid"] > 0)
        & (chain["ask"] > chain["bid"])
        & np.isfinite(chain["raw_mid"])
        & np.isfinite(chain["spread_pct"])
        & (chain["spread_pct"] <= max_spread_pct)
    )
    chain["mid"] = chain["raw_mid"].where(chain["quote_quality_ok"])
    chain["mid_iv"] = [
        implied_volatility_from_price(mid, spot, strike, y, rate, option_type)
        if np.isfinite(mid) and np.isfinite(y)
        else float("nan")
        for mid, strike, y, option_type in zip(chain["mid"], chain["strike"], years, chain["option_type"])
    ]
    mid_iv = pd.to_numeric(chain["mid_iv"], errors="coerce")
    source_iv = chain["input_impliedVolatility"].where(chain["input_impliedVolatility"].between(0.01, 5.0))
    chain["impliedVolatility"] = mid_iv.where(mid_iv.between(0.01, 5.0), source_iv)
    chain["iv_source_model"] = np.where(mid_iv.between(0.01, 5.0), "mid", "source")
    chain["gamma"] = [
        bs_gamma(spot, strike, y, rate, iv) for strike, y, iv in zip(chain["strike"], years, chain["impliedVolatility"])
    ]
    chain["delta"] = [
        bs_delta(spot, strike, y, rate, iv, option_type)
        for strike, y, iv, option_type in zip(chain["strike"], years, chain["impliedVolatility"], chain["option_type"])
    ]
    chain["vanna"] = [
        bs_vanna(spot, strike, y, rate, iv) for strike, y, iv in zip(chain["strike"], years, chain["impliedVolatility"])
    ]
    chain["charm"] = [
        bs_charm(spot, strike, y, rate, iv) for strike, y, iv in zip(chain["strike"], years, chain["impliedVolatility"])
    ]
    raw_gex = chain["gamma"] * chain["openInterest"] * MULTIPLIER * spot**2 * 0.01
    chain["gex"] = np.where(chain["option_type"] == "call", raw_gex, -raw_gex)
    chain["abs_gex"] = chain["gex"].abs()
    chain["dex"] = chain["delta"] * chain["openInterest"] * MULTIPLIER * spot
    chain["abs_dex"] = chain["dex"].abs()
    raw_vex = chain["vanna"] * chain["openInterest"] * MULTIPLIER * spot * 0.01
    chain["vex"] = np.where(chain["option_type"] == "call", raw_vex, -raw_vex)
    chain["abs_vex"] = chain["vex"].abs()
    raw_chex = (chain["charm"] / 365.0) * chain["openInterest"] * MULTIPLIER * spot
    chain["chex"] = np.where(chain["option_type"] == "call", raw_chex, -raw_chex)
    chain["abs_chex"] = chain["chex"].abs()
    return chain


def aggregate_by_strike(chain: pd.DataFrame) -> pd.DataFrame:
    calls = chain[chain["option_type"] == "call"]
    puts = chain[chain["option_type"] == "put"]
    call_by_strike = calls.groupby("strike", dropna=True).agg(
        call_gex=("gex", "sum"),
        call_dex=("dex", "sum"),
        call_vex=("vex", "sum"),
        call_chex=("chex", "sum"),
        call_oi=("openInterest", "sum"),
        call_volume=("volume", "sum"),
        call_iv=("impliedVolatility", "mean"),
        call_mid=("mid", "mean"),
    )
    put_by_strike = puts.groupby("strike", dropna=True).agg(
        put_gex=("gex", "sum"),
        put_dex=("dex", "sum"),
        put_vex=("vex", "sum"),
        put_chex=("chex", "sum"),
        put_oi=("openInterest", "sum"),
        put_volume=("volume", "sum"),
        put_iv=("impliedVolatility", "mean"),
        put_mid=("mid", "mean"),
    )
    by_strike = call_by_strike.join(put_by_strike, how="outer").sort_index()
    by_strike["iv"] = by_strike[["call_iv", "put_iv"]].mean(axis=1, skipna=True)
    fill_cols = [
        "call_gex", "call_dex", "call_vex", "call_chex", "call_oi", "call_volume",
        "put_gex", "put_dex", "put_vex", "put_chex", "put_oi", "put_volume",
    ]
    by_strike[fill_cols] = by_strike[fill_cols].fillna(0.0)
    by_strike["net_gex"] = by_strike["call_gex"] + by_strike["put_gex"]
    by_strike["abs_net_gex"] = by_strike["net_gex"].abs()
    by_strike["net_dex"] = by_strike["call_dex"] + by_strike["put_dex"]
    by_strike["abs_net_dex"] = by_strike["net_dex"].abs()
    by_strike["net_vex"] = by_strike["call_vex"] + by_strike["put_vex"]
    by_strike["abs_net_vex"] = by_strike["net_vex"].abs()
    by_strike["net_chex"] = by_strike["call_chex"] + by_strike["put_chex"]
    by_strike["abs_net_chex"] = by_strike["net_chex"].abs()
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


def nearest_atm_iv(by_strike: pd.DataFrame, spot: float) -> float | None:
    """Robust ATM IV (%): median of the 'iv' column across the strikes
    nearest to spot that carry real open interest or volume.

    A single nearest-strike pick is vulnerable to one-off BSM back-solve
    noise: call_iv and put_iv on the very same strike can diverge 5-10x from
    a stale or crossed quote on one leg even when open interest/volume are
    large. Taking the median across a small local window of liquid strikes
    damps that single-point noise without pulling in strikes far enough OTM
    to carry real skew.
    """
    clean = by_strike[np.isfinite(by_strike["strike"])].copy()
    if clean.empty or "iv" not in clean:
        return None
    for col in ("call_oi", "put_oi", "call_volume", "put_volume"):
        if col not in clean:
            clean[col] = 0.0
    oi = clean["call_oi"].fillna(0) + clean["put_oi"].fillna(0)
    volume = clean["call_volume"].fillna(0) + clean["put_volume"].fillna(0)
    liquid = clean[((oi > 0) | (volume > 0)) & clean["iv"].notna()]
    pool = liquid if not liquid.empty else clean[clean["iv"].notna()]
    if pool.empty:
        return None
    near = pool.assign(dist=(pool["strike"] - spot).abs()).sort_values("dist").head(5)
    return float(near["iv"].median()) * 100


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
    top_vex_levels = (
        by_strike.sort_values("abs_net_vex", ascending=False)
        .head(top)
        .loc[:, ["strike", "net_vex", "call_vex", "put_vex", "abs_net_vex"]]
    )
    top_chex_levels = (
        by_strike.sort_values("abs_net_chex", ascending=False)
        .head(top)
        .loc[:, ["strike", "net_chex", "call_chex", "put_chex", "abs_net_chex"]]
    )

    gamma_wall_abs = first_or_none(top_levels["strike"])
    positive = by_strike[by_strike["net_gex"] > 0].sort_values("net_gex", ascending=False)
    gamma_wall_positive = first_or_none(positive["strike"])
    dex_wall_abs = first_or_none(top_dex_levels["strike"])
    dex_positive = by_strike[by_strike["net_dex"] > 0].sort_values("net_dex", ascending=False)
    dex_wall_positive = first_or_none(dex_positive["strike"])
    vanna_wall_abs = first_or_none(top_vex_levels["strike"])
    vex_positive = by_strike[by_strike["net_vex"] > 0].sort_values("net_vex", ascending=False)
    vanna_wall_positive = first_or_none(vex_positive["strike"])
    charm_wall_abs = first_or_none(top_chex_levels["strike"])
    chex_positive = by_strike[by_strike["net_chex"] > 0].sort_values("net_chex", ascending=False)
    charm_wall_positive = first_or_none(chex_positive["strike"])

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
        net_vex=float(by_strike["net_vex"].sum()),
        total_call_vex=float(calls["vex"].sum()),
        total_put_vex=float(puts["vex"].sum()),
        vanna_wall_abs=vanna_wall_abs,
        vanna_wall_positive=vanna_wall_positive,
        vanna_flip=compute_flip(by_strike, "net_vex", spot),
        net_chex=float(by_strike["net_chex"].sum()),
        total_call_chex=float(calls["chex"].sum()),
        total_put_chex=float(puts["chex"].sum()),
        charm_wall_abs=charm_wall_abs,
        charm_wall_positive=charm_wall_positive,
        charm_flip=compute_flip(by_strike, "net_chex", spot),
        top_abs_gex_levels=top_levels.to_dict(orient="records"),
        top_abs_dex_levels=top_dex_levels.to_dict(orient="records"),
        avg_iv=avg_iv,
        included_expiries=included_expiries if included_expiries is not None else [expiry],
    )
