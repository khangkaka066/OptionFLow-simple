#!/usr/bin/env python3
"""Local realtime dashboard server for intraday Volatility Flow.

The browser does not reload. A background worker collects snapshots on a
cadence, while the page polls `/api/state` and updates Plotly charts in place.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data" / "options"


HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QQQ Live Volatility Flow</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #05070B; color: #E5E7EB;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .topbar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 18px; border-bottom: 1px solid #1F2937;
    position: sticky; top: 0; background: #05070B; z-index: 2;
  }
  .title { font-weight: 700; font-size: 18px; }
  .meta { color: #94A3B8; font-size: 13px; }
  .grid { padding: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .panel {
    background: #090D15; border: 1px solid #1F2937; border-radius: 10px;
    overflow: hidden;
  }
  .panel.wide { grid-column: 1 / -1; }
  .panel-header {
    padding: 11px 14px; border-bottom: 1px solid #1F2937;
    font-weight: 700; display: flex; gap: 10px; align-items: center;
  }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #22D3EE; }
  .body { padding: 10px 12px; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .card { background: #10151F; border: 1px solid #1F2937; border-radius: 8px; padding: 12px; }
  .label { color: #94A3B8; font-size: 11px; letter-spacing: .04em; text-transform: uppercase; }
  .value { font-size: 20px; font-weight: 800; margin-top: 4px; }
  .ok { color: #4ADE80; }
  .warn { color: #FACC15; }
  .bad { color: #F59E0B; }
  .muted { color: #94A3B8; }
  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
    .cards { grid-template-columns: 1fr 1fr; }
  }
</style>
</head>
<body>
<div class="topbar">
  <div>
    <div class="title">QQQ Live Volatility Flow</div>
    <div class="meta" id="status">Starting...</div>
  </div>
  <div class="meta" id="clock"></div>
</div>
<div class="grid">
  <div class="panel wide">
    <div class="panel-header"><span class="dot"></span>Volatility Flow</div>
    <div class="body"><div id="flow" style="height:430px;"></div></div>
  </div>
  <div class="panel wide">
    <div class="panel-header"><span class="dot" style="background:#FACC15"></span>Current Snapshot</div>
    <div class="body"><div class="cards" id="cards"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot"></span>Net GEX</div>
    <div class="body"><div id="gex" style="height:340px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot" style="background:#F59E0B"></span>Net DEX</div>
    <div class="body"><div id="dex" style="height:340px;"></div></div>
  </div>
</div>
<script>
const COLORS = {
  bg: "#05070B", panel: "#090D15", grid: "#1F2937", text: "#E5E7EB",
  muted: "#94A3B8", cyan: "#22D3EE", yellow: "#FACC15", spot: "#CBD5E1",
  orange: "#F59E0B", green: "#4ADE80"
};

function compact(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "NA";
  const sign = Number(v) < 0 ? "-" : "";
  const n = Math.abs(Number(v));
  if (n >= 1e9) return sign + (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return sign + (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return sign + (n / 1e3).toFixed(2) + "K";
  return sign + n.toFixed(0);
}

function baseLayout(height) {
  return {
    paper_bgcolor: COLORS.bg,
    plot_bgcolor: COLORS.panel,
    font: {color: COLORS.text},
    margin: {l: 65, r: 60, t: 30, b: 45},
    height,
    hovermode: "x unified",
    legend: {orientation: "h", x: 1, xanchor: "right", y: 1.12},
    xaxis: {gridcolor: COLORS.grid, color: COLORS.muted, tickformat: "%H:%M", nticks: 10},
    yaxis: {gridcolor: COLORS.grid, color: COLORS.muted},
  };
}

function barLayout(height, title) {
  return {
    paper_bgcolor: COLORS.bg,
    plot_bgcolor: COLORS.panel,
    font: {color: COLORS.text},
    margin: {l: 65, r: 30, t: 20, b: 25},
    height,
    showlegend: false,
    xaxis: {visible: false, range: null},
    yaxis: {gridcolor: COLORS.grid, color: COLORS.muted, title: null},
  };
}

function drawFlow(points) {
  const x = points.map(p => p.time);
  const flowData = [
    {x, y: points.map(p => p.atm_iv), type: "scatter", mode: "lines+markers", name: "ATM IV", line: {color: COLORS.cyan, width: 2}},
    {x, y: points.map(p => p.avg_iv), type: "scatter", mode: "lines+markers", name: "Avg IV", line: {color: COLORS.yellow, width: 2}},
    {x, y: points.map(p => p.spot), type: "scatter", mode: "lines+markers", name: "Spot", yaxis: "y2", line: {color: COLORS.spot, width: 2}},
  ];
  const layout = baseLayout(430);
  layout.yaxis.title = "IV %";
  layout.yaxis2 = {title: "Spot", overlaying: "y", side: "right", color: COLORS.muted, showgrid: false};
  Plotly.react("flow", flowData, layout, {displayModeBar: false, scrollZoom: false, responsive: true});
}

function drawExposure(id, rows, key) {
  const values = rows.map(r => r[key]);
  const maxAbs = Math.max(1, ...values.map(v => Math.abs(Number(v) || 0)));
  const data = [{
    x: values,
    y: rows.map(r => r.strike),
    type: "bar",
    orientation: "h",
    marker: {color: values.map(v => v >= 0 ? COLORS.cyan : COLORS.orange)},
    hovertemplate: "Strike %{y}<br>%{x:,.0f}<extra></extra>"
  }];
  const layout = barLayout(340);
  layout.xaxis.range = [-maxAbs * 1.12, maxAbs * 1.12];
  layout.shapes = [{type: "line", x0: 0, x1: 0, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: "#475569", dash: "dot"}}];
  Plotly.react(id, data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function drawCards(summary, state) {
  const items = [
    ["Spot", summary?.spot ? "$" + Number(summary.spot).toFixed(2) : "NA", "ok"],
    ["Avg IV", summary?.avg_iv ? (summary.avg_iv * 100).toFixed(1) + "%" : "NA", "warn"],
    ["Net GEX", compact(summary?.net_gex), Number(summary?.net_gex) >= 0 ? "ok" : "bad"],
    ["Net DEX", compact(summary?.net_dex), Number(summary?.net_dex) >= 0 ? "ok" : "bad"],
    ["Call Res", summary?.call_resistance ?? "NA", "ok"],
    ["Put Sup", summary?.put_support ?? "NA", "bad"],
    ["Gamma Flip", summary?.gamma_flip ? Number(summary.gamma_flip).toFixed(2) : "NA", "muted"],
    ["Delta Flip", summary?.delta_flip ? Number(summary.delta_flip).toFixed(2) : "NA", "muted"],
  ];
  document.getElementById("cards").innerHTML = items.map(([label, value, cls]) =>
    `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`
  ).join("");
}

async function update() {
  const res = await fetch("/api/state?ts=" + Date.now());
  const state = await res.json();
  const status = [
    state.running ? "Running" : "Stopped",
    `${state.successes} ok / ${state.failures} failed`,
    state.latest_error ? "Last error: " + state.latest_error : null,
    state.next_fetch ? "Next fetch: " + new Date(state.next_fetch).toLocaleTimeString() : null,
  ].filter(Boolean).join(" · ");
  document.getElementById("status").textContent = status;
  document.getElementById("clock").textContent = new Date().toLocaleTimeString();
  drawFlow(state.points || []);
  drawCards(state.latest_summary || {}, state);
  drawExposure("gex", state.by_strike || [], "net_gex");
  drawExposure("dex", state.by_strike || [], "net_dex");
}

update();
setInterval(update, 3000);
</script>
</body>
</html>
"""


class LiveState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.points: list[dict] = []
        self.by_strike: list[dict] = []
        self.latest_summary: dict | None = None
        self.latest_error: str | None = None
        self.running = True
        self.successes = 0
        self.failures = 0
        self.next_fetch: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "points": self.points,
                "by_strike": self.by_strike,
                "latest_summary": self.latest_summary,
                "latest_error": self.latest_error,
                "running": self.running,
                "successes": self.successes,
                "failures": self.failures,
                "next_fetch": self.next_fetch,
            }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local realtime QQQ Volatility Flow dashboard.")
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--expiry", default=None)
    parser.add_argument("--duration-minutes", type=int, default=90)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--window", type=float, default=14)
    parser.add_argument("--rate", type=float, default=0.04)
    parser.add_argument("--top", type=int, default=10)
    return parser.parse_args()


def latest_summary_path(ticker: str) -> Path:
    matches = [
        path
        for path in DATA_ROOT.glob(f"*/{ticker.upper()}_*_summary.json")
        if len(path.stem.split("_")) == 3
    ]
    if not matches:
        raise FileNotFoundError(f"No latest summary found for {ticker}")
    return max(matches, key=lambda path: path.stat().st_mtime)


def nearest_atm_iv(by_strike: pd.DataFrame, spot: float) -> float | None:
    clean = by_strike[np.isfinite(by_strike["strike"])].copy()
    if clean.empty or "iv" not in clean:
        return None
    idx = (clean["strike"] - spot).abs().idxmin()
    iv = clean.loc[idx, "iv"]
    return float(iv) * 100 if pd.notna(iv) else None


def load_latest(ticker: str, window: float) -> tuple[dict, dict, list[dict]]:
    summary_path = latest_summary_path(ticker)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    by_strike_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))
    by_strike = pd.read_parquet(by_strike_path)
    spot = float(summary["spot"])
    atm_iv = nearest_atm_iv(by_strike, spot)
    point = {
        "time": summary.get("snapshot_utc"),
        "atm_iv": atm_iv,
        "avg_iv": float(summary.get("avg_iv") or np.nan) * 100,
        "spot": spot,
    }
    chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)].copy()
    if chart_rows.empty:
        chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
    rows = chart_rows.sort_values("strike")[
        ["strike", "net_gex", "net_dex", "call_oi", "put_oi", "iv"]
    ].replace({np.nan: None}).to_dict(orient="records")
    return summary, point, rows


def run_snapshot(args: argparse.Namespace) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "scripts/run_gex_dashboard.py",
        "--ticker",
        args.ticker.upper(),
        "--rate",
        str(args.rate),
        "--top",
        str(args.top),
        "--window",
        str(args.window),
        "--no-open",
    ]
    if args.expiry:
        cmd += ["--expiry", args.expiry]
    return subprocess.run(cmd, cwd=PROJECT_ROOT, check=False, text=True, capture_output=True)


def collector(args: argparse.Namespace, state: LiveState) -> None:
    total = max(1, int(args.duration_minutes * 60 / args.interval_seconds) + 1)
    next_run = time.monotonic()
    for _ in range(total):
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_fetch_dt = datetime.now() + timedelta(seconds=args.interval_seconds)
        with state.lock:
            state.next_fetch = next_fetch_dt.isoformat()

        result = run_snapshot(args)
        with state.lock:
            if result.returncode != 0:
                state.failures += 1
                tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
                state.latest_error = tail[0] if tail else f"exit {result.returncode}"
            else:
                try:
                    summary, point, rows = load_latest(args.ticker, args.window)
                    if not any(p.get("time") == point.get("time") for p in state.points):
                        state.points.append(point)
                    state.latest_summary = summary
                    state.by_strike = rows
                    state.latest_error = None
                    state.successes += 1
                except Exception as exc:
                    state.failures += 1
                    state.latest_error = str(exc)
        next_run += args.interval_seconds
    with state.lock:
        state.running = False
        state.next_fetch = None


class Handler(BaseHTTPRequestHandler):
    state: LiveState

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send(HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self.send(json.dumps(self.state.snapshot(), default=str), "application/json")
            return
        self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)

    def send(self, body: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    args = parse_args()
    state = LiveState()
    Handler.state = state
    worker = threading.Thread(target=collector, args=(args, state), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Live dashboard: {url}", flush=True)
    print(
        f"Collecting {args.ticker.upper()} every {args.interval_seconds}s "
        f"for {args.duration_minutes} minutes.",
        flush=True,
    )
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with state.lock:
            state.running = False
        server.server_close()


if __name__ == "__main__":
    main()
