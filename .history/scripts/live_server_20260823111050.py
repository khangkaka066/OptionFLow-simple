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
from datetime import datetime, time as dt_time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data" / "options"
IV_RANK_HISTORY_PATH = DATA_ROOT / "iv_rank_history.csv"
NY_TZ = ZoneInfo("America/New_York")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def market_session_utc(now_utc: pd.Timestamp | None = None) -> dict:
    """Today's (NY trading day) 9:30-16:00 ET session window, in UTC."""
    now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
    now_ny = now_utc.tz_convert(NY_TZ)
    trading_date = now_ny.date()
    open_ny = pd.Timestamp.combine(trading_date, dt_time(9, 30)).tz_localize(NY_TZ)
    close_ny = pd.Timestamp.combine(trading_date, dt_time(16, 0)).tz_localize(NY_TZ)
    return {
        "trading_date": trading_date.isoformat(),
        "market_open_utc": open_ny.tz_convert("UTC").isoformat(),
        "market_close_utc": close_ny.tz_convert("UTC").isoformat(),
    }


def collection_start_utc(start_vn: str, now_utc: pd.Timestamp | None = None) -> str:
    now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
    now_vn = now_utc.tz_convert(VN_TZ)
    hour, minute = [int(part) for part in start_vn.split(":", 1)]
    start_vn_ts = pd.Timestamp.combine(now_vn.date(), dt_time(hour, minute)).tz_localize(VN_TZ)
    return start_vn_ts.tz_convert("UTC").isoformat()


HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QQQ Live Option Flow</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {
    --accent-call: #22D3EE;
    --accent-put: #F59E0B;
  }
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
  .top-actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; justify-content: flex-end; }
  .color-controls { display: flex; align-items: center; gap: 10px; }
  .history-control {
    display: inline-flex; align-items: center; gap: 7px; color: #94A3B8;
    font-size: 12px; font-weight: 600;
  }
  .history-control select {
    min-width: 230px; max-width: 300px; height: 28px;
    border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 3px 8px; font-size: 12px;
  }
  .color-control {
    display: inline-flex; align-items: center; gap: 6px; color: #94A3B8;
    font-size: 12px; font-weight: 600;
  }
  .color-control input[type="color"] {
    width: 26px; height: 22px; padding: 0; border: 1px solid #263244;
    border-radius: 5px; background: #0F172A; cursor: pointer;
  }
  .hex-input {
    width: 82px; height: 24px; border: 1px solid #263244; border-radius: 5px;
    background: #0F172A; color: #E5E7EB; padding: 3px 7px;
    font-family: Menlo, Consolas, monospace; font-size: 12px; text-transform: uppercase;
  }
  .hex-input.invalid { border-color: #EF4444; color: #FCA5A5; }
  .grid { padding: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .panel {
    background: #090D15; border: 1px solid #1A2332; border-radius: 10px;
    overflow: hidden;
  }
  .panel.wide { grid-column: 1 / -1; }
  .panel-header {
    padding: 11px 14px; border-bottom: 1px solid #1A2332;
    font-weight: 700; display: flex; gap: 10px; align-items: center;
  }
  .panel-subtitle { color: #94A3B8; font-weight: 500; font-size: 12px; margin-left: 4px; }
  .mode-pill {
    margin-left: auto; color: var(--accent-call); background: rgba(34,211,238,0.13);
    border: 1px solid rgba(34,211,238,0.35); border-radius: 6px;
    padding: 3px 10px; font-size: 11px; letter-spacing: .08em;
  }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent-call); }
  .dot.put-color { background: var(--accent-put); }
  .body { padding: 10px 12px; }
  .export-line {
    margin: 0; padding: 12px 14px; background: #05070B; border: 1px solid #1A2332;
    border-radius: 8px; color: #E5E7EB; font-family: Menlo, Consolas, monospace;
    font-size: 13px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere;
  }
  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="topbar">
  <div>
    <div class="title">QQQ Live Volatility Flow</div>
    <div class="meta" id="status">Starting...</div>
  </div>
  <div class="top-actions">
    <label class="history-control">History <select id="historySelect"><option value="live">Live realtime</option></select></label>
    <div class="color-controls">
      <label class="color-control">Call <input id="callColor" type="color" value="#22D3EE"><input id="callHex" class="hex-input" value="#22D3EE" spellcheck="false"></label>
      <label class="color-control">Put <input id="putColor" type="color" value="#F59E0B"><input id="putHex" class="hex-input" value="#F59E0B" spellcheck="false"></label>
    </div>
    <div class="meta" id="clock"></div>
  </div>
</div>
<div class="grid">
  <div class="panel wide">
    <div class="panel-header"><span class="dot"></span>Volatility Flow</div>
    <div class="body"><div id="flow" style="height:430px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot"></span>IV Rank</div>
    <div class="body"><div id="ivrank" style="height:340px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot" style="background:#FACC15"></span>Volatility Skew</div>
    <div class="body"><div id="skew" style="height:340px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot"></span>OI × IV by Strike</div>
    <div class="body"><div id="oiiv" style="height:360px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot put-color"></span>OI by Strike</div>
    <div class="body"><div id="oi" style="height:360px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot"></span>GEX Exposure</div>
    <div class="body"><div id="gex" style="height:520px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot put-color"></span>DEX Exposure</div>
    <div class="body"><div id="dex" style="height:520px;"></div></div>
  </div>
  <div class="panel wide">
    <div class="panel-header">
      <span class="dot"></span>Heat Tracker
      <span class="panel-subtitle">Intraday GEX exposure by strike x time, with spot path overlay</span>
      <span class="mode-pill">GEX</span>
    </div>
    <div class="body"><div id="gexribbon" style="height:440px;"></div></div>
  </div>
  <div class="panel wide">
    <div class="panel-header"><span class="dot" style="background:#FACC15"></span>Levels Export</div>
    <div class="body"><pre class="export-line" id="levelsExport">$QQQ: loading...</pre></div>
  </div>
</div>
<script>
const COLORS = {
  bg: "#05070B", panel: "#000000", grid: "#1F2937", text: "#E5E7EB",
  muted: "#94A3B8", cyan: "#22D3EE", yellow: "#FACC15", spot: "#CBD5E1",
  orange: "#F59E0B", green: "#4ADE80"
};
const DEFAULT_ACCENTS = {cyan: COLORS.cyan, orange: COLORS.orange};
let latestState = null;
let lastChartKey = "";
let flowHasInitialized = false;
let selectedHistoryId = "live";

function resetChartLocks() {
  lastChartKey = "";
  flowHasInitialized = false;
}

function validHex(value) {
  return typeof value === "string" && /^#[0-9A-F]{6}$/i.test(value);
}

function normalizeHex(value) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  const withHash = text.startsWith("#") ? text : "#" + text;
  return validHex(withHash) ? withHash.toUpperCase() : null;
}

function hexToRgb(hex) {
  const clean = validHex(hex) ? hex.slice(1) : "000000";
  return {
    r: parseInt(clean.slice(0, 2), 16),
    g: parseInt(clean.slice(2, 4), 16),
    b: parseInt(clean.slice(4, 6), 16)
  };
}

function rgbaFromHex(hex, alpha) {
  const rgb = hexToRgb(hex);
  return `rgba(${rgb.r},${rgb.g},${rgb.b},${alpha})`;
}

function loadAccentColors() {
  try {
    const call = localStorage.getItem("qqqDashboardCallColor");
    const put = localStorage.getItem("qqqDashboardPutColor");
    if (validHex(call)) COLORS.cyan = call;
    if (validHex(put)) COLORS.orange = put;
  } catch (_err) {}
}

function applyAccentColors() {
  document.documentElement.style.setProperty("--accent-call", COLORS.cyan);
  document.documentElement.style.setProperty("--accent-put", COLORS.orange);
  const callInput = document.getElementById("callColor");
  const putInput = document.getElementById("putColor");
  const callHex = document.getElementById("callHex");
  const putHex = document.getElementById("putHex");
  if (callInput) callInput.value = COLORS.cyan;
  if (putInput) putInput.value = COLORS.orange;
  if (callHex) callHex.value = COLORS.cyan;
  if (putHex) putHex.value = COLORS.orange;
}

function setAccentColor(key, value) {
  const normalized = normalizeHex(value);
  if (!normalized) return false;
  COLORS[key] = normalized;
  try {
    localStorage.setItem(key === "cyan" ? "qqqDashboardCallColor" : "qqqDashboardPutColor", normalized);
  } catch (_err) {}
  applyAccentColors();
  resetChartLocks();
  if (latestState) drawAll(latestState);
  return true;
}

function bindColorControls(colorId, hexId, key) {
  const colorInput = document.getElementById(colorId);
  const hexInput = document.getElementById(hexId);
  colorInput?.addEventListener("input", event => {
    setAccentColor(key, event.target.value);
    if (hexInput) hexInput.classList.remove("invalid");
  });
  hexInput?.addEventListener("input", event => {
    const normalized = normalizeHex(event.target.value);
    if (!normalized) {
      event.target.classList.add("invalid");
      return;
    }
    event.target.classList.remove("invalid");
    setAccentColor(key, normalized);
  });
  hexInput?.addEventListener("blur", event => {
    const normalized = normalizeHex(event.target.value);
    event.target.value = normalized || COLORS[key];
    event.target.classList.remove("invalid");
  });
}

loadAccentColors();

function compact(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "NA";
  const sign = Number(v) < 0 ? "-" : "";
  const n = Math.abs(Number(v));
  if (n >= 1e9) return sign + (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return sign + (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return sign + (n / 1e3).toFixed(2) + "K";
  return sign + n.toFixed(0);
}

function moneyM(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "NA";
  const sign = Number(v) < 0 ? "-" : "";
  return sign + "$" + (Math.abs(Number(v)) / 1e6).toFixed(1) + "M";
}

function fmtLevel(value, decimals = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "NA";
  const n = Number(value);
  if (decimals > 0) return n.toFixed(decimals);
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(3)));
}

function buildLevelsLine(summary) {
  const ticker = String(summary?.ticker || "QQQ").toUpperCase();
  const top = Array.isArray(summary?.top_abs_gex_levels) ? summary.top_abs_gex_levels : [];
  const gex = top.slice(0, 10).map(item => item?.strike);
  while (gex.length < 10) gex.push(null);
  const parts = [
    `$${ticker}: Call Resistance`, fmtLevel(summary?.call_resistance),
    "Put Support", fmtLevel(summary?.put_support),
    "HVL", fmtLevel(summary?.spot, 2),
    "1D Min", fmtLevel(summary?.one_day_min),
    "1D Max", fmtLevel(summary?.one_day_max),
    "Call Resistance 0DTE", fmtLevel(summary?.call_resistance),
    "Put Support 0DTE", fmtLevel(summary?.put_support),
    "HVL 0DTE", fmtLevel(summary?.gamma_flip),
    "Gamma Wall 0DTE", fmtLevel(summary?.gamma_wall_abs),
  ];
  gex.forEach((strike, idx) => {
    parts.push(`GEX ${idx + 1}`, fmtLevel(strike));
  });
  return parts.join(", ");
}

function timeET(v) {
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v || "NA");
  return d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }) + " ET";
}

function plotTimeNY(v) {
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23"
  }).formatToParts(d).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
}

function baseLayout(height) {
  return {
    paper_bgcolor: COLORS.bg,
    plot_bgcolor: COLORS.panel,
    font: {color: COLORS.text},
    margin: {l: 65, r: 60, t: 30, b: 45},
    height,
    uirevision: "keep-user-zoom",
    dragmode: "pan",
    hovermode: "x unified",
    legend: {orientation: "h", x: 1, xanchor: "right", y: 1.12},
    xaxis: {showgrid: false, zeroline: false, color: COLORS.muted, tickformat: "%H:%M", nticks: 10},
    yaxis: {showgrid: false, zeroline: false, color: COLORS.muted},
  };
}

function rightLegend() {
  return {orientation: "h", x: 1, xanchor: "right", y: 1.13, bgcolor: "rgba(9,13,21,0.70)"};
}

function paddedRange(values, minSpan, padRatio = 0.12) {
  const clean = values.map(Number).filter(Number.isFinite);
  if (!clean.length) return undefined;
  let lo = Math.min(...clean);
  let hi = Math.max(...clean);
  const center = (lo + hi) / 2;
  const span = Math.max(hi - lo, minSpan);
  const pad = span * padRatio;
  return [center - span / 2 - pad, center + span / 2 + pad];
}

function lastFinite(values) {
  for (let i = values.length - 1; i >= 0; i--) {
    if (Number.isFinite(values[i])) return values[i];
  }
  return null;
}

function drawFlow(points, session) {
  const x = points.map(p => plotTimeNY(p.time));
  const atmIv = points.map(p => Number.isFinite(Number(p.atm_iv)) ? Number(p.atm_iv) : null);
  const avgIv = points.map(p => Number.isFinite(Number(p.avg_iv)) ? Number(p.avg_iv) : null);
  const spot = points.map(p => Number.isFinite(Number(p.spot)) ? Number(p.spot) : null);
  const ivRangeValues = [...atmIv, ...avgIv].filter(Number.isFinite);
  const spotRangeValues = spot.filter(Number.isFinite);
  const flowArea = points.map((p, i) => {
    const iv = Number(atmIv[i]);
    const arv = Number(avgIv[i]);
    if (Number.isFinite(iv) && Number.isFinite(arv)) return Math.abs(iv - arv) * 1000;
    return null;
  });
  const flowRangeValues = flowArea.filter(Number.isFinite);
  const latestIv = lastFinite(atmIv);
  const latestArv = lastFinite(avgIv);
  const flowData = [
    {x, y: atmIv, type: "scatter", mode: "lines", name: "IV", line: {color: COLORS.cyan, width: 1.7}},
    {x, y: avgIv, type: "scatter", mode: "lines", name: "ARV", line: {color: COLORS.orange, width: 1.5}},
    {x, y: spot, type: "scatter", mode: "lines", name: "Spot", yaxis: "y2", line: {color: COLORS.spot, width: 1.4}},
    {
      x,
      y: flowArea,
      type: "scatter",
      mode: "lines",
      name: "Flow",
      yaxis: "y3",
      fill: "tozeroy",
      line: {color: COLORS.cyan, width: 1},
      fillcolor: rgbaFromHex(COLORS.cyan, 0.32),
      hoverinfo: "skip",
      showlegend: false
    },
  ];
  const layout = baseLayout(430);
  layout.uirevision = "volatility-flow";
  layout.yaxis = {
    title: "IV %",
    domain: [0.28, 1],
    showgrid: false,
    zeroline: false,
    color: COLORS.muted
  };
  layout.yaxis2 = {
    title: "Spot",
    overlaying: "y",
    side: "right",
    color: COLORS.muted,
    showgrid: false,
    zeroline: false,
    domain: [0.28, 1]
  };
  layout.yaxis3 = {
    title: "",
    domain: [0, 0.22],
    showgrid: false,
    zeroline: false,
    color: COLORS.muted,
    range: paddedRange(flowRangeValues, 1.0, 0.08)
  };
  layout.legend = {orientation: "h", x: 0.005, xanchor: "left", y: 1.12, bgcolor: "rgba(0,0,0,0)"};
  layout.annotations = [{
    x: 0,
    y: 1.14,
    xref: "paper",
    yref: "paper",
    showarrow: false,
    xanchor: "left",
    text: `<span style="color:${COLORS.cyan}">IV ${latestIv === null ? "NA" : latestIv.toFixed(1) + "%"}</span> · <span style="color:${COLORS.orange}">ARV ${latestArv === null ? "NA" : latestArv.toFixed(1) + "%"}</span>`,
    font: {size: 11}
  }];
  if (!flowHasInitialized) {
    layout.yaxis.range = paddedRange(ivRangeValues, 2.0);
    layout.yaxis2.range = paddedRange(spotRangeValues, 1.0);
  }
  // Anchor the time axis to the full trading session (9:30-16:00 ET) so the
  // chart always reads as "today's session so far" instead of just whatever
  // narrow window happens to be in memory since the server last restarted.
  if (!flowHasInitialized && session && session.market_open_utc && session.market_close_utc) {
    layout.xaxis.range = [plotTimeNY(session.market_open_utc), plotTimeNY(session.market_close_utc)];
    layout.xaxis.autorange = false;
  }
  Plotly.react("flow", flowData, layout, {displayModeBar: false, scrollZoom: true, responsive: true})
    .then(() => { flowHasInitialized = true; });
}

function exposureLine(y, color, dash = "dash") {
  return {
    type: "line", x0: 0, x1: 1, y0: y, y1: y,
    xref: "paper", yref: "y",
    line: {color, dash, width: 1.2}
  };
}

function exposureLabel(text, y, color, side = "right") {
  return {
    x: side === "left" ? 0.015 : 0.985,
    y,
    xref: "paper",
    yref: "y",
    text,
    showarrow: false,
    xanchor: side === "left" ? "left" : "right",
    yanchor: "middle",
    font: {color, size: 11, family: "Menlo, Consolas, monospace"},
    bgcolor: "rgba(5,7,11,0.70)",
    borderpad: 2
  };
}

function drawExposure(id, rows, key, summary) {
  const baseRows = rows
    .filter(r => Number.isFinite(Number(r.strike)) && Number.isFinite(Number(r[key])))
    .filter(r => Number(r[key]) !== 0 || Number(r.call_oi) > 0 || Number(r.put_oi) > 0);
  // Strikes whose exposure is negligible next to the biggest strike just
  // clutter the row axis with invisible bars — keep only the ones with a
  // real share of the total so the panel stays cropped to what matters.
  const fullMaxAbs = Math.max(1, ...baseRows.map(r => Math.abs(Number(r[key]) || 0)));
  const cleanRows = baseRows
    .filter(r => Math.abs(Number(r[key]) || 0) >= fullMaxAbs * 0.03)
    .sort((a, b) => Number(a.strike) - Number(b.strike));
  const values = cleanRows.map(r => Number(r[key]) || 0);
  const strikes = cleanRows.map(r => Number(r.strike));
  const maxAbs = Math.max(1, ...values.map(v => Math.abs(v)));
  const positiveColor = COLORS.cyan;
  const negativeColor = COLORS.orange;
  const label = key === "net_gex" ? "GEX Exposure" : "DEX Exposure";
  const callKey = key === "net_gex" ? "call_gex" : "call_dex";
  const putKey = key === "net_gex" ? "put_gex" : "put_dex";
  const buildExposureTrace = (sideRows, color, name) => ({
    x: sideRows.map(r => Number(r[key]) || 0),
    y: sideRows.map(r => Number(r.strike)),
    type: "bar",
    orientation: "h",
    width: 0.52,
    marker: {
      color,
      opacity: sideRows.map(r => Math.max(0.38, Math.min(0.98, Math.abs(Number(r[key]) || 0) / maxAbs))),
      line: {width: 0}
    },
    customdata: sideRows.map(r => [
      moneyM(Number(r[key]) || 0),
      moneyM(Number(r[callKey]) || 0),
      moneyM(Number(r[putKey]) || 0)
    ]),
    hoverlabel: {bgcolor: "#111827", bordercolor: "#374151", font: {color}},
    hovertemplate: (
      `<b><span style='color:${color}'>$%{y} net %{customdata[0]}</span></b><br>` +
      `<span style='color:${COLORS.cyan}'>Net Call %{customdata[1]}</span><br>` +
      `<span style='color:${COLORS.orange}'>Net Put %{customdata[2]}</span><extra></extra>`
    ),
    name
  });
  const data = [
    buildExposureTrace(cleanRows.filter(r => Number(r[key]) >= 0), positiveColor, label + " +"),
    buildExposureTrace(cleanRows.filter(r => Number(r[key]) < 0), negativeColor, label + " -"),
  ];
  const yMin = Math.min(...strikes, Number(summary?.spot || Infinity)) - 0.8;
  const yMax = Math.max(...strikes, Number(summary?.spot || -Infinity)) + 0.8;
  const shapes = [
    {type: "line", x0: 0, x1: 0, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.cyan, dash: "dot", width: 1.2}},
  ];
  const annotations = [];

  if (summary?.spot) {
    shapes.push(exposureLine(Number(summary.spot), COLORS.spot, "dot"));
    annotations.push(exposureLabel("SPOT " + Number(summary.spot).toFixed(2), Number(summary.spot), COLORS.spot, "right"));
  }

  if (summary?.call_resistance) {
    shapes.push(exposureLine(Number(summary.call_resistance), COLORS.cyan, "dash"));
    annotations.push(exposureLabel("CALL RESISTANCE " + Number(summary.call_resistance).toFixed(0), Number(summary.call_resistance), COLORS.cyan, "right"));
  }
  if (summary?.put_support) {
    shapes.push(exposureLine(Number(summary.put_support), COLORS.orange, "dash"));
    annotations.push(exposureLabel("PUT SUPPORT " + Number(summary.put_support).toFixed(0), Number(summary.put_support), COLORS.orange, "left"));
  }

  if (key === "net_gex") {
    if (summary?.gamma_wall_abs) {
      shapes.push(exposureLine(Number(summary.gamma_wall_abs), "#A78BFA", "dashdot"));
      annotations.push(exposureLabel("GAMMA WALL " + Number(summary.gamma_wall_abs).toFixed(0), Number(summary.gamma_wall_abs), "#A78BFA", "right"));
    }
    if (summary?.gamma_flip) {
      shapes.push(exposureLine(Number(summary.gamma_flip), "#C084FC", "dot"));
      annotations.push(exposureLabel("GAMMA FLIP " + Number(summary.gamma_flip).toFixed(2), Number(summary.gamma_flip), "#C084FC", "right"));
    }
  } else {
    if (summary?.dex_wall_abs) {
      shapes.push(exposureLine(Number(summary.dex_wall_abs), "#A78BFA", "dashdot"));
      annotations.push(exposureLabel("DELTA WALL " + Number(summary.dex_wall_abs).toFixed(0), Number(summary.dex_wall_abs), "#A78BFA", "right"));
    }
    if (summary?.delta_flip) {
      shapes.push(exposureLine(Number(summary.delta_flip), "#C084FC", "dot"));
      annotations.push(exposureLabel("DELTA FLIP " + Number(summary.delta_flip).toFixed(2), Number(summary.delta_flip), "#C084FC", "right"));
    }
  }

  const layout = {
    paper_bgcolor: COLORS.bg,
    plot_bgcolor: "#000000",
    font: {color: COLORS.text, family: "Menlo, Consolas, monospace"},
    margin: {l: 58, r: 42, t: 12, b: 10},
    height: 520,
    uirevision: "keep-user-zoom",
    dragmode: "pan",
    hovermode: "closest",
    showlegend: false,
    bargap: 0,
    xaxis: {visible: false, zeroline: false, range: [-maxAbs * 1.18, maxAbs * 1.18], fixedrange: false},
    yaxis: {
      showgrid: false,
      zeroline: false,
      color: COLORS.muted,
      dtick: 1,
      range: [yMin, yMax],
      fixedrange: false,
      title: null
    },
    shapes,
    annotations
  };
  Plotly.react(id, data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function drawOi(rows, summary) {
  const x = rows.map(r => r.strike);
  const data = [
    {x, y: rows.map(r => r.call_oi), type: "bar", name: "Calls", marker: {color: COLORS.cyan}},
    {x, y: rows.map(r => r.put_oi), type: "bar", name: "Puts", marker: {color: COLORS.orange}},
  ];
  const layout = baseLayout(360);
  layout.margin.t = 55;
  layout.legend = rightLegend();
  layout.barmode = "group";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis = {title: "Open Interest", showgrid: false, zeroline: false, color: COLORS.muted};
  if (summary?.spot) {
    layout.shapes = [{type: "line", x0: summary.spot, x1: summary.spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
    layout.annotations = [{x: summary.spot, y: 0.98, xref: "x", yref: "paper", text: "SPOT " + Number(summary.spot).toFixed(2), showarrow: false, xanchor: "left", yanchor: "top", xshift: 6, font: {color: COLORS.spot, size: 11}, bgcolor: "rgba(5,7,11,0.70)"}];
  }
  Plotly.react("oi", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function drawOiIv(rows, summary) {
  const x = rows.map(r => r.strike);
  const data = [
    {x, y: rows.map(r => r.call_oi), type: "bar", name: "Calls", marker: {color: COLORS.cyan}},
    {x, y: rows.map(r => r.put_oi), type: "bar", name: "Puts", marker: {color: COLORS.orange}},
    {x, y: rows.map(r => r.iv_pct), type: "scatter", mode: "lines", name: "IV", yaxis: "y2", line: {color: "#F8FAFC", width: 2}},
  ];
  const layout = baseLayout(360);
  layout.margin.t = 55;
  layout.legend = rightLegend();
  layout.barmode = "group";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis = {title: "Open Interest", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis2 = {title: "IV %", overlaying: "y", side: "right", color: COLORS.muted, showgrid: false, zeroline: false};
  if (summary?.spot) {
    layout.shapes = [{type: "line", x0: summary.spot, x1: summary.spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
    layout.annotations = [{x: summary.spot, y: 0.98, xref: "x", yref: "paper", text: "SPOT " + Number(summary.spot).toFixed(2), showarrow: false, xanchor: "left", yanchor: "top", xshift: 6, font: {color: COLORS.spot, size: 11}, bgcolor: "rgba(5,7,11,0.70)"}];
  }
  Plotly.react("oiiv", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function drawSkew(rows, summary) {
  const spot = Number(summary?.spot);
  const clean = rows
    .filter(r => Number.isFinite(Number(r.strike)))
    .sort((a, b) => Number(a.strike) - Number(b.strike));
  const atmRow = clean.reduce((best, row) => {
    if (!Number.isFinite(spot)) return best;
    if (!best) return row;
    return Math.abs(Number(row.strike) - spot) < Math.abs(Number(best.strike) - spot) ? row : best;
  }, null);
  const atmStrike = atmRow ? Number(atmRow.strike) : spot;
  const atmValues = [];
  if (atmRow && Number.isFinite(Number(atmRow.call_iv_pct))) atmValues.push(Number(atmRow.call_iv_pct));
  if (atmRow && Number.isFinite(Number(atmRow.put_iv_pct))) atmValues.push(Number(atmRow.put_iv_pct));
  const atmIv = atmValues.length ? atmValues.reduce((a, b) => a + b, 0) / atmValues.length : NaN;
  const atmPoint = Number.isFinite(atmIv) ? [{strike: atmStrike, iv: atmIv}] : [];
  const callRows = [
    ...atmPoint,
    ...clean
      .filter(r => Number(r.strike) > atmStrike && Number.isFinite(Number(r.call_iv_pct)))
      .map(r => ({strike: Number(r.strike), iv: Number(r.call_iv_pct)}))
  ];
  const putRows = [
    ...clean
      .filter(r => Number(r.strike) < atmStrike && Number.isFinite(Number(r.put_iv_pct)))
      .map(r => ({strike: Number(r.strike), iv: Number(r.put_iv_pct)})),
    ...atmPoint
  ];
  const smoothIvRows = curve => {
    const cleanCurve = curve
      .filter(r => Number.isFinite(Number(r.strike)) && Number.isFinite(Number(r.iv)))
      .sort((a, b) => Number(a.strike) - Number(b.strike));
    return cleanCurve.map((row, i) => {
      const neighbors = cleanCurve.slice(Math.max(0, i - 1), Math.min(cleanCurve.length, i + 2)).map(r => Number(r.iv));
      const sorted = [...neighbors].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      const jumpLimit = Math.max(3.0, Math.abs(median) * 0.35);
      const cleaned = Math.abs(Number(row.iv) - median) > jumpLimit ? median : Number(row.iv);
      const prev = i > 0 ? Number(cleanCurve[i - 1].iv) : cleaned;
      const next = i < cleanCurve.length - 1 ? Number(cleanCurve[i + 1].iv) : cleaned;
      return {strike: Number(row.strike), iv: (prev * 0.2 + cleaned * 0.6 + next * 0.2)};
    });
  };
  const smoothCallRows = smoothIvRows(callRows);
  const smoothPutRows = smoothIvRows(putRows);
  const data = [
    {
      x: smoothCallRows.map(r => r.strike),
      y: smoothCallRows.map(r => r.iv),
      type: "scatter",
      mode: "lines",
      name: "Calls",
      line: {color: COLORS.cyan, width: 2},
      hovertemplate: "<span style='color:#94A3B8'>strike %{x}</span><br><span style='color:#FACC15'>0DTE</span> C %{y:.1f}% IV %{y:.1f}%<extra></extra>"
    },
    {
      x: smoothPutRows.map(r => r.strike),
      y: smoothPutRows.map(r => r.iv),
      type: "scatter",
      mode: "lines",
      name: "Puts",
      line: {color: COLORS.orange, width: 2, dash: "dash"},
      hovertemplate: "<span style='color:#94A3B8'>strike %{x}</span><br><span style='color:#FACC15'>0DTE</span> P %{y:.1f}% IV %{y:.1f}%<extra></extra>"
    },
  ];
  const layout = baseLayout(340);
  layout.margin.t = 55;
  layout.legend = rightLegend();
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis = {title: "IV %", showgrid: false, zeroline: false, color: COLORS.muted};
  if (Number.isFinite(spot)) {
    layout.shapes = [{type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
  }
  Plotly.react("skew", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function drawIvRank(history, summary) {
  const rows = (history || [])
    .filter(r => Number.isFinite(Number(r.iv_rank_pct)) && Number.isFinite(Number(r.avg_iv_pct)) && Number.isFinite(Number(r.spot)))
    .sort((a, b) => new Date(a.snapshot_utc || a.snapshot_vn) - new Date(b.snapshot_utc || b.snapshot_vn));
  const x = rows.map(r => r.snapshot_vn || r.snapshot_utc);
  const y = rows.map(r => Number(r.iv_rank_pct));
  const spot = rows.map(r => Number(r.spot));
  const tickText = x.map(v => new Date(v).toLocaleDateString("en-US", {month: "short", day: "2-digit"}));
  const labelText = x.map(v => new Date(v).toLocaleDateString("en-US", {month: "short", day: "2-digit"}));
  const formatHeader = row => `rank ${Number(row.iv_rank_pct || 0).toFixed(1)}% · IV ${Number(row.avg_iv_pct || 0).toFixed(1)}% · $${Number(row.spot || 0).toFixed(2)}`;
  const data = [
    {
      x,
      y,
      customdata: rows.map((r, i) => [Number(r.avg_iv_pct), Number(r.spot), Number(r.iv_rank_pct), labelText[i]]),
      type: "scatter",
      mode: "lines",
      name: "IV Rank",
      line: {color: COLORS.cyan, width: 2},
      hovertemplate: "%{customdata[3]}<extra></extra>"
    },
    {x, y: spot, type: "scatter", mode: "lines", name: "Spot", yaxis: "y2", line: {color: COLORS.spot, width: 1.5, dash: "dot"}, hoverinfo: "skip"},
  ];
  const layout = baseLayout(340);
  layout.margin.t = 55;
  layout.legend = rightLegend();
  layout.dragmode = false;
  layout.xaxis = {showgrid: false, zeroline: false, color: COLORS.muted, tickmode: "array", tickvals: x, ticktext: tickText, tickangle: 0, fixedrange: true};
  layout.yaxis = {title: "IV Rank %", showgrid: false, zeroline: false, color: COLORS.muted, range: [0, 100], fixedrange: true};
  const spotValues = spot.filter(Number.isFinite);
  const spotMin = Math.min(...spotValues);
  const spotMax = Math.max(...spotValues);
  const spotPad = Math.max(1, (spotMax - spotMin) * 0.18);
  layout.yaxis2 = {title: "Spot", overlaying: "y", side: "right", color: COLORS.muted, showgrid: false, zeroline: false, fixedrange: true, range: [spotMin - spotPad, spotMax + spotPad]};
  const current = rows.length ? rows[rows.length - 1] : {};
  layout.shapes = [
    {type: "rect", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 0, y1: 20, fillcolor: "rgba(34,211,238,0.10)", line: {width: 0}, layer: "below"},
    {type: "rect", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 80, y1: 100, fillcolor: "rgba(245,158,11,0.12)", line: {width: 0}, layer: "below"}
  ];
  layout.annotations = [{
    x: 0, y: 1.15, xref: "paper", yref: "paper", showarrow: false, xanchor: "left",
    text: formatHeader(current),
    font: {color: COLORS.cyan, size: 11}
  }];
  Plotly.react("ivrank", data, layout, {displayModeBar: false, scrollZoom: false, responsive: true}).then(() => {
    const gd = document.getElementById("ivrank");
    if (!gd || !gd.on) return;
    if (gd.removeAllListeners) {
      gd.removeAllListeners("plotly_hover");
      gd.removeAllListeners("plotly_unhover");
    }
    gd.on("plotly_hover", ev => {
      const point = (ev.points || []).find(p => p.curveNumber === 0) || (ev.points || [])[0];
      const cd = point && point.customdata;
      if (!cd) return;
      Plotly.relayout(gd, {"annotations[0].text": formatHeader({avg_iv_pct: cd[0], spot: cd[1], iv_rank_pct: cd[2]})});
    });
    gd.on("plotly_unhover", () => {
      Plotly.relayout(gd, {"annotations[0].text": formatHeader(current)});
    });
  });
}

function drawGexRibbon(ribbon, points, summary, session) {
  const times = [...new Set((ribbon || []).map(s => s.time).filter(Boolean))].sort();
  const strikeSet = new Set();
  let maxAbs = 1;
  for (const snap of ribbon) {
    for (const row of snap.rows || []) {
      const strike = Number(row.strike);
      const gex = Number(row.net_gex);
      if (Number.isFinite(strike) && Number.isFinite(gex) && gex !== 0) {
        strikeSet.add(strike);
        maxAbs = Math.max(maxAbs, Math.abs(gex));
      }
    }
  }
  const minDrawAbs = Math.max(50000, maxAbs * 0.002);
  const strikes = [...strikeSet].sort((a, b) => a - b);
  const pointByTime = new Map((points || []).map(p => [p.time, p]));
  const posCells = {x: [], y: [], color: [], customdata: []};
  const negCells = {x: [], y: [], color: [], customdata: []};
  const exposureByStrike = new Map();
  const cellColor = (gex) => {
    const intensity = Math.max(0.18, Math.min(1, Math.pow(Math.abs(gex) / maxAbs, 0.55)));
    if (gex > 0) return rgbaFromHex(COLORS.cyan, intensity.toFixed(3));
    return rgbaFromHex(COLORS.orange, intensity.toFixed(3));
  };
  for (const snap of ribbon) {
    for (const row of snap.rows || []) {
      const strike = Number(row.strike);
      const gex = Number(row.net_gex);
      if (Number.isFinite(strike) && Number.isFinite(gex) && Math.abs(gex) >= minDrawAbs) {
        const aggregate = exposureByStrike.get(strike) || {strike, posMax: 0, negMax: 0, posSum: 0, negSum: 0, posCount: 0, negCount: 0};
        if (gex > 0) {
          aggregate.posMax = Math.max(aggregate.posMax, Math.abs(gex));
          aggregate.posSum += Math.abs(gex);
          aggregate.posCount += 1;
        } else {
          aggregate.negMax = Math.max(aggregate.negMax, Math.abs(gex));
          aggregate.negSum += Math.abs(gex);
          aggregate.negCount += 1;
        }
        exposureByStrike.set(strike, aggregate);
        const target = gex > 0 ? posCells : negCells;
        const p = pointByTime.get(snap.time) || {};
        const spot = Number(p.spot);
        target.x.push(plotTimeNY(snap.time));
        target.y.push(strike);
        target.color.push(cellColor(gex));
        target.customdata.push([
          moneyM(gex),
          timeET(snap.time),
          Number.isFinite(spot) ? spot.toFixed(2) : "NA",
          Number.isFinite(spot) ? spot.toFixed(2) : "NA",
          Number.isFinite(spot) ? spot.toFixed(2) : "NA",
        ]);
      }
    }
  }
  const heatHover = (
    "<b>$%{y}</b><br>" +
    "GEX&nbsp;&nbsp;%{customdata[0]}<br><br>" +
    "<span style='color:#94A3B8'>%{customdata[1]} · O %{customdata[2]} C %{customdata[4]}</span><extra></extra>"
  );
  const data = [
    {
      x: posCells.x,
      y: posCells.y,
      customdata: posCells.customdata,
      type: "scattergl",
      mode: "markers",
      marker: {symbol: "square", size: 10, color: posCells.color, line: {width: 0}},
      hoverlabel: {bgcolor: "#111827", bordercolor: "#374151", font: {color: COLORS.cyan}},
      hovertemplate: heatHover,
      name: "GEX +"
    },
    {
      x: negCells.x,
      y: negCells.y,
      customdata: negCells.customdata,
      type: "scattergl",
      mode: "markers",
      marker: {symbol: "square", size: 10, color: negCells.color, line: {width: 0}},
      hoverlabel: {bgcolor: "#111827", bordercolor: "#374151", font: {color: COLORS.orange}},
      hovertemplate: heatHover,
      name: "GEX -"
    },
    {
      x: points.map(p => plotTimeNY(p.time)), y: points.map(p => p.spot), type: "scatter", mode: "lines",
      name: "Spot", line: {color: "#F8FAFC", width: 1.6}, hoverinfo: "skip"
    },
  ];

  const shapes = [];
  const annotations = [];
  const spot = Number(summary?.spot);
  const heatLevelLine = (y, color, dash = "solid") => ({
    type: "line",
    x0: 0,
    x1: 1,
    y0: y,
    y1: y,
    xref: "paper",
    yref: "y",
    line: {color, dash, width: dash === "solid" ? 2.4 : 1.6}
  });
  const dynamicLevels = [...exposureByStrike.values()]
    .flatMap(level => [
      {
        strike: level.strike,
        net_gex: level.posMax,
        side: "call",
        score: level.posMax * 0.75 + (level.posCount ? level.posSum / level.posCount : 0) * 0.25,
      },
      {
        strike: level.strike,
        net_gex: -level.negMax,
        side: "put",
        score: level.negMax * 0.75 + (level.negCount ? level.negSum / level.negCount : 0) * 0.25,
      }
    ])
    .filter(level => level.score > maxAbs * 0.08);
  const summaryLevels = (summary?.top_abs_gex_levels || []).map(l => ({
    strike: Number(l.strike),
    net_gex: Number(l.net_gex),
    side: Number(l.net_gex) >= 0 ? "call" : "put",
    score: Math.abs(Number(l.net_gex) || 0),
  }));
  const levels = [...dynamicLevels, ...summaryLevels]
    .filter(l => Number.isFinite(l.strike) && Number.isFinite(l.net_gex) && Number.isFinite(l.score));
  const uniqueLevels = (items, side) => {
    const seen = new Set();
    return items
      .filter(l => l.side === side)
      .sort((a, b) => b.score - a.score)
      .filter(l => {
        const key = String(l.strike);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 3);
  };

  if (Number.isFinite(spot)) {
    const callWalls = uniqueLevels(levels.filter(l => l.strike > spot && l.net_gex > 0), "call");
    const callLabels = ["CALL RESISTANCE", "CALL WALL 2", "CALL WALL 3"];
    callWalls.forEach((l, i) => {
      shapes.push(heatLevelLine(l.strike, COLORS.cyan, i === 0 ? "solid" : "dash"));
      annotations.push(exposureLabel(callLabels[i] + " " + l.strike.toFixed(0), l.strike, COLORS.cyan, "right"));
    });

    const putWalls = uniqueLevels(levels.filter(l => l.strike < spot && l.net_gex < 0), "put");
    const putLabels = ["PUT SUPPORT", "PUT WALL 2", "PUT WALL 3"];
    putWalls.forEach((l, i) => {
      shapes.push(heatLevelLine(l.strike, COLORS.orange, i === 0 ? "solid" : "dash"));
      annotations.push(exposureLabel(putLabels[i] + " " + l.strike.toFixed(0), l.strike, COLORS.orange, "left"));
    });
  }

  if (summary?.gamma_flip) {
    shapes.push(heatLevelLine(Number(summary.gamma_flip), "#C084FC", "dot"));
    annotations.push(exposureLabel("GAMMA FLIP " + Number(summary.gamma_flip).toFixed(2), Number(summary.gamma_flip), "#C084FC", "right"));
  }

  const layout = baseLayout(440);
  layout.margin = {l: 65, r: 140, t: 30, b: 45};
  layout.legend = {orientation: "h", x: 1, xanchor: "right", y: 1.12};
  layout.yaxis.title = "Strike";
  layout.yaxis.nticks = 18;
  layout.yaxis.tickformat = ".2~f";
  layout.hovermode = "closest";
  layout.xaxis.type = "date";
  layout.xaxis.tickformat = "%H:%M";
  if (strikes.length) {
    const yPad = Math.max(0.8, (Math.max(...strikes) - Math.min(...strikes)) * 0.08);
    layout.yaxis.range = [Math.min(...strikes) - yPad, Math.max(...strikes) + yPad];
  }
  const sessionOpen = session && session.market_open_utc ? plotTimeNY(session.market_open_utc) : null;
  const sessionClose = session && session.market_close_utc ? plotTimeNY(session.market_close_utc) : null;
  if (sessionOpen && sessionClose) {
    layout.xaxis.range = [sessionOpen, sessionClose];
    layout.xaxis.autorange = false;
    layout.xaxis.fixedrange = true;
    layout.xaxis.uirevision = "session-" + (session.trading_date || sessionOpen.slice(0, 10));
  }
  layout.shapes = shapes;
  layout.annotations = annotations;
  Plotly.react("gexribbon", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function drawAll(state) {
  const levelsExport = document.getElementById("levelsExport");
  if (levelsExport) {
    levelsExport.textContent = state.levels_summary
      ? buildLevelsLine(state.levels_summary)
      : "$" + String(state.latest_summary?.ticker || "QQQ").toUpperCase() + ": waiting for fixed 20:25 Vietnam snapshot...";
  }
  drawFlow(state.points || [], state.session || null);
  drawIvRank(state.history || [], state.latest_summary || {});
  drawSkew(state.by_strike || [], state.latest_summary || {});
  drawOiIv(state.by_strike || [], state.latest_summary || {});
  drawOi(state.by_strike || [], state.latest_summary || {});
  drawExposure("gex", state.by_strike || [], "net_gex", state.latest_summary || {});
  drawExposure("dex", state.by_strike || [], "net_dex", state.latest_summary || {});
  drawGexRibbon(state.gex_ribbon || [], state.points || [], state.latest_summary || {}, state.session || null);
}

async function loadHistoryChoices() {
  const select = document.getElementById("historySelect");
  if (!select) return;
  try {
    const res = await fetch("/api/history?ts=" + Date.now());
    const payload = await res.json();
    const currentValue = select.value || "live";
    select.innerHTML = '<option value="live">Live realtime</option>';
    for (const item of payload.snapshots || []) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      select.appendChild(option);
    }
    select.value = [...select.options].some(opt => opt.value === currentValue) ? currentValue : "live";
  } catch (err) {
    console.warn("Could not load history choices", err);
  }
}

async function loadHistoricalSnapshot(snapshotId) {
  const res = await fetch("/api/snapshot?id=" + encodeURIComponent(snapshotId) + "&ts=" + Date.now());
  const state = await res.json();
  if (state.error) throw new Error(state.error);
  selectedHistoryId = snapshotId;
  latestState = state;
  resetChartLocks();
  drawAll(state);
  document.getElementById("status").textContent = "Viewing history · " + (state.latest_summary?.snapshot_vn || state.latest_summary?.snapshot_utc || snapshotId);
}

async function update() {
  const res = await fetch("/api/state?ts=" + Date.now());
  const state = await res.json();
  if (selectedHistoryId !== "live") {
    document.getElementById("clock").textContent = new Date().toLocaleTimeString();
    return;
  }
  latestState = state;
  const status = [
    state.running ? "Running" : "Stopped",
    `${state.successes} ok / ${state.failures} failed`,
    state.latest_error ? "Last error: " + state.latest_error : null,
    state.next_fetch ? "Next fetch: " + new Date(state.next_fetch).toLocaleTimeString() : null,
  ].filter(Boolean).join(" · ");
  document.getElementById("status").textContent = status;
  document.getElementById("clock").textContent = new Date().toLocaleTimeString();
  const points = state.points || [];
  const ribbon = state.gex_ribbon || [];
  const latestPoint = points.length ? points[points.length - 1].time : "";
  const latestRibbon = ribbon.length ? ribbon[ribbon.length - 1].time : "";
  const chartKey = [
    state.latest_summary?.snapshot_utc || "",
    state.levels_summary?.snapshot_utc || "",
    points.length,
    latestPoint,
    ribbon.length,
    latestRibbon,
    state.by_strike?.length || 0,
  ].join("|");
  if (chartKey === lastChartKey) return;
  lastChartKey = chartKey;
  drawAll(state);
}

applyAccentColors();
bindColorControls("callColor", "callHex", "cyan");
bindColorControls("putColor", "putHex", "orange");
loadHistoryChoices();
document.getElementById("historySelect")?.addEventListener("change", async event => {
  const value = event.target.value;
  if (value === "live") {
    selectedHistoryId = "live";
    resetChartLocks();
    await update();
    return;
  }
  try {
    await loadHistoricalSnapshot(value);
  } catch (err) {
    document.getElementById("status").textContent = "History error: " + err.message;
  }
});
update();
setInterval(update, 10000);
</script>
</body>
</html>
"""


class LiveState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.points: list[dict] = []
        self.gex_ribbon: list[dict] = []
        self.history: list[dict] = []
        self.by_strike: list[dict] = []
        self.latest_summary: dict | None = None
        self.levels_summary: dict | None = None
        self.latest_error: str | None = None
        self.running = True
        self.successes = 0
        self.failures = 0
        self.next_fetch: str | None = None
        self.session: dict = market_session_utc()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "points": self.points,
                "gex_ribbon": self.gex_ribbon,
                "history": self.history,
                "by_strike": self.by_strike,
                "latest_summary": self.latest_summary,
                "levels_summary": self.levels_summary,
                "latest_error": self.latest_error,
                "running": self.running,
                "successes": self.successes,
                "failures": self.failures,
                "next_fetch": self.next_fetch,
                "session": self.session,
            }


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (int, np.integer)):
        return int(value)
    return value


def clean_records(records: list[dict]) -> list[dict]:
    return [{key: clean_value(value) for key, value in row.items()} for row in records]


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
    candidates: list[tuple[float, float]] = []
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local realtime QQQ Volatility Flow dashboard.")
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--expiry", default=None)
    parser.add_argument("--duration-minutes", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--collect-start-vn", default="20:25")
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


def build_iv_rank_history_rows(ticker: str) -> list[dict]:
    rows_by_day: dict[str, dict] = {}
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_summary.json")):
        if len(path.stem.split("_")) != 3:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot):
            continue
        spot = clean_value(summary.get("spot"))
        iv_value = None
        iv_source = None
        by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
        if by_strike_path.exists() and spot is not None:
            try:
                by_strike = pd.read_parquet(by_strike_path)
                clean = by_strike[np.isfinite(by_strike["strike"])].copy()
                clean["dist"] = (clean["strike"] - float(spot)).abs()
                atm = clean.sort_values("dist").head(1)
                if not atm.empty and "iv" in atm.columns and pd.notna(atm.iloc[0].get("iv")):
                    iv_value = float(atm.iloc[0]["iv"])
                    iv_source = "by_strike_atm"
            except Exception:
                pass
        if iv_value is None and spot is not None:
            iv_value = atm_iv_from_yahoo_raw(path.parent, ticker, float(spot))
            if iv_value is not None:
                iv_source = "raw_yahoo_atm"
        if iv_value is None:
            summary_iv = summary.get("avg_iv")
            if summary_iv is not None and np.isfinite(float(summary_iv)) and float(summary_iv) <= 0.8:
                iv_value = float(summary_iv)
                iv_source = "summary_avg_iv"
        avg_iv_pct = clean_value(iv_value * 100 if iv_value is not None else None)
        if spot is None or avg_iv_pct is None:
            continue
        day_key = snapshot.date().isoformat()
        row = {
            "date": day_key,
            "ticker": ticker.upper(),
            "snapshot_utc": snapshot.isoformat(),
            "snapshot_vn": snapshot.tz_convert("Asia/Ho_Chi_Minh").isoformat(),
            "spot": spot,
            "atm_iv_pct": avg_iv_pct,
            "avg_iv_pct": avg_iv_pct,
            "iv_source": iv_source,
        }
        previous = rows_by_day.get(day_key)
        if previous is None or row["snapshot_utc"] > previous["snapshot_utc"]:
            rows_by_day[day_key] = row
    rows = sorted(rows_by_day.values(), key=lambda row: row["snapshot_utc"])
    iv_values: list[float] = []
    for row in rows:
        iv_values.append(float(row["avg_iv_pct"]))
        window = iv_values[-60:]
        low = min(window)
        high = max(window)
        row["iv_rank_pct"] = ((iv_values[-1] - low) / (high - low) * 100.0) if high > low else None
    return clean_records(rows)


def write_iv_rank_history_csv(rows: list[dict]) -> None:
    if not rows:
        return
    columns = ["date", "ticker", "snapshot_utc", "snapshot_vn", "spot", "atm_iv_pct", "iv_rank_pct", "iv_source"]
    df = pd.DataFrame(rows)
    for col in columns:
        if col not in df:
            df[col] = None
    IV_RANK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.loc[:, columns].to_csv(IV_RANK_HISTORY_PATH, index=False)


def load_history(ticker: str) -> list[dict]:
    rows = build_iv_rank_history_rows(ticker)
    write_iv_rank_history_csv(rows)
    return rows[-60:]


def seed_session_data(ticker: str, session: dict, window: float) -> tuple[list[dict], list[dict]]:
    """Load already-collected snapshots for today's configured collection window
    off disk, so Volatility Flow and the GEX ribbon show the session so far instead of
    resetting empty every time the server restarts (state normally only lives in RAM).
    Reads each snapshot file once to produce both series together."""
    open_ts = pd.Timestamp(session["collection_start_utc"])
    points: list[dict] = []
    ribbon: list[dict] = []
    seen_times: set[str] = set()
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:  # skip the canonical "latest" file (3 parts)
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot) or snapshot < open_ts:
            continue
        if snapshot.tz_convert(NY_TZ).date().isoformat() != session["trading_date"]:
            continue
        time_key = summary.get("snapshot_utc")
        if time_key in seen_times:
            continue
        seen_times.add(time_key)
        spot = float(summary["spot"])
        atm_iv = None
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            ribbon.append(gex_snapshot_from_chart_rows(time_key, chart_rows))
        except Exception:
            pass
        points.append({
            "time": time_key,
            "atm_iv": clean_value(atm_iv),
            "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
            "spot": clean_value(spot),
        })
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def seed_levels_summary(ticker: str, session: dict) -> dict | None:
    """Return the first timestamped summary at/after the fixed 20:25 VN level snapshot.

    This keeps Levels Export stable across browser refreshes and local server restarts
    during the same NY trading session.
    """
    open_ts = pd.Timestamp(session["collection_start_utc"])
    candidates: list[tuple[pd.Timestamp, dict]] = []
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot) or snapshot < open_ts:
            continue
        if snapshot.tz_convert(NY_TZ).date().isoformat() != session["trading_date"]:
            continue
        candidates.append((snapshot, summary))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def gex_snapshot_from_chart_rows(time_key: str, chart_rows: pd.DataFrame) -> dict:
    rows = chart_rows.sort_values("strike")[["strike", "net_gex"]].copy()
    records = rows.replace([np.inf, -np.inf], np.nan).where(pd.notna(rows), None).to_dict(orient="records")
    return {"time": time_key, "rows": clean_records(records)}


def session_from_summary(summary: dict) -> dict:
    snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
    if pd.isna(snapshot):
        return market_session_utc()
    trading_date = snapshot.tz_convert(NY_TZ).date()
    open_ny = pd.Timestamp.combine(trading_date, dt_time(9, 30)).tz_localize(NY_TZ)
    close_ny = pd.Timestamp.combine(trading_date, dt_time(16, 0)).tz_localize(NY_TZ)
    return {
        "trading_date": trading_date.isoformat(),
        "market_open_utc": open_ny.tz_convert("UTC").isoformat(),
        "market_close_utc": close_ny.tz_convert("UTC").isoformat(),
    }


def snapshot_id_for_path(path: Path) -> str:
    return path.relative_to(DATA_ROOT).as_posix()


def summary_path_from_id(snapshot_id: str) -> Path:
    decoded = unquote(snapshot_id)
    candidate = (DATA_ROOT / decoded).resolve()
    root = DATA_ROOT.resolve()
    if root not in candidate.parents or candidate.suffix != ".json" or not candidate.name.endswith("_summary.json"):
        raise ValueError("invalid snapshot id")
    if not candidate.exists():
        raise FileNotFoundError("snapshot not found")
    return candidate


def snapshot_label(path: Path, summary: dict) -> str:
    expiry = summary.get("expiry") or path.stem.split("_")[1]
    snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
    if pd.notna(snapshot):
        label_time = snapshot.tz_convert(VN_TZ).strftime("%Y-%m-%d %H:%M VN")
    else:
        label_time = path.parent.name
    label_type = "Daily" if len(path.stem.split("_")) == 3 else "Snapshot"
    return f"{label_type} · {label_time} · {expiry}"


def list_history_choices(ticker: str) -> list[dict]:
    choices = []
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        choices.append(
            {
                "id": snapshot_id_for_path(path),
                "label": snapshot_label(path, summary),
                "snapshot_utc": summary.get("snapshot_utc"),
                "expiry": summary.get("expiry"),
            }
        )
    return choices[:600]


def chart_payload_from_summary_path(
    summary_path: Path, ticker: str, window: float
) -> tuple[dict, dict, list[dict], list[dict], dict]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    by_strike_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_by_strike.parquet"))
    by_strike = pd.read_parquet(by_strike_path)
    spot = float(summary["spot"])
    atm_iv = nearest_atm_iv(by_strike, spot)
    time_key = summary.get("snapshot_utc")
    point = {
        "time": time_key,
        "atm_iv": clean_value(atm_iv),
        "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
        "spot": clean_value(spot),
    }
    chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)].copy()
    if chart_rows.empty:
        chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
    rows = chart_rows.sort_values("strike")[
        [
            "strike",
            "net_gex",
            "call_gex",
            "put_gex",
            "net_dex",
            "call_dex",
            "put_dex",
            "call_oi",
            "put_oi",
            "iv",
            "call_iv",
            "put_iv",
        ]
    ].copy()
    rows["iv_pct"] = rows["iv"] * 100
    rows["call_iv_pct"] = rows["call_iv"] * 100
    rows["put_iv_pct"] = rows["put_iv"] * 100
    records = rows.replace([np.inf, -np.inf], np.nan).where(pd.notna(rows), None).to_dict(orient="records")
    gex_snapshot = gex_snapshot_from_chart_rows(time_key, chart_rows)
    return summary, point, clean_records(records), load_history(ticker), gex_snapshot


def day_series_from_summary_path(summary_path: Path, ticker: str, window: float) -> tuple[list[dict], list[dict]]:
    points: list[dict] = []
    ribbon: list[dict] = []
    seen_times: set[str] = set()
    selected_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    selected_session = session_from_summary(selected_summary)
    for path in sorted(summary_path.parent.glob(f"{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot) or snapshot.tz_convert(NY_TZ).date().isoformat() != selected_session["trading_date"]:
            continue
        time_key = summary.get("snapshot_utc")
        if not time_key or time_key in seen_times:
            continue
        seen_times.add(time_key)
        spot = float(summary["spot"])
        atm_iv = None
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            ribbon.append(gex_snapshot_from_chart_rows(time_key, chart_rows))
        except Exception:
            pass
        points.append(
            {
                "time": time_key,
                "atm_iv": clean_value(atm_iv),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(spot),
            }
        )
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def load_snapshot_state(snapshot_id: str, ticker: str, window: float) -> dict:
    summary_path = summary_path_from_id(snapshot_id)
    summary, point, rows, history, gex_snapshot = chart_payload_from_summary_path(summary_path, ticker, window)
    points, ribbon = day_series_from_summary_path(summary_path, ticker, window)
    if not points:
        points = [point]
    if not ribbon:
        ribbon = [gex_snapshot]
    snapshot_vn = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
    if pd.notna(snapshot_vn):
        summary["snapshot_vn"] = snapshot_vn.tz_convert(VN_TZ).isoformat()
    return {
        "points": points,
        "gex_ribbon": ribbon,
        "history": history,
        "by_strike": rows,
        "latest_summary": summary,
        "levels_summary": summary,
        "latest_error": None,
        "running": False,
        "successes": 0,
        "failures": 0,
        "next_fetch": None,
        "session": session_from_summary(summary),
        "history_snapshot_id": snapshot_id,
    }


def load_latest(ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
    return chart_payload_from_summary_path(latest_summary_path(ticker), ticker, window)


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
    deadline = None
    if args.duration_minutes is not None:
        deadline = time.monotonic() + max(0, args.duration_minutes) * 60
    next_run = time.monotonic()
    while True:
        if deadline is not None and time.monotonic() > deadline:
            break
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
                    summary, point, rows, history, gex_snapshot = load_latest(args.ticker, args.window)
                    point_ts = pd.to_datetime(point.get("time"), errors="coerce", utc=True)
                    collect_start = pd.Timestamp(state.session["collection_start_utc"])
                    if pd.notna(point_ts) and point_ts >= collect_start:
                        if not any(p.get("time") == point.get("time") for p in state.points):
                            state.points.append(point)
                        if not any(r.get("time") == gex_snapshot.get("time") for r in state.gex_ribbon):
                            state.gex_ribbon.append(gex_snapshot)
                        if state.levels_summary is None:
                            state.levels_summary = summary
                    state.latest_summary = summary
                    state.by_strike = rows
                    state.history = history
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
    ticker: str = "QQQ"
    window: float = 14.0

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send(HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self.send(json.dumps(self.state.snapshot(), default=str, allow_nan=False), "application/json")
            return
        if parsed.path == "/api/history":
            payload = {"snapshots": list_history_choices(self.ticker)}
            self.send(json.dumps(payload, default=str, allow_nan=False), "application/json")
            return
        if parsed.path == "/api/snapshot":
            snapshot_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                payload = load_snapshot_state(snapshot_id, self.ticker, self.window)
                self.send(json.dumps(payload, default=str, allow_nan=False), "application/json")
            except Exception as exc:
                self.send(json.dumps({"error": str(exc)}), "application/json", HTTPStatus.BAD_REQUEST)
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
    state.session["collection_start_utc"] = collection_start_utc(args.collect_start_vn)
    state.points, state.gex_ribbon = seed_session_data(args.ticker, state.session, args.window)
    state.levels_summary = seed_levels_summary(args.ticker, state.session)
    Handler.state = state
    Handler.ticker = args.ticker.upper()
    Handler.window = args.window
    worker = threading.Thread(target=collector, args=(args, state), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Live dashboard: {url}", flush=True)
    duration = "until stopped" if args.duration_minutes is None else f"for {args.duration_minutes} minutes"
    print(f"Collecting {args.ticker.upper()} every {args.interval_seconds}s {duration}.", flush=True)
    print(f"Volatility Flow + Heat Tracker start at {args.collect_start_vn} Vietnam time.", flush=True)
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
