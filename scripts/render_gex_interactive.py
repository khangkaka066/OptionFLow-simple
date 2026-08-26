#!/usr/bin/env python3
"""Render an interactive Plotly GEX/DEX dashboard from saved outputs."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from export_gex_levels_text import build_line as build_levels_line
from exposure import nearest_atm_iv


BG = "#05070B"
PANEL = "#090D15"
CARD = "#10151F"
GRID = "#1F2937"
TEXT = "#E5E7EB"
MUTED = "#94A3B8"
GREEN = "#4ADE80"
PURPLE = "#7C3AED"
ORANGE = "#F59E0B"
PINK = "#F472B6"
SPOT_COLOR = "#CBD5E1"

DATE_DIR_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

CHART_HEIGHT = 520
CHART_MARGIN_TOP = 20
CHART_MARGIN_BOTTOM = 10
CYAN = "#22D3EE"
YELLOW = "#FACC15"

# MenthorQ-style exposure chart palette: cyan = call/positive side,
# amber = put/negative side, purple/blue reserved for the wall/flip levels.
EXPOSURE_BG = "#000000"
EXPOSURE_POS = "#22D3EE"
EXPOSURE_NEG = "#F59E0B"
LEVEL_COLORS = {
    "CALL RESISTANCE": "#22D3EE",
    "PUT SUPPORT": "#F59E0B",
    "GAMMA WALL": "#C084FC",
    "GAMMA FLIP": "#38BDF8",
    "DELTA WALL": "#C084FC",
    "DELTA FLIP": "#38BDF8",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render interactive GEX dashboard HTML.")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--expiry", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--window", type=float, default=14)
    parser.add_argument("--top", type=int, default=40)
    return parser.parse_args()


def choose_latest_file(input_dir: Path, suffix: str, ticker: str | None, expiry: str | None) -> Path:
    """Pick the untimestamped 'latest' file, i.e. exactly `{TICKER}_{EXPIRY}_{suffix}`."""
    if ticker and expiry:
        exact = input_dir / f"{ticker.upper()}_{expiry}_{suffix}"
        if exact.exists():
            return exact
    pattern = f"[A-Z]*_????-??-??_{suffix}"
    matches = sorted(input_dir.glob(pattern))
    if ticker:
        matches = [p for p in matches if p.name.startswith(f"{ticker.upper()}_")]
    if expiry:
        matches = [p for p in matches if f"_{expiry}_{suffix}" in p.name]
    if not matches:
        raise FileNotFoundError(f"No latest {suffix} found in {input_dir}")
    return matches[0]


def load_data(input_dir: Path, ticker: str | None, expiry: str | None):
    summary_path = choose_latest_file(input_dir, "summary.json", ticker, expiry)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    by_strike_path = choose_latest_file(input_dir, "by_strike.parquet", summary["ticker"], summary["expiry"])
    by_strike = pd.read_parquet(by_strike_path)
    return summary, by_strike


def compact(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(float(value))
    for unit, div in [("B", 1e9), ("M", 1e6), ("K", 1e3)]:
        if value >= div:
            return f"{sign}{value / div:.2f}{unit}"
    return f"{sign}{value:.0f}"


def money_m(value: float) -> str:
    if value is None or not np.isfinite(float(value)):
        return "NA"
    sign = "-" if float(value) < 0 else ""
    return f"{sign}${abs(float(value)) / 1e6:.1f}M"


def pct_rank(series: pd.Series, value: float) -> float:
    clean = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty or not np.isfinite(value):
        return float("nan")
    return float((clean <= value).mean() * 100)


MIN_IV_RANK_SESSIONS = 8


def rolling_iv_rank(values: pd.Series, window: int = 60, min_periods: int = MIN_IV_RANK_SESSIONS) -> pd.Series:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    low = clean.rolling(window=window, min_periods=min_periods).min()
    high = clean.rolling(window=window, min_periods=min_periods).max()
    span = high - low
    rank = (clean - low) / span * 100.0
    return rank.where(span > 0).clip(0, 100)


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
    candidates = []
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


def nearest_row(data: pd.DataFrame, spot: float) -> pd.Series | None:
    clean = data[np.isfinite(data["strike"])].copy()
    if clean.empty:
        return None
    idx = (clean["strike"] - spot).abs().idxmin()
    return clean.loc[idx]


def smooth_iv_curve(curve: pd.DataFrame) -> pd.DataFrame:
    clean = curve.copy()
    if clean.empty or "iv" not in clean:
        return clean
    clean["iv"] = pd.to_numeric(clean["iv"], errors="coerce")
    clean = clean.replace([np.inf, -np.inf], np.nan).dropna(subset=["strike", "iv"]).sort_values("strike")
    if len(clean) < 3:
        return clean
    median = clean["iv"].rolling(window=3, center=True, min_periods=1).median()
    jump_limit = np.maximum(0.03, median.abs() * 0.35)
    cleaned_iv = clean["iv"].where((clean["iv"] - median).abs() <= jump_limit, median)
    clean["iv"] = cleaned_iv.rolling(window=3, center=True, min_periods=1).mean()
    return clean


def compute_arv_pct(flow: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Annualized realized volatility from the spot path, in percent."""
    if flow.empty or "spot" not in flow or "time" not in flow:
        return pd.Series(dtype=float)
    rows = flow.copy()
    rows["time"] = pd.to_datetime(rows["time"], errors="coerce")
    rows["spot"] = pd.to_numeric(rows["spot"], errors="coerce")
    log_returns = np.log(rows["spot"] / rows["spot"].shift(1))
    minutes = rows["time"].diff().dt.total_seconds().div(60).clip(lower=1)
    out = []
    for idx in range(len(rows)):
        window_returns = log_returns.iloc[max(0, idx - lookback + 1) : idx + 1].dropna()
        window_minutes = minutes.iloc[max(0, idx - lookback + 1) : idx + 1].dropna()
        if len(window_returns) < 3 or window_minutes.empty:
            out.append(np.nan)
            continue
        median_minutes = float(window_minutes.median()) or 1.0
        out.append(float(window_returns.std(ddof=1) * np.sqrt((252 * 390) / median_minutes) * 100))
    return pd.Series(out, index=flow.index)


def top_right_legend() -> dict:
    return dict(
        orientation="h",
        y=1.16,
        x=1,
        xanchor="right",
        yanchor="bottom",
        bgcolor="rgba(9,13,21,0.70)",
    )


def chart_layout(title: str, height: int = 360, margin_t: int = 64) -> dict:
    return dict(
        title=dict(text="", font=dict(color=TEXT, size=13), x=0),
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT),
        height=height,
        margin=dict(l=70, r=60, t=margin_t, b=45),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(5,7,11,0.9)", font=dict(color=TEXT)),
        dragmode="pan",
        showlegend=True,
        legend=top_right_legend(),
        xaxis=dict(gridcolor=GRID, color=MUTED),
        yaxis=dict(gridcolor=GRID, color=MUTED),
    )


def prepare_data(by_strike: pd.DataFrame, spot: float, window: float, top: int) -> pd.DataFrame:
    data = by_strike.copy()
    data = data[np.isfinite(data["strike"])].copy()
    for col in ["call_dex", "put_dex", "net_dex", "abs_net_dex", "call_oi", "put_oi"]:
        if col not in data:
            data[col] = 0.0
    if "iv" not in data:
        data["iv"] = np.nan
    in_window = data[(data["strike"] >= spot - window) & (data["strike"] <= spot + window)]
    if len(in_window) < 12:
        in_window = data.sort_values("abs_net_gex", ascending=False).head(top)
    return in_window.sort_values("strike")


def add_hline(fig: go.Figure, y: float, color: str, dash: str = "dash") -> None:
    fig.add_hline(y=y, line_dash=dash, line_color=color, opacity=0.85)


def add_spot_vline_label(fig: go.Figure, spot: float) -> None:
    fig.add_vline(x=spot, line_dash="dot", line_color=SPOT_COLOR)
    fig.add_annotation(
        x=spot,
        y=0.98,
        xref="x",
        yref="paper",
        text=f"SPOT {spot:.2f}",
        showarrow=False,
        yanchor="top",
        xanchor="left",
        xshift=6,
        font=dict(color=SPOT_COLOR, size=11),
        bgcolor="rgba(5,7,11,0.70)",
        borderpad=2,
    )


def add_level_labels(
    fig: go.Figure,
    levels: list[tuple[str, float, str]],
    y_min: float,
    y_max: float,
    plot_height_px: float,
    min_gap_px: float = 18.0,
) -> None:
    """Labels stay pinned to their line's y, nudged apart only when crowded.

    Levels within a few strikes of each other (common near 0DTE spot) used
    to render text right on top of each other. Instead of moving the label
    to a detached legend, keep it next to its own line and only push it up
    when it would collide with the next one below, drawing a thin leader
    line back to the real level so it still reads as "attached" to the line.
    """
    value_range = (y_max - y_min) or 1.0
    px_per_unit = plot_height_px / value_range

    ordered = sorted(levels, key=lambda item: item[1], reverse=True)
    assigned_px: list[float] = []
    for _, level, _ in ordered:
        natural_px = (y_max - level) * px_per_unit
        min_px = (assigned_px[-1] + min_gap_px) if assigned_px else natural_px
        assigned_px.append(max(natural_px, min_px))

    for (text, level, color), natural_px, target_px in zip(
        ordered, [(y_max - lvl) * px_per_unit for _, lvl, _ in ordered], assigned_px
    ):
        nudge_px = target_px - natural_px
        show_arrow = abs(nudge_px) > 2.0
        fig.add_annotation(
            x=0.995,
            y=level,
            xref="x domain",
            yref="y",
            text=f"{text} {level:g}",
            showarrow=show_arrow,
            arrowhead=0,
            arrowwidth=1,
            arrowcolor=color,
            standoff=2,
            ax=0,
            ay=nudge_px,
            xanchor="right",
            font=dict(color=color, size=12),
            bgcolor="rgba(5,7,11,0.75)",
            borderpad=2,
        )


def build_chart(mode: str, summary: dict, data: pd.DataFrame, spot: float) -> go.Figure:
    value_col = "net_gex" if mode == "gex" else "net_dex"
    call_col = "call_gex" if mode == "gex" else "call_dex"
    put_col = "put_gex" if mode == "gex" else "put_dex"
    abs_col = "abs_net_gex" if mode == "gex" else "abs_net_dex"
    label = "Net GEX" if mode == "gex" else "Net DEX"

    colors = np.where(data[value_col] >= 0, EXPOSURE_POS, EXPOSURE_NEG)
    max_abs_value = float(data[value_col].abs().max()) if not data.empty else 1.0
    x_limit = max(max_abs_value * 1.12, 1.0)

    strike_step = data["strike"].sort_values().diff().dropna()
    strike_step = float(strike_step.median()) if not strike_step.empty else 1.0

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=data[value_col],
            y=data["strike"],
            orientation="h",
            width=strike_step * 0.35,
            marker=dict(color=colors, line=dict(width=0)),
            customdata=np.stack(
                [
                    data[value_col].map(money_m),
                    data[call_col].map(money_m),
                    data[put_col].map(money_m),
                ],
                axis=-1,
            ),
            hovertemplate=(
                "<b>$%{y:g} net %{customdata[0]}</b><br>"
                "<span style='color:#22D3EE'>Net Call %{customdata[1]}</span><br>"
                "<span style='color:#F59E0B'>Net Put %{customdata[2]}</span><extra></extra>"
            ),
            name=label,
        )
    )
    fig.add_vline(x=0, line_dash="dot", line_color="#475569")
    add_hline(fig, spot, SPOT_COLOR, dash="dot")

    if mode == "gex":
        levels = [
            ("CALL RESISTANCE", summary.get("call_resistance"), LEVEL_COLORS["CALL RESISTANCE"]),
            ("PUT SUPPORT", summary.get("put_support"), LEVEL_COLORS["PUT SUPPORT"]),
            ("GAMMA WALL", summary.get("gamma_wall_abs"), LEVEL_COLORS["GAMMA WALL"]),
            ("GAMMA FLIP", summary.get("gamma_flip"), LEVEL_COLORS["GAMMA FLIP"]),
        ]
    else:
        levels = [
            ("PUT SUPPORT", summary.get("put_support"), LEVEL_COLORS["PUT SUPPORT"]),
            ("DELTA FLIP", summary.get("delta_flip"), LEVEL_COLORS["DELTA FLIP"]),
            ("CALL RESISTANCE", summary.get("call_resistance"), LEVEL_COLORS["CALL RESISTANCE"]),
            ("DELTA WALL", summary.get("dex_wall_abs"), LEVEL_COLORS["DELTA WALL"]),
        ]
    label_levels = [("SPOT", spot, SPOT_COLOR)]
    for text, level, color in levels:
        if level is None or pd.isna(level):
            continue
        level = float(level)
        add_hline(fig, level, color, dash="dashdot")
        label_levels.append((text, level, color))
    add_level_labels(
        fig,
        label_levels,
        y_min=float(data["strike"].min()),
        y_max=float(data["strike"].max()),
        plot_height_px=CHART_HEIGHT - CHART_MARGIN_TOP - CHART_MARGIN_BOTTOM,
    )

    fig.update_layout(
        paper_bgcolor=EXPOSURE_BG,
        plot_bgcolor=EXPOSURE_BG,
        font=dict(color=TEXT, family="Menlo, Consolas, monospace"),
        height=CHART_HEIGHT,
        margin=dict(l=55, r=35, t=CHART_MARGIN_TOP, b=10),
        hovermode="closest",
        dragmode="pan",
        showlegend=False,
        xaxis=dict(visible=False, zeroline=False, range=[-x_limit, x_limit]),
        yaxis=dict(gridcolor="#1A1D24", color=MUTED, fixedrange=False, title=None),
        bargap=0,
    )
    return fig


def build_oi_iv_chart(data: pd.DataFrame, spot: float) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=data["strike"],
            y=data["call_oi"],
            marker=dict(color=EXPOSURE_POS),
            name="Calls",
            hovertemplate="Calls %{y:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=data["strike"],
            y=data["put_oi"],
            marker=dict(color=EXPOSURE_NEG),
            name="Puts",
            hovertemplate="Puts %{y:,.0f}<extra></extra>",
        )
    )
    iv_data = data[data["iv"].notna()]
    fig.add_trace(
        go.Scatter(
            x=iv_data["strike"],
            y=iv_data["iv"] * 100,
            mode="lines",
            line=dict(color="#F8FAFC", width=2),
            name="IV",
            yaxis="y2",
            hovertemplate="IV %{y:.1f}%<extra></extra>",
        )
    )
    add_spot_vline_label(fig, spot)

    fig.update_layout(
        title=dict(text="", font=dict(color=TEXT, size=13), x=0),
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT),
        height=380,
        margin=dict(l=70, r=60, t=64, b=45),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(5,7,11,0.9)", font=dict(color=TEXT)),
        dragmode="pan",
        barmode="group",
        bargap=0.15,
        bargroupgap=0.0,
        showlegend=True,
        legend=top_right_legend(),
        xaxis=dict(title="Strike", gridcolor=GRID, color=MUTED),
        yaxis=dict(title="Open Interest", gridcolor=GRID, color=MUTED),
        yaxis2=dict(title="IV %", overlaying="y", side="right", color=MUTED, showgrid=False),
    )
    return fig


def build_oi_chart(data: pd.DataFrame, spot: float) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=data["strike"],
            y=data["call_oi"],
            marker=dict(color=EXPOSURE_POS),
            name="Calls",
            hovertemplate="Calls %{y:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=data["strike"],
            y=data["put_oi"],
            marker=dict(color=EXPOSURE_NEG),
            name="Puts",
            hovertemplate="Puts %{y:,.0f}<extra></extra>",
        )
    )
    add_spot_vline_label(fig, spot)

    fig.update_layout(
        title=dict(text="", font=dict(color=TEXT, size=13), x=0),
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT),
        height=340,
        margin=dict(l=70, r=35, t=64, b=45),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(5,7,11,0.9)", font=dict(color=TEXT)),
        dragmode="pan",
        barmode="group",
        bargap=0.15,
        bargroupgap=0.0,
        showlegend=True,
        legend=top_right_legend(),
        xaxis=dict(title="Strike", gridcolor=GRID, color=MUTED),
        yaxis=dict(title="Open Interest", gridcolor=GRID, color=MUTED),
    )
    return fig


TENOR_COLORS = [ORANGE, CYAN, GREEN, PURPLE, PINK, YELLOW]


def load_multi_tenor_skew(options_root: Path, ticker: str) -> tuple[pd.DataFrame, str | None]:
    """Latest per-contract chain capture across all fetched expiries.

    Unlike `by_strike.parquet` (single expiry, or already collapsed across
    expiries with no `expiry` column), `data/market/ticker=<T>/date=*/raw_chain.parquet`
    keeps one row per (source, expiry, strike, option_type) contract, which is
    what's needed to build one IV curve per tenor. Returns (rows, capture_ts);
    rows is empty and capture_ts is None when no market dataset exists yet.
    """
    market_root = options_root.parent / "market" / f"ticker={ticker.upper()}"
    if not market_root.is_dir():
        return pd.DataFrame(), None
    frames = []
    for day_dir in sorted(market_root.iterdir()):
        raw_path = day_dir / "raw_chain.parquet"
        if raw_path.exists():
            try:
                frames.append(pd.read_parquet(raw_path, columns=[
                    "capture_ts", "source", "expiry", "strike", "option_type", "iv", "open_interest", "volume",
                ]))
            except Exception:
                continue
    if not frames:
        return pd.DataFrame(), None
    raw = pd.concat(frames, ignore_index=True)
    if raw.empty:
        return pd.DataFrame(), None
    # Prefer the latest capture that actually spans multiple expiries: some
    # callers (e.g. live_server's per-pull single-expiry snapshots) write to
    # this same table far more often than the periodic multi-expiry refresh,
    # so the newest capture_ts alone would usually be a single-tenor pull.
    expiry_counts = raw.groupby("capture_ts")["expiry"].nunique()
    multi_tenor_ts = expiry_counts[expiry_counts >= 2]
    capture_ts = multi_tenor_ts.index.max() if not multi_tenor_ts.empty else raw["capture_ts"].max()
    return raw[raw["capture_ts"] == capture_ts].copy(), capture_ts


def build_tenor_curves(
    raw: pd.DataFrame, spot: float, effective_day: str, dtes: tuple[int, ...] = (0, 1, 2), moneyness_band: float = 0.22
) -> list[dict]:
    """Per-tenor OTM Call/Put/blended-IV curves for near-dated expiries in `dtes`.

    Deep-OTM strikes carry real quotes but only from stale/wide/crossed
    single-sided prints (no BSM mid-price backsolve here, unlike the aggregated
    by_strike pipeline), so IV there can spike to 100-250%+ noise. Clamping to
    a moneyness band around spot keeps each curve to the region real skew
    lives in. Liquidity is required per-side (open interest OR volume, like
    the single-expiry Call/Put split) rather than dropping the whole row when
    only one side of a strike is quoted.
    """
    rows = raw.copy()
    rows["iv"] = pd.to_numeric(rows["iv"], errors="coerce")
    rows["strike"] = pd.to_numeric(rows["strike"], errors="coerce")
    rows["open_interest"] = pd.to_numeric(rows.get("open_interest", 0.0), errors="coerce").fillna(0.0)
    rows["volume"] = pd.to_numeric(rows.get("volume", 0.0), errors="coerce").fillna(0.0)
    moneyness = (rows["strike"] / float(spot) - 1.0).abs()
    rows = rows[
        (rows["iv"] > 0.01)
        & (rows["iv"] < 3.0)
        & ((rows["open_interest"] > 0) | (rows["volume"] > 0))
        & (moneyness <= moneyness_band)
    ]
    if rows.empty:
        return []

    # yahoo IV tends to be the cleaner backsolve on this pipeline; prefer it over
    # cboe for a given (expiry, strike, option_type) and fall back to cboe otherwise.
    rows["source_rank"] = np.where(rows["source"] == "yahoo", 0, 1)
    rows = rows.sort_values(["expiry", "strike", "option_type", "source_rank"])
    rows = rows.drop_duplicates(subset=["expiry", "strike", "option_type"], keep="first")

    try:
        ref_day = date.fromisoformat(effective_day)
    except ValueError:
        ref_day = date.today()

    expiry_dte = {}
    for expiry in rows["expiry"].unique():
        try:
            expiry_dte[expiry] = (date.fromisoformat(expiry) - ref_day).days
        except ValueError:
            continue
    wanted_expiries = sorted(exp for exp, dte in expiry_dte.items() if dte in dtes)

    curves = []
    for i, expiry in enumerate(wanted_expiries):
        exp_rows = rows[rows["expiry"] == expiry]
        per_strike = exp_rows.groupby("strike", as_index=False)["iv"].mean()
        raw_iv_curve = smooth_iv_curve(per_strike.loc[:, ["strike", "iv"]])
        atm = nearest_row(raw_iv_curve, spot) if not raw_iv_curve.empty else None
        atm_strike = float(atm["strike"]) if atm is not None else float(spot)
        atm_iv = float(atm["iv"]) if atm is not None and pd.notna(atm.get("iv")) else float("nan")
        atm_point = pd.DataFrame([{"strike": atm_strike, "iv": atm_iv}]) if np.isfinite(atm_iv) else pd.DataFrame()

        raw_call_curve = exp_rows[exp_rows["option_type"] == "call"].loc[:, ["strike", "iv"]]
        raw_put_curve = exp_rows[exp_rows["option_type"] == "put"].loc[:, ["strike", "iv"]]
        call_side = raw_call_curve[raw_call_curve["strike"] > atm_strike]
        put_side = raw_put_curve[raw_put_curve["strike"] < atm_strike]

        call_curve = smooth_iv_curve(pd.concat([atm_point, call_side], ignore_index=True).dropna())
        put_curve = smooth_iv_curve(pd.concat([put_side, atm_point], ignore_index=True).dropna())
        iv_curve = smooth_iv_curve(pd.concat([put_side, atm_point, call_side], ignore_index=True).dropna())
        if call_curve.empty and put_curve.empty and iv_curve.empty:
            continue
        curves.append(
            {
                "expiry": expiry,
                "dte": expiry_dte[expiry],
                "atm_iv": atm_iv,
                "call": call_curve,
                "put": put_curve,
                "iv": iv_curve,
                "color": TENOR_COLORS[i % len(TENOR_COLORS)],
            }
        )
    return curves


def build_volatility_skew_chart(
    data: pd.DataFrame, spot: float, summary: dict, input_dir: Path | None = None
) -> go.Figure:
    if input_dir is not None:
        ticker = summary.get("ticker") or ""
        raw, capture_ts = load_multi_tenor_skew(input_dir.parent, ticker)
        if not raw.empty:
            # See live_server.skew_tenors_payload: DTE must be measured from
            # when this multi-tenor snapshot was captured, not from the
            # single-expiry summary's (possibly stale) effective_snapshot_date.
            effective_day = (
                str(capture_ts)[:10]
                if capture_ts
                else summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or ""
            )
            tenors = build_tenor_curves(raw, spot, effective_day)
            if len(tenors) >= 1:
                return build_multi_tenor_skew_chart(tenors, spot)
    return build_single_expiry_skew_chart(data, spot, summary)


def build_multi_tenor_skew_chart(tenors: list[dict], spot: float) -> go.Figure:
    fig = go.Figure()
    labels = []
    for tenor in tenors:
        dte = tenor["dte"]
        name = f"{dte}DTE" if dte is not None else tenor["expiry"]
        atm_iv_pct = tenor["atm_iv"] * 100 if np.isfinite(tenor["atm_iv"]) else float("nan")
        if np.isfinite(atm_iv_pct):
            labels.append(f"{name} {atm_iv_pct:.1f}%")
        for side, curve, dash in (("C", tenor["call"], "solid"), ("P", tenor["put"], "dash"), ("IV", tenor["iv"], "dot")):
            if curve.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=curve["strike"],
                    y=curve["iv"] * 100,
                    mode="lines",
                    line=dict(color=tenor["color"], width=2, dash=dash),
                    name=f"{name} {side}",
                    showlegend=False,
                    hovertemplate=(
                        "<span style='color:#94A3B8'>strike %{x:g}</span><br>"
                        f"<span style='color:{tenor['color']}'>{name} {side}</span> "
                        "%{y:.1f}% IV<extra></extra>"
                    ),
                )
            )
    fig.add_vline(x=spot, line_dash="dot", line_color=SPOT_COLOR)

    if labels:
        fig.add_annotation(
            x=0,
            y=1.16,
            xref="paper",
            yref="paper",
            text=" · ".join(labels),
            showarrow=False,
            font=dict(color=MUTED, size=11),
            xanchor="left",
        )

    layout = chart_layout("Volatility Skew", height=360)
    layout["yaxis"].update(title="IV %")
    layout["xaxis"].update(title="Strike")
    fig.update_layout(**layout)
    return fig


def build_single_expiry_skew_chart(data: pd.DataFrame, spot: float, summary: dict) -> go.Figure:
    skew = data.sort_values("strike").copy()
    for side, iv_col in (("call", "call_iv"), ("put", "put_iv")):
        if iv_col not in skew:
            skew[iv_col] = np.nan
        skew[iv_col] = pd.to_numeric(skew[iv_col], errors="coerce")
        skew.loc[(skew[iv_col] <= 0) | (skew[iv_col] > 5), iv_col] = np.nan
        oi_col, vol_col = f"{side}_oi", f"{side}_volume"
        oi = pd.to_numeric(skew.get(oi_col, 0.0), errors="coerce").fillna(0.0)
        volume = pd.to_numeric(skew.get(vol_col, 0.0), errors="coerce").fillna(0.0)
        # Strikes with no open interest and no traded volume have no real quote behind
        # them; their IV is a stale/extrapolated BSM back-solve, not a market price.
        skew.loc[(oi <= 0) & (volume <= 0), iv_col] = np.nan
    atm = nearest_row(skew, spot)
    atm_strike = float(atm["strike"]) if atm is not None else float(spot)
    atm_candidates = []
    if atm is not None:
        for col in ["call_iv", "put_iv"]:
            value = atm.get(col)
            if pd.notna(value) and np.isfinite(float(value)):
                atm_candidates.append(float(value))
    atm_iv = float(np.mean(atm_candidates)) if atm_candidates else float("nan")

    put_side = skew[skew["strike"] < atm_strike].loc[:, ["strike", "put_iv"]].rename(columns={"put_iv": "iv"})
    call_side = skew[skew["strike"] > atm_strike].loc[:, ["strike", "call_iv"]].rename(columns={"call_iv": "iv"})
    atm_point = pd.DataFrame([{"strike": atm_strike, "iv": atm_iv}]) if np.isfinite(atm_iv) else pd.DataFrame()
    put_curve = pd.concat([put_side, atm_point], ignore_index=True).dropna().sort_values("strike")
    call_curve = pd.concat([atm_point, call_side], ignore_index=True).dropna().sort_values("strike")
    put_curve = smooth_iv_curve(put_curve)
    call_curve = smooth_iv_curve(call_curve)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=call_curve["strike"],
            y=call_curve["iv"] * 100,
            mode="lines",
            line=dict(color=CYAN, width=2),
            name="Calls",
            hovertemplate="<span style='color:#94A3B8'>strike %{x:g}</span><br><span style='color:#FACC15'>0DTE</span> C %{y:.1f}% IV %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=put_curve["strike"],
            y=put_curve["iv"] * 100,
            mode="lines",
            line=dict(color=ORANGE, width=2, dash="dash"),
            name="Puts",
            hovertemplate="<span style='color:#94A3B8'>strike %{x:g}</span><br><span style='color:#FACC15'>0DTE</span> P %{y:.1f}% IV %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_vline(x=spot, line_dash="dot", line_color=SPOT_COLOR)

    call_atm = float(atm["call_iv"] * 100) if atm is not None and pd.notna(atm.get("call_iv")) else float("nan")
    put_atm = float(atm["put_iv"] * 100) if atm is not None and pd.notna(atm.get("put_iv")) else float("nan")
    subtitle = f"ATM calls {call_atm:.1f}% · puts {put_atm:.1f}%" if np.isfinite(call_atm) and np.isfinite(put_atm) else ""
    if subtitle:
        fig.add_annotation(
            x=0,
            y=1.16,
            xref="paper",
            yref="paper",
            text=subtitle,
            showarrow=False,
            font=dict(color=MUTED, size=11),
            xanchor="left",
        )

    layout = chart_layout("Volatility Skew", height=360)
    layout["yaxis"].update(title="IV %")
    layout["xaxis"].update(title="Strike")
    fig.update_layout(**layout)
    return fig


def load_iv_history(options_root: Path, ticker: str) -> pd.DataFrame:
    """Local, network-free daily ATM IV/spot history for `ticker`.

    Scans sibling day directories under `options_root` (e.g. data/options/*)
    for each day's canonical "latest" summary file
    (`{TICKER}_{YYYY-MM-DD}_summary.json`), skipping days that don't have an
    IV recorded yet. ATM IV is preferred because whole-chain average IV is too
    easily distorted by far OTM stale/free-data quotes.
    """
    rows = []
    if not options_root.is_dir():
        return pd.DataFrame(rows)
    for day_dir in sorted(options_root.iterdir()):
        if not day_dir.is_dir() or not DATE_DIR_RE.fullmatch(day_dir.name):
            continue
        summary_path = day_dir / f"{ticker.upper()}_{day_dir.name}_summary.json"
        if not summary_path.exists():
            continue
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        spot = float(data.get("spot", np.nan))
        iv_value = None
        by_strike_path = day_dir / f"{ticker.upper()}_{day_dir.name}_by_strike.parquet"
        if by_strike_path.exists() and np.isfinite(spot):
            try:
                by_strike = pd.read_parquet(by_strike_path)
                atm_pct = nearest_atm_iv(by_strike, spot)
                if atm_pct is not None:
                    iv_value = atm_pct / 100
            except Exception:
                pass
        if iv_value is None:
            iv_value = atm_iv_from_yahoo_raw(day_dir, ticker, spot)
        if iv_value is None:
            summary_iv = data.get("avg_iv")
            if summary_iv is not None and np.isfinite(float(summary_iv)) and float(summary_iv) <= 0.8:
                iv_value = float(summary_iv)
        if iv_value is None:
            continue
        rows.append(
            {
                "date": day_dir.name,
                "iv": float(iv_value) * 100,
                "spot": spot,
            }
        )
    return pd.DataFrame(rows)


def build_iv_rank_chart(input_dir: Path, summary: dict, latest_by_strike: pd.DataFrame) -> go.Figure:
    ticker = summary["ticker"]
    current_spot = float(summary.get("spot") or np.nan)
    current_iv = nearest_atm_iv(latest_by_strike, current_spot)
    if current_iv is None:
        current_iv = float(summary.get("avg_iv") or np.nan) * 100
    fig = go.Figure()

    hist = load_iv_history(input_dir.parent, ticker)
    tickvals = None
    ticktext = None
    visible = None
    rank = float("nan")

    if len(hist) >= 2:
        hist = hist.copy()
        hist["date_dt"] = pd.to_datetime(hist["date"], errors="coerce")
        hist["iv_rank"] = rolling_iv_rank(hist["iv"], window=60)
        visible = hist.tail(60).copy()
        tickvals = visible["date_dt"]
        ticktext = visible["date_dt"].dt.strftime("%b %d")
        rank_valid = visible["iv_rank"].dropna()
        rank = float(rank_valid.iloc[-1]) if not rank_valid.empty else float("nan")

    if visible is not None and np.isfinite(rank):
        fig.add_hrect(y0=0, y1=20, fillcolor="rgba(34,211,238,0.10)", line_width=0)
        fig.add_hrect(y0=80, y1=100, fillcolor="rgba(245,158,11,0.12)", line_width=0)
        hover_labels = visible["date_dt"].dt.strftime("%b %d")
        fig.add_trace(
            go.Scatter(
                x=visible["date_dt"],
                y=visible["iv_rank"],
                customdata=[
                    [float(iv), float(spot_value), float(rank_value), str(label)]
                    for iv, spot_value, rank_value, label in zip(visible["iv"], visible["spot"], visible["iv_rank"], hover_labels)
                ],
                mode="lines",
                line=dict(color=CYAN, width=2),
                name="IV Rank",
                hovertemplate="%{customdata[3]}<extra></extra>",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=visible["date_dt"],
                y=visible["spot"],
                mode="lines",
                line=dict(color=SPOT_COLOR, width=1.5, dash="dot"),
                name="Spot",
                yaxis="y2",
                hoverinfo="skip",
            )
        )
        fig.add_annotation(
            x=0,
            y=1.16,
            xref="paper",
            yref="paper",
            text=f"rank {rank:.1f}% · IV {current_iv:.1f}% · ${current_spot:.2f}",
            showarrow=False,
            font=dict(color=CYAN, size=11),
            xanchor="left",
        )
        layout = chart_layout("IV Rank", height=360)
        layout["yaxis"].update(title="IV Rank %", range=[0, 100])
        layout["yaxis2"] = dict(title="Spot", overlaying="y", side="right", color=MUTED, showgrid=False)
    elif visible is not None:
        # Too few sessions collected for a statistically meaningful rank
        # (min-max over a handful of points swings wildly between 0 and
        # 100). Show the raw IV trend instead of a misleading percentage.
        collected = len(hist)
        fig.add_trace(
            go.Scatter(
                x=visible["date_dt"],
                y=visible["iv"],
                mode="lines+markers",
                line=dict(color=CYAN, width=2),
                marker=dict(size=5),
                name="ATM IV",
                hovertemplate="%{x|%b %d}: %{y:.1f}%<extra></extra>",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=visible["date_dt"],
                y=visible["spot"],
                mode="lines",
                line=dict(color=SPOT_COLOR, width=1.5, dash="dot"),
                name="Spot",
                yaxis="y2",
                hoverinfo="skip",
            )
        )
        fig.add_annotation(
            x=0,
            y=1.16,
            xref="paper",
            yref="paper",
            text=f"rank warming up ({collected}/{MIN_IV_RANK_SESSIONS} sessions) · IV {current_iv:.1f}% · ${current_spot:.2f}",
            showarrow=False,
            font=dict(color=MUTED, size=11),
            xanchor="left",
        )
        layout = chart_layout("IV Rank", height=360)
        iv_max = float(visible["iv"].max()) if not visible["iv"].empty else 30.0
        layout["yaxis"].update(title="ATM IV %", range=[0, max(30.0, iv_max * 1.3)])
        layout["yaxis2"] = dict(title="Spot", overlaying="y", side="right", color=MUTED, showgrid=False)
    else:
        fig.add_annotation(
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            text="Not enough local daily snapshots yet for IV Rank (need 2+ days)",
            showarrow=False,
            font=dict(color=MUTED, size=13),
        )
        layout = chart_layout("IV Rank", height=360)
        layout["yaxis"].update(title="Avg IV %", range=[0, 100])

    layout["dragmode"] = False
    layout["xaxis"].update(
        title=None,
        tickformat="%b %d",
        tickangle=0,
        nticks=6,
        fixedrange=True,
    )
    if tickvals is not None and ticktext is not None:
        layout["xaxis"].update(tickmode="array", tickvals=tickvals, ticktext=ticktext)
    layout["yaxis"].update(fixedrange=True)
    layout["yaxis2"] = {**layout.get("yaxis2", {}), "fixedrange": True}
    fig.update_layout(**layout)
    return fig


def build_volatility_flow_chart(input_dir: Path, summary: dict, latest_by_strike: pd.DataFrame) -> go.Figure:
    ticker = summary["ticker"]
    expiry = summary["expiry"]
    snapshot_day = str(summary.get("requested_snapshot_date") or summary.get("effective_snapshot_date") or "")
    entries = []
    index_path = input_dir / "replay_index.jsonl"
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("ticker") == ticker and entry.get("expiry") == expiry:
                entries.append(entry)

    rows = []
    for entry in entries:
        try:
            summary_path = input_dir / entry["summary"]
            by_strike_path = input_dir / entry["by_strike"]
            snap_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            snap_strikes = pd.read_parquet(by_strike_path)
            spot = float(snap_summary["spot"])
            atm = nearest_row(snap_strikes, spot)
            atm_iv = float(atm["iv"] * 100) if atm is not None and pd.notna(atm.get("iv")) else np.nan
            ts = str(entry.get("timestamp", ""))
            label = f"{ts[0:2]}:{ts[2:4]}:{ts[4:6]}" if len(ts) >= 6 else ts
            stamp = pd.to_datetime(f"{snapshot_day} {label}", errors="coerce")
            rows.append(
                {
                    "time": stamp if pd.notna(stamp) else label,
                    "atm_iv": atm_iv,
                    "avg_iv": float(snap_summary.get("avg_iv") or np.nan) * 100,
                    "spot": spot,
                    "net_gex": float(snap_summary.get("net_gex") or 0.0),
                }
            )
        except Exception:
            continue

    if not rows:
        spot = float(summary["spot"])
        atm = nearest_row(latest_by_strike, spot)
        rows.append(
            {
                "time": pd.to_datetime(summary.get("snapshot_utc", ""), errors="coerce"),
                "atm_iv": float(atm["iv"] * 100) if atm is not None and pd.notna(atm.get("iv")) else np.nan,
                "avg_iv": float(summary.get("avg_iv") or np.nan) * 100,
                "spot": spot,
                "net_gex": float(summary.get("net_gex") or 0.0),
            }
        )

    flow = pd.DataFrame(rows)
    flow["arv"] = compute_arv_pct(flow)
    flow["flow_area"] = (flow["arv"] - flow["atm_iv"]) * 1000
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=flow["time"],
            y=flow["atm_iv"],
            mode="lines",
            line=dict(color=CYAN, width=1.7),
            name="ATM IV",
            hovertemplate="%{x}<br>ATM IV %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=flow["time"],
            y=flow["arv"],
            mode="lines",
            line=dict(color=ORANGE, width=1.5),
            name="ARV",
            hovertemplate="%{x}<br>ARV %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=flow["time"],
            y=flow["spot"],
            mode="lines",
            line=dict(color=SPOT_COLOR, width=1.4),
            name="Spot",
            yaxis="y2",
            hovertemplate="%{x}<br>Spot $%{y:.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=flow["time"],
            y=flow["flow_area"],
            mode="lines",
            line=dict(color=ORANGE, width=1),
            fill="tozeroy",
            fillcolor="rgba(245,158,11,0.30)",
            name="ARV - IV",
            yaxis="y3",
            hoverinfo="skip",
            showlegend=False,
        )
    )

    layout = chart_layout("Volatility Flow", height=390)
    layout["xaxis"].update(title=None, tickformat="%H:%M", tickangle=0, nticks=9, anchor="y3")
    layout["legend"] = dict(orientation="h", x=0.005, xanchor="left", y=1.12, bgcolor="rgba(0,0,0,0)")
    layout["yaxis"].update(title="IV %", domain=[0.28, 1])
    layout["yaxis2"] = dict(title="Spot", overlaying="y", side="right", color=MUTED, showgrid=False, domain=[0.28, 1])
    layout["yaxis3"] = dict(title="", domain=[0, 0.22], color=MUTED, showgrid=False, zeroline=False)
    latest = flow.dropna(subset=["atm_iv", "arv"]).tail(1)
    if not latest.empty:
        fig.add_annotation(
            x=0,
            y=1.16,
            xref="paper",
            yref="paper",
            text=f"<span style='color:{CYAN}'>ATM IV {latest['atm_iv'].iloc[0]:.1f}%</span> · <span style='color:{ORANGE}'>ARV {latest['arv'].iloc[0]:.1f}%</span>",
            showarrow=False,
            xanchor="left",
            font=dict(size=11),
        )
    if len(flow) < 2:
        fig.add_annotation(
            x=0.5,
            y=0.08,
            xref="paper",
            yref="paper",
            text="Needs multiple same-day snapshots for a full flow line",
            showarrow=False,
            font=dict(color=MUTED, size=11),
        )
    fig.update_layout(**layout)
    return fig


def render_key_level_cards(mode: str, summary: dict, effective: str) -> str:
    if mode == "gex":
        cards = [
            ("CALL RESISTANCE", summary.get("call_resistance"), LEVEL_COLORS["CALL RESISTANCE"]),
            ("PUT SUPPORT", summary.get("put_support"), LEVEL_COLORS["PUT SUPPORT"]),
            ("GAMMA WALL", summary.get("gamma_wall_abs"), LEVEL_COLORS["GAMMA WALL"]),
            ("GAMMA FLIP", summary.get("gamma_flip"), LEVEL_COLORS["GAMMA FLIP"]),
            ("CALL RES 0DTE", summary.get("call_resistance"), LEVEL_COLORS["CALL RESISTANCE"]),
            ("PUT SUP 0DTE", summary.get("put_support"), LEVEL_COLORS["PUT SUPPORT"]),
            ("GAMMA WALL 0DTE", summary.get("gamma_wall_abs"), LEVEL_COLORS["GAMMA WALL"]),
            ("GAMMA FLIP 0DTE", summary.get("gamma_flip"), LEVEL_COLORS["GAMMA FLIP"]),
        ]
    else:
        cards = [
            ("PUT SUPPORT", summary.get("put_support"), LEVEL_COLORS["PUT SUPPORT"]),
            ("DELTA FLIP", summary.get("delta_flip"), LEVEL_COLORS["DELTA FLIP"]),
            ("CALL RESISTANCE", summary.get("call_resistance"), LEVEL_COLORS["CALL RESISTANCE"]),
            ("DELTA WALL", summary.get("dex_wall_abs"), LEVEL_COLORS["DELTA WALL"]),
            ("PUT SUP 0DTE", summary.get("put_support"), LEVEL_COLORS["PUT SUPPORT"]),
            ("DELTA FLIP 0DTE", summary.get("delta_flip"), LEVEL_COLORS["DELTA FLIP"]),
            ("CALL RES 0DTE", summary.get("call_resistance"), LEVEL_COLORS["CALL RESISTANCE"]),
            ("DELTA WALL 0DTE", summary.get("dex_wall_abs"), LEVEL_COLORS["DELTA WALL"]),
        ]
    html_cards = []
    for label, value, color in cards:
        value_text = f"${value:g}" if value is not None and not pd.isna(value) else "NA"
        html_cards.append(
            f'<div class="card"><div class="card-label">{label}</div>'
            f'<div class="card-value" style="color:{color}">{value_text}</div>'
            f'<div class="card-date">{effective}</div></div>'
        )
    return "\n".join(html_cards)


def render_dealer_balance(mode: str, summary: dict) -> str:
    if mode == "gex":
        positive = summary.get("total_call_gex") or 0.0
        negative = abs(summary.get("total_put_gex") or 0.0)
    else:
        positive = summary.get("total_call_dex") or 0.0
        negative = abs(summary.get("total_put_dex") or 0.0)
    total = positive + negative
    green_pct = (positive / total * 100) if total else 50.0
    return (
        f'<div class="balance-labels">'
        f'<span style="color:{GREEN}">+{compact(positive)}</span>'
        f'<span style="color:{PURPLE}">-{compact(negative)}</span></div>'
        f'<div class="balance-bar">'
        f'<div class="balance-fill" style="width:{green_pct:.1f}%"></div></div>'
    )


PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{ticker} Exposure</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: {bg}; color: {text};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .topbar {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 22px; border-bottom: 1px solid {grid};
  }}
  .meta {{ color: {muted}; font-size: 13px; }}
  .meta b {{ color: {text}; }}
  .panel-grid {{ padding: 16px; display: flex; flex-direction: column; gap: 16px; }}
  .panel-row {{ display: flex; gap: 16px; }}
  .panel-row > .panel {{ flex: 1; min-width: 0; }}
  .panel {{
    background: {panel}; border: 1px solid {grid}; border-radius: 10px;
    overflow: hidden; display: flex; flex-direction: column;
  }}
  .panel-header {{
    display: flex; align-items: center; gap: 8px; padding: 10px 14px;
    border-bottom: 1px solid {grid}; font-size: 12px; font-weight: 600;
    letter-spacing: 0.03em; color: {text};
  }}
  .panel-header .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
  .panel-body {{ padding: 6px 10px 10px; }}
  .panel-sub {{ padding: 0 14px 14px; }}
  .panel-wide {{ width: 100%; }}
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }}
  .card {{
    background: {card}; border: 1px solid {grid}; border-radius: 8px; padding: 10px 12px;
  }}
  .card-label {{ font-size: 10px; color: {muted}; letter-spacing: 0.05em; }}
  .card-value {{ font-size: 18px; font-weight: 700; margin-top: 2px; }}
  .card-date {{ font-size: 10px; color: {muted}; margin-top: 2px; }}
  .balance-labels {{ display: flex; justify-content: space-between; font-size: 13px; font-weight: 700; }}
  .balance-bar {{
    height: 8px; border-radius: 4px; background: {purple}; margin-top: 6px; overflow: hidden;
  }}
  .balance-fill {{ height: 100%; background: {green}; }}
  .export-line {{
    margin: 0; padding: 12px 14px; background: #05070B; border: 1px solid {grid};
    border-radius: 8px; color: {text}; font-family: Menlo, Consolas, monospace;
    font-size: 13px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere;
  }}
</style>
</head>
<body>
<div class="topbar">
  <div class="meta"><b>{ticker}</b> &middot; 0DTE &middot; expiry {expiry} &middot; snapshot {snapshot}</div>
</div>
<div class="panel-grid">
    <div class="panel panel-wide">
      <div class="panel-header"><span class="dot" style="background:{cyan}"></span>Volatility Flow</div>
      <div class="panel-body">{vol_flow_chart_html}</div>
    </div>
    <div class="panel-row">
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:{cyan}"></span>IV Rank</div>
        <div class="panel-body">{iv_rank_chart_html}</div>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:{yellow}"></span>Volatility Skew</div>
        <div class="panel-body">{vol_skew_chart_html}</div>
      </div>
    </div>
    <div class="panel-row">
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:{exposure_pos}"></span>OI &times; IV by Strike</div>
        <div class="panel-body">{oiiv_chart_html}</div>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:{exposure_neg}"></span>OI by Strike</div>
        <div class="panel-body">{oi_chart_html}</div>
      </div>
    </div>
    <div class="panel-row">
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:{green}"></span>GEX Exposure</div>
        <div class="panel-body">{gex_chart_html}</div>
        <div class="panel-sub">
          <div class="cards">{gex_cards}</div>
          <div class="balance">{gex_balance}</div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:{purple}"></span>DEX Exposure</div>
        <div class="panel-body">{dex_chart_html}</div>
        <div class="panel-sub">
          <div class="cards">{dex_cards}</div>
          <div class="balance">{dex_balance}</div>
        </div>
      </div>
    </div>
    <div class="panel panel-wide">
      <div class="panel-header"><span class="dot" style="background:{yellow}"></span>Levels Export</div>
      <div class="panel-body"><pre class="export-line">{levels_export}</pre></div>
    </div>
</div>
<script>
  window.addEventListener("load", function() {{
    ["vol-flow-plot", "iv-rank-plot", "vol-skew-plot", "gex-plot", "dex-plot", "oiiv-plot", "oi-plot"].forEach(function(id) {{
      var gd = document.getElementById(id);
      if (gd && window.Plotly) {{ Plotly.Plots.resize(gd); }}
    }});
    var ivRank = document.getElementById("iv-rank-plot");
    if (ivRank && window.Plotly && ivRank.on && ivRank.data && ivRank.data[0]) {{
      var currentText = ivRank.layout.annotations && ivRank.layout.annotations[0] ? ivRank.layout.annotations[0].text : "";
      function headerFromCustom(customdata) {{
        if (!customdata) {{ return currentText; }}
        return "rank " + Number(customdata[2] || 0).toFixed(1) + "% · IV " + Number(customdata[0] || 0).toFixed(1) + "% · $" + Number(customdata[1] || 0).toFixed(2);
      }}
      ivRank.on("plotly_hover", function(ev) {{
        var point = (ev.points || []).find(function(p) {{ return p.curveNumber === 0; }}) || (ev.points || [])[0];
        if (point && point.customdata) {{
          Plotly.relayout(ivRank, {{"annotations[0].text": headerFromCustom(point.customdata)}});
        }}
      }});
      ivRank.on("plotly_unhover", function() {{
        Plotly.relayout(ivRank, {{"annotations[0].text": currentText}});
      }});
    }}
  }});
</script>
</body>
</html>
"""


def render(
    summary: dict,
    by_strike: pd.DataFrame,
    input_dir: Path,
    output: Path,
    window: float,
    top: int,
) -> None:
    spot = float(summary["spot"])
    data = prepare_data(by_strike, spot, window, top)

    gex_fig = build_chart("gex", summary, data, spot)
    dex_fig = build_chart("dex", summary, data, spot)
    oiiv_fig = build_oi_iv_chart(data, spot)
    oi_fig = build_oi_chart(data, spot)
    vol_flow_fig = build_volatility_flow_chart(input_dir, summary, by_strike)
    iv_rank_fig = build_iv_rank_chart(input_dir, summary, by_strike)
    vol_skew_fig = build_volatility_skew_chart(data, spot, summary, input_dir)

    config = {
        "displaylogo": False,
        "displayModeBar": False,
        "scrollZoom": True,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    }
    no_zoom_config = {
        "displaylogo": False,
        "displayModeBar": False,
        "scrollZoom": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    }
    gex_html = gex_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="gex-plot")
    dex_html = dex_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="dex-plot")
    oiiv_html = oiiv_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="oiiv-plot")
    oi_html = oi_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="oi-plot")
    vol_flow_html = vol_flow_fig.to_html(full_html=False, include_plotlyjs="cdn", config=config, div_id="vol-flow-plot")
    iv_rank_html = iv_rank_fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config=no_zoom_config,
        div_id="iv-rank-plot",
    )
    vol_skew_html = vol_skew_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="vol-skew-plot")

    effective = summary.get("effective_snapshot_date", summary.get("requested_snapshot_date", ""))
    requested = summary.get("requested_snapshot_date", "")
    snapshot_text = effective if effective == requested else f"{requested} (effective {effective})"

    page = PAGE_TEMPLATE.format(
        bg=BG,
        text=TEXT,
        muted=MUTED,
        grid=GRID,
        card=CARD,
        panel=PANEL,
        green=GREEN,
        purple=PURPLE,
        cyan=CYAN,
        yellow=YELLOW,
        exposure_pos=EXPOSURE_POS,
        exposure_neg=EXPOSURE_NEG,
        ticker=summary["ticker"],
        expiry=summary["expiry"],
        snapshot=snapshot_text,
        gex_chart_html=gex_html,
        dex_chart_html=dex_html,
        oiiv_chart_html=oiiv_html,
        oi_chart_html=oi_html,
        vol_flow_chart_html=vol_flow_html,
        iv_rank_chart_html=iv_rank_html,
        vol_skew_chart_html=vol_skew_html,
        gex_cards=render_key_level_cards("gex", summary, effective),
        dex_cards=render_key_level_cards("dex", summary, effective),
        gex_balance=render_dealer_balance("gex", summary),
        dex_balance=render_dealer_balance("dex", summary),
        levels_export=build_levels_line(summary),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


def main() -> None:
    args = parse_args()
    summary, by_strike = load_data(args.input_dir, args.ticker, args.expiry)
    output = args.output or args.input_dir / f"{summary['ticker']}_{summary['expiry']}_interactive.html"
    render(summary, by_strike, args.input_dir, output, args.window, args.top)
    print(f"Saved interactive dashboard: {output}")


if __name__ == "__main__":
    main()
