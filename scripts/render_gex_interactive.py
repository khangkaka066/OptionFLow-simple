#!/usr/bin/env python3
"""Render an interactive Plotly GEX/DEX dashboard from saved outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go


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

CHART_HEIGHT = 520
CHART_MARGIN_TOP = 20
CHART_MARGIN_BOTTOM = 10

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
            customdata=np.stack([data[call_col], data[put_col], data[abs_col]], axis=-1),
            hovertemplate=(
                f"<b>Strike $%{{y:g}}</b><br>"
                f"{label}: %{{x:,.0f}}<br>"
                f"Call: %{{customdata[0]:,.0f}}<br>"
                f"Put: %{{customdata[1]:,.0f}}<br>"
                f"Abs {label}: %{{customdata[2]:,.0f}}<extra></extra>"
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
        dragmode="zoom",
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
    fig.add_vline(x=spot, line_dash="dot", line_color=SPOT_COLOR, annotation_text=f"SPOT {spot:.2f}", annotation_position="top")

    fig.update_layout(
        title=dict(text="OI × IV by Strike", font=dict(color=TEXT, size=13), x=0),
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT),
        height=380,
        margin=dict(l=70, r=60, t=40, b=45),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(5,7,11,0.9)", font=dict(color=TEXT)),
        dragmode="zoom",
        barmode="group",
        bargap=0.15,
        bargroupgap=0.0,
        showlegend=True,
        legend=dict(orientation="h", y=1.12, x=0),
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
    fig.add_vline(x=spot, line_dash="dot", line_color=SPOT_COLOR, annotation_text=f"SPOT {spot:.2f}", annotation_position="top")

    fig.update_layout(
        title=dict(text="OI by Strike", font=dict(color=TEXT, size=13), x=0),
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT),
        height=340,
        margin=dict(l=70, r=35, t=40, b=45),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="rgba(5,7,11,0.9)", font=dict(color=TEXT)),
        dragmode="zoom",
        barmode="group",
        bargap=0.15,
        bargroupgap=0.0,
        showlegend=True,
        legend=dict(orientation="h", y=1.12, x=0),
        xaxis=dict(title="Strike", gridcolor=GRID, color=MUTED),
        yaxis=dict(title="Open Interest", gridcolor=GRID, color=MUTED),
    )
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
</style>
</head>
<body>
<div class="topbar">
  <div class="meta"><b>{ticker}</b> &middot; 0DTE &middot; expiry {expiry} &middot; snapshot {snapshot}</div>
</div>
<div class="panel-grid">
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
</div>
<script>
  window.addEventListener("load", function() {{
    ["gex-plot", "dex-plot", "oiiv-plot", "oi-plot"].forEach(function(id) {{
      var gd = document.getElementById(id);
      if (gd && window.Plotly) {{ Plotly.Plots.resize(gd); }}
    }});
  }});
</script>
</body>
</html>
"""


def render(
    summary: dict,
    by_strike: pd.DataFrame,
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

    config = {
        "displaylogo": False,
        "displayModeBar": False,
        "scrollZoom": True,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    }
    gex_html = gex_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="gex-plot")
    dex_html = dex_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="dex-plot")
    oiiv_html = oiiv_fig.to_html(full_html=False, include_plotlyjs="cdn", config=config, div_id="oiiv-plot")
    oi_html = oi_fig.to_html(full_html=False, include_plotlyjs=False, config=config, div_id="oi-plot")

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
        exposure_pos=EXPOSURE_POS,
        exposure_neg=EXPOSURE_NEG,
        ticker=summary["ticker"],
        expiry=summary["expiry"],
        snapshot=snapshot_text,
        gex_chart_html=gex_html,
        dex_chart_html=dex_html,
        oiiv_chart_html=oiiv_html,
        oi_chart_html=oi_html,
        gex_cards=render_key_level_cards("gex", summary, effective),
        dex_cards=render_key_level_cards("dex", summary, effective),
        gex_balance=render_dealer_balance("gex", summary),
        dex_balance=render_dealer_balance("dex", summary),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


def main() -> None:
    args = parse_args()
    summary, by_strike = load_data(args.input_dir, args.ticker, args.expiry)
    output = args.output or args.input_dir / f"{summary['ticker']}_{summary['expiry']}_interactive.html"
    render(summary, by_strike, output, args.window, args.top)
    print(f"Saved interactive dashboard: {output}")


if __name__ == "__main__":
    main()
