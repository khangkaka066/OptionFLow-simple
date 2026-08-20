#!/usr/bin/env python3
"""Replay a day's GEX snapshots as an animated Plotly dashboard with a time slider.

Reads `replay_index.jsonl` (appended once per run by daily_qqq_snapshot.py) and
builds one frame per snapshot so you can step/animate through how GEX-by-strike
changed intraday.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import storage
from render_gex_interactive import BG, GREEN, GRID, MUTED, PANEL, PURPLE, TEXT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "options"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an animated intraday GEX replay HTML.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--expiry", required=True)
    parser.add_argument(
        "--date",
        default=None,
        help="Snapshot date YYYY-MM-DD (the data/options/<date> folder). Defaults to today.",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--window", type=float, default=14)
    parser.add_argument("--top", type=int, default=40)
    return parser.parse_args()


def prepare_frame_data(by_strike: pd.DataFrame, spot: float, window: float, top: int) -> pd.DataFrame:
    data = by_strike.copy()
    data = data[np.isfinite(data["strike"])].copy()
    in_window = data[(data["strike"] >= spot - window) & (data["strike"] <= spot + window)]
    if len(in_window) < 12:
        in_window = data.sort_values("abs_net_gex", ascending=False).head(top)
    return in_window.sort_values("strike")


def load_snapshots(output_dir: Path, ticker: str, expiry: str) -> list[dict]:
    entries = storage.read_replay_index(output_dir, ticker, expiry)
    snapshots = []
    for entry in entries:
        summary_path = output_dir / entry["summary"]
        by_strike_path = output_dir / entry["by_strike"]
        if not summary_path.exists() or not by_strike_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        by_strike = pd.read_parquet(by_strike_path)
        snapshots.append({"timestamp": entry["timestamp"], "summary": summary, "by_strike": by_strike})
    return snapshots


def build_replay(snapshots: list[dict], window: float, top: int) -> go.Figure:
    frames = []
    steps = []
    for snap in snapshots:
        summary = snap["summary"]
        spot = float(summary["spot"])
        data = prepare_frame_data(snap["by_strike"], spot, window, top)
        colors = np.where(data["net_gex"] >= 0, GREEN, PURPLE)
        hhmmss = snap["timestamp"]
        pretty_time = f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"

        frames.append(
            go.Frame(
                data=[
                    go.Bar(
                        x=data["net_gex"],
                        y=data["strike"],
                        orientation="h",
                        marker=dict(color=colors),
                        name="Net GEX",
                    )
                ],
                name=hhmmss,
                layout=go.Layout(
                    title=dict(
                        text=(
                            f"<b>{summary['ticker']} GEX Replay</b> - {pretty_time} NY | spot {spot:.2f} | "
                            f"Gamma Flip {summary.get('gamma_flip')} | Delta Flip {summary.get('delta_flip')}"
                        ),
                        font=dict(color=TEXT, size=20),
                    )
                ),
            )
        )
        steps.append(
            dict(
                method="animate",
                args=[[hhmmss], dict(mode="immediate", frame=dict(duration=0, redraw=True), transition=dict(duration=0))],
                label=pretty_time,
            )
        )

    first = frames[0]
    fig = go.Figure(data=first.data, layout=first.layout, frames=frames)
    fig.update_layout(
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT),
        height=880,
        width=1600,
        margin=dict(l=70, r=35, t=90, b=90),
        xaxis=dict(title="Net GEX", gridcolor=GRID, zerolinecolor="#475569", color=MUTED),
        yaxis=dict(title="Strike", gridcolor=GRID, color=MUTED),
        updatemenus=[
            dict(
                type="buttons",
                x=0.02,
                y=-0.12,
                xanchor="left",
                yanchor="top",
                buttons=[
                    dict(
                        label="Play",
                        method="animate",
                        args=[None, dict(frame=dict(duration=700, redraw=True), fromcurrent=True)],
                    ),
                    dict(
                        label="Pause",
                        method="animate",
                        args=[[None], dict(mode="immediate", frame=dict(duration=0, redraw=False))],
                    ),
                ],
            )
        ],
        sliders=[
            dict(
                active=0,
                x=0.02,
                y=-0.02,
                len=0.96,
                currentvalue=dict(prefix="Snapshot: ", font=dict(color=TEXT)),
                steps=steps,
            )
        ],
    )
    return fig


def main() -> None:
    args = parse_args()
    ticker = args.ticker.upper()
    snapshot_date = args.date or datetime.now().date().isoformat()
    output_dir = args.output_root / snapshot_date

    snapshots = load_snapshots(output_dir, ticker, args.expiry)
    if not snapshots:
        raise SystemExit(f"No replay snapshots found in {output_dir} for {ticker} {args.expiry}.")

    fig = build_replay(snapshots, args.window, args.top)
    output = args.output or output_dir / f"{ticker}_{args.expiry}_replay.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(output, include_plotlyjs="cdn")
    print(f"Saved replay dashboard ({len(snapshots)} snapshots): {output}")


if __name__ == "__main__":
    main()
