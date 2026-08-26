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

# Minutes before NY market open (9:30 ET) that Volatility Flow + Heat Tracker
# start collecting data. Anchored to NY time (not a VN wall-clock string) so it
# self-adjusts across US DST changes - only this one number needs tuning.
COLLECT_START_OFFSET_MIN = 30.0


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


def collection_start_utc(market_open_utc: str, offset_minutes: float | None = None) -> str:
    offset_minutes = COLLECT_START_OFFSET_MIN if offset_minutes is None else offset_minutes
    open_ts = pd.Timestamp(market_open_utc)
    return (open_ts - pd.Timedelta(minutes=offset_minutes)).isoformat()


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
  .replay-controls {
    display: none; align-items: center; gap: 7px; color: #94A3B8;
    font-size: 12px; font-weight: 600;
  }
  .replay-controls.active { display: inline-flex; }
  .replay-button {
    height: 28px; min-width: 34px; border: 1px solid #263244; border-radius: 6px;
    background: #0F172A; color: #E5E7EB; cursor: pointer; font-weight: 700;
  }
  .replay-button:disabled { opacity: 0.45; cursor: default; }
  .replay-slider { width: 180px; accent-color: var(--accent-call); }
  .replay-time {
    width: 78px; color: #CBD5E1; font-family: Menlo, Consolas, monospace;
    font-size: 12px;
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
  .heat-controls {
    margin-left: auto; display: flex; align-items: center; gap: 10px;
    color: #94A3B8; font-size: 11px; font-weight: 600;
  }
  .heat-controls select, .heat-controls button {
    height: 24px; border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 0 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .heat-controls button.active { color: var(--accent-call); border-color: rgba(34,211,238,0.45); }
  .heat-controls input[type="range"] { width: 90px; accent-color: var(--accent-call); }
  .flow-wrap { height: 430px; position: relative; padding: 0; }
  .flow-controls {
    margin-left: auto; display: flex; align-items: center; gap: 8px;
    color: #94A3B8; font-size: 11px; font-weight: 600;
  }
  .flow-controls select, .flow-controls button {
    height: 24px; border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 0 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .heat-tooltip {
    position: absolute; pointer-events: none; z-index: 5; background: #111827;
    border: 1px solid #374151; border-radius: 6px; padding: 8px 10px; font-size: 12px;
    line-height: 1.5; color: #E5E7EB; white-space: nowrap;
  }
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
    <div class="title">QQQ Live Option Flow</div>
    <div class="meta" id="status">Starting...</div>
  </div>
  <div class="top-actions">
    <label class="history-control">History <select id="historySelect"><option value="live">Live realtime</option></select></label>
    <div class="replay-controls" id="replayControls">
      <button class="replay-button" id="replayPrev" type="button">&lt;&lt;</button>
      <button class="replay-button" id="replayPlay" type="button">Play</button>
      <button class="replay-button" id="replayNext" type="button">&gt;&gt;</button>
      <input class="replay-slider" id="replaySlider" type="range" min="0" max="0" value="0">
      <span class="replay-time" id="replayTime">--:--</span>
    </div>
    <div class="color-controls">
      <label class="color-control">Call <input id="callColor" type="color" value="#22D3EE"><input id="callHex" class="hex-input" value="#22D3EE" spellcheck="false"></label>
      <label class="color-control">Put <input id="putColor" type="color" value="#F59E0B"><input id="putHex" class="hex-input" value="#F59E0B" spellcheck="false"></label>
    </div>
    <div class="meta" id="clock"></div>
  </div>
</div>
<div class="grid">
  <div class="panel wide">
    <div class="panel-header">
      <span class="dot"></span>Volatility Flow
      <div class="flow-controls">
        <select id="flowInterval">
          <option value="1">1m</option>
          <option value="5">5m</option>
          <option value="15">15m</option>
          <option value="30">30m</option>
        </select>
        <select id="flowMoneyness">
          <option value="ALL">ALL</option>
          <option value="ITM">ITM</option>
          <option value="ATM" selected>ATM</option>
          <option value="OTM">OTM</option>
        </select>
        <select id="flowExpiry">
          <option value="ALL">ALL</option>
          <option value="0DTE" selected>0DTE</option>
        </select>
        <button id="flowResetZoom" type="button">Reset zoom</button>
      </div>
    </div>
    <div class="body flow-wrap" id="flowWrap">
      <canvas id="flowCanvas" style="width:100%; height:100%; display:block;"></canvas>
      <div id="flowTooltip" class="heat-tooltip" style="display:none;"></div>
    </div>
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
      <div class="heat-controls">
        <select id="heatInterval">
          <option value="1">1m</option>
          <option value="5">5m</option>
          <option value="15">15m</option>
          <option value="30">30m</option>
        </select>
        <button id="heatModeToggle" type="button">Blocks</button>
        <span>Intensity</span>
        <input id="heatIntensity" type="range" min="0" max="100" value="35">
        <button id="heatResetZoom" type="button">Reset zoom</button>
      </div>
    </div>
    <div class="body" id="gexribbonWrap" style="height:440px; position:relative; padding:0;">
      <canvas id="gexribbonCanvas" style="width:100%; height:100%; display:block;"></canvas>
      <div id="gexribbonTooltip" class="heat-tooltip" style="display:none;"></div>
    </div>
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
let replaySnapshots = [];
let replayIndex = 0;
let replayTimer = null;

function resetChartLocks() {
  lastChartKey = "";
  flowHasInitialized = false;
}

function stopReplay() {
  if (replayTimer) {
    clearInterval(replayTimer);
    replayTimer = null;
  }
  const play = document.getElementById("replayPlay");
  if (play) play.textContent = "Play";
}

function setReplayVisible(visible) {
  document.getElementById("replayControls")?.classList.toggle("active", visible);
  if (!visible) stopReplay();
}

function updateReplayControls() {
  const slider = document.getElementById("replaySlider");
  const time = document.getElementById("replayTime");
  const prev = document.getElementById("replayPrev");
  const next = document.getElementById("replayNext");
  const play = document.getElementById("replayPlay");
  const max = Math.max(0, replaySnapshots.length - 1);
  if (slider) {
    slider.max = String(max);
    slider.value = String(Math.min(replayIndex, max));
    slider.disabled = replaySnapshots.length <= 1;
  }
  if (time) time.textContent = replaySnapshots[replayIndex] ? timeET(replaySnapshots[replayIndex].snapshot_utc).replace(" ET", "") : "--:--";
  if (prev) prev.disabled = replayIndex <= 0;
  if (next) next.disabled = replayIndex >= max;
  if (play) play.disabled = replaySnapshots.length <= 1;
}

async function loadReplayIndex(index) {
  if (!replaySnapshots.length) return;
  replayIndex = Math.max(0, Math.min(index, replaySnapshots.length - 1));
  updateReplayControls();
  await loadHistoricalSnapshot(replaySnapshots[replayIndex].id, false);
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

function computeArvPct(points, lookback = 20) {
  const rows = points.map(p => ({
    time: new Date(p.time).getTime(),
    spot: Number(p.spot)
  }));
  const returns = [];
  const out = [];
  let prev = null;
  for (const row of rows) {
    if (Number.isFinite(row.time) && Number.isFinite(row.spot) && row.spot > 0 && prev && row.time > prev.time) {
      const minutes = Math.max((row.time - prev.time) / 60000, 1);
      returns.push({value: Math.log(row.spot / prev.spot), minutes});
    }
    prev = Number.isFinite(row.time) && Number.isFinite(row.spot) && row.spot > 0 ? row : prev;
    const window = returns.slice(-lookback);
    if (window.length < 3) {
      out.push(null);
      continue;
    }
    const mean = window.reduce((sum, item) => sum + item.value, 0) / window.length;
    const variance = window.reduce((sum, item) => sum + Math.pow(item.value - mean, 2), 0) / Math.max(window.length - 1, 1);
    const medianMinutes = window.map(item => item.minutes).sort((a, b) => a - b)[Math.floor(window.length / 2)] || 1;
    out.push(Math.sqrt(variance) * Math.sqrt((252 * 390) / medianMinutes) * 100);
  }
  return out;
}

function drawFlow(points, session) {
  const flowStartUtc = session && session.collection_start_utc
    ? new Date(session.collection_start_utc)
    : (session && session.market_open_utc ? new Date(new Date(session.market_open_utc).getTime() - 30 * 60 * 1000) : null);
  if (flowStartUtc && !Number.isNaN(flowStartUtc.getTime())) {
    points = (points || []).filter(p => p.time && new Date(p.time).getTime() >= flowStartUtc.getTime());
  }
  const canvas = document.getElementById("flowCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const wrap = document.getElementById("flowWrap");
  const rect = wrap ? wrap.getBoundingClientRect() : {width: 900, height: 430};
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, rect.width), cssH = Math.max(1, rect.height);
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cssW, cssH);

  const margin = {l: 64, r: 62, t: 22, b: 28};
  const gap = 6;
  const plotW = cssW - margin.l - margin.r;
  const plotH = cssH - margin.t - margin.b;
  const topH = Math.floor(plotH * 0.65);
  const bottomH = plotH - topH - gap;
  const top = {x: margin.l, y: margin.t, w: plotW, h: topH};
  const bot = {x: margin.l, y: margin.t + topH + gap, w: plotW, h: bottomH};
  const intervalMin = Number(document.getElementById("flowInterval")?.value || 1);
  const intervalMs = intervalMin * 60 * 1000;
  const startUtc = flowStartUtc || (points[0]?.time ? new Date(points[0].time) : new Date());
  const closeUtc = session?.market_close_utc ? new Date(session.market_close_utc) : new Date(Math.max(...points.map(p => new Date(p.time).getTime()), startUtc.getTime()));
  const bucketCount = Math.max(1, Math.ceil((closeUtc.getTime() - startUtc.getTime()) / intervalMs));
  const buckets = Array.from({length: bucketCount}, (_, i) => new Date(startUtc.getTime() + i * intervalMs));
  const rows = buckets.map((bucket, i) => ({bucket, i, iv: null, arv: null, spot: null, call: 0, put: 0, net: 0}));
  const arv = computeArvPct(points);
  points.forEach((p, i) => {
    const t = new Date(p.time).getTime();
    const idx = Math.floor((t - startUtc.getTime()) / intervalMs);
    if (idx < 0 || idx >= rows.length) return;
    const row = rows[idx];
    const iv = Number(p.atm_iv);
    const spot = Number(p.spot);
    if (Number.isFinite(iv)) row.iv = iv;
    if (Number.isFinite(Number(arv[i]))) row.arv = Number(arv[i]);
    if (Number.isFinite(spot)) row.spot = spot;
    row.call += Number(p.call_volume || 0);
    row.put += Number(p.put_volume || 0);
  });
  let cum = 0;
  rows.forEach(row => {
    cum += row.call - row.put;
    row.net = cum / 1000;
  });
  const validIv = rows.flatMap(r => [r.iv, r.arv]).filter(Number.isFinite);
  const validSpot = rows.map(r => r.spot).filter(Number.isFinite);
  const validNet = rows.map(r => r.net).filter(Number.isFinite);
  const ivMax = Math.max(20, ...validIv) * 1.15;
  const spotMin = Math.min(...validSpot);
  const spotMax = Math.max(...validSpot);
  const spotPad = Math.max(0.5, (spotMax - spotMin) * 0.18);
  const netAbs = Math.max(5, ...validNet.map(v => Math.abs(v))) * 1.15;
  const x = i => top.x + (i + 0.5) * (top.w / rows.length);
  const yPct = v => top.y + top.h - (v / ivMax) * top.h;
  const ySpot = v => top.y + top.h - ((v - (spotMin - spotPad)) / ((spotMax + spotPad) - (spotMin - spotPad))) * top.h;
  const yNet = v => bot.y + bot.h / 2 - (v / netAbs) * (bot.h / 2);

  ctx.strokeStyle = "rgba(148,163,184,0.18)";
  ctx.setLineDash([5, 5]);
  ctx.beginPath(); ctx.moveTo(top.x, yPct(20)); ctx.lineTo(top.x + top.w, yPct(20)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = "rgba(148,163,184,0.28)";
  ctx.beginPath(); ctx.moveTo(bot.x, yNet(0)); ctx.lineTo(bot.x + bot.w, yNet(0)); ctx.stroke();
  ctx.fillStyle = COLORS.muted;
  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.textAlign = "right";
  ctx.fillText(Math.round(ivMax) + "%", top.x - 8, top.y + 12);
  ctx.fillText(Math.round(netAbs) + "K", bot.x - 8, bot.y + 12);
  ctx.fillText("-" + Math.round(netAbs) + "K", bot.x - 8, bot.y + bot.h - 2);
  ctx.save();
  ctx.translate(14, top.y + top.h / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("IV %", 0, 0);
  ctx.restore();

  function line(series, yFn, color, width = 1.5) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    let started = false;
    rows.forEach((r, i) => {
      const v = r[series];
      if (!Number.isFinite(v)) return;
      const px = x(i), py = yFn(v);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }
  line("iv", yPct, COLORS.cyan, 1.7);
  line("arv", yPct, COLORS.orange, 1.5);
  line("spot", ySpot, COLORS.spot, 1.3);

  ctx.beginPath();
  rows.forEach((r, i) => {
    const px = x(i), py = yNet(r.net);
    if (i === 0) ctx.moveTo(px, yNet(0));
    ctx.lineTo(px, py);
  });
  for (let i = rows.length - 1; i >= 0; i--) ctx.lineTo(x(i), yNet(0));
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, bot.y, 0, bot.y + bot.h);
  grad.addColorStop(0, rgbaFromHex(COLORS.cyan, 0.36));
  grad.addColorStop(0.5, "rgba(0,0,0,0)");
  grad.addColorStop(1, rgbaFromHex(COLORS.orange, 0.36));
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.strokeStyle = COLORS.cyan;
  ctx.lineWidth = 1.1;
  ctx.beginPath();
  rows.forEach((r, i) => {
    const px = x(i), py = yNet(r.net);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  ctx.stroke();

  const latest = [...rows].reverse().find(r => Number.isFinite(r.iv) || Number.isFinite(r.arv) || Number.isFinite(r.spot));
  ctx.textAlign = "left";
  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.fillStyle = COLORS.cyan;
  ctx.fillText("IV " + (latest?.iv == null ? "NA" : latest.iv.toFixed(1) + "%"), top.x + 2, top.y + 12);
  ctx.fillStyle = COLORS.orange;
  ctx.fillText(" · ARV " + (latest?.arv == null ? "NA" : latest.arv.toFixed(1) + "%"), top.x + 72, top.y + 12);
  if (latest && Number.isFinite(latest.spot)) {
    const py = ySpot(latest.spot);
    ctx.fillStyle = "#F8FAFC";
    ctx.fillRect(top.x + top.w - 50, py - 9, 48, 18);
    ctx.fillStyle = "#020617";
    ctx.textAlign = "center";
    ctx.fillText(latest.spot.toFixed(2), top.x + top.w - 26, py + 4);
  }
  const tickEvery = Math.max(1, Math.floor(rows.length / 8));
  ctx.fillStyle = COLORS.muted;
  ctx.textAlign = "center";
  rows.forEach((r, i) => {
    if (i % tickEvery !== 0 && i !== rows.length - 1) return;
    ctx.fillText(r.bucket.toLocaleTimeString("en-US", {timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false}), x(i), cssH - 8);
  });

  canvas.onmousemove = ev => {
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const idx = Math.max(0, Math.min(rows.length - 1, Math.floor((mx - top.x) / (top.w / rows.length))));
    const r = rows[idx];
    drawFlow(points, session);
    const c = canvas.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.strokeStyle = "rgba(248,250,252,0.45)";
    c.beginPath(); c.moveTo(x(idx), top.y); c.lineTo(x(idx), bot.y + bot.h); c.stroke();
    const tip = document.getElementById("flowTooltip");
    if (tip) {
      tip.style.display = "block";
      tip.style.left = Math.min(cssW - 250, mx + 14) + "px";
      tip.style.top = Math.max(4, ev.clientY - rect.top + 12) + "px";
      tip.innerHTML = `IV ${Number.isFinite(r.iv) ? r.iv.toFixed(1) + "%" : "NA"} &nbsp; ARV ${Number.isFinite(r.arv) ? r.arv.toFixed(1) + "%" : "NA"} &nbsp; Call - Put ${r.net.toFixed(1)}K<br>` +
        `TRADED IN BUCKET: Call contracts ${(r.call / 1000).toFixed(1)}K · Put contracts ${(r.put / 1000).toFixed(1)}K · price ${Number.isFinite(r.spot) ? r.spot.toFixed(2) : "NA"}`;
    }
  };
  canvas.onmouseleave = () => {
    const tip = document.getElementById("flowTooltip");
    if (tip) tip.style.display = "none";
    drawFlow(points, session);
  };
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

// ---- Heat Tracker: hand-rolled canvas 2D renderer ----
// Band-scale axes (real strikes / fixed time buckets), snapshot-per-bucket
// GEX values, diverging intensity-floor color map, and view-only zoom/pan —
// see tasks context: rebuilt per user's own architecture spec of the
// Quantdecay heat tracker (Plotly could not express band scales/pure view
// transforms, so this chart no longer goes through Plotly.react at all).

function loadHeatPrefs() {
  let interval = 1, mode = "blocks", intensity = 35;
  try {
    const storedInterval = Number(localStorage.getItem("qqqHeatInterval"));
    if ([1, 5, 15, 30].includes(storedInterval)) interval = storedInterval;
    const storedMode = localStorage.getItem("qqqHeatMode");
    if (storedMode === "dots" || storedMode === "blocks") mode = storedMode;
    const storedIntensity = Number(localStorage.getItem("qqqHeatIntensity"));
    if (Number.isFinite(storedIntensity) && storedIntensity >= 0 && storedIntensity <= 100) intensity = storedIntensity;
  } catch (_err) {}
  return {interval, mode, intensity};
}

const heatPrefs = loadHeatPrefs();
const heatState = {
  ribbon: [], points: [], summary: {}, session: null,
  interval: heatPrefs.interval, mode: heatPrefs.mode, intensity: heatPrefs.intensity,
  viewX: [0, 1], viewY: [0, 1], sessionKey: "", grid: null,
};
let heatDrag = null;

function lerpHex(hexA, hexB, t) {
  const a = hexToRgb(hexA), b = hexToRgb(hexB);
  const r = Math.round(a.r + (b.r - a.r) * t);
  const g = Math.round(a.g + (b.g - a.g) * t);
  const bl = Math.round(a.b + (b.b - a.b) * t);
  return `rgb(${r},${g},${bl})`;
}

function heatIntensityT(absGex, maxAbs, floorFrac) {
  const denom = Math.max(1e-9, maxAbs - floorFrac * maxAbs);
  const raw = Math.max(0, Math.min(1, (absGex - floorFrac * maxAbs) / denom));
  return Math.pow(raw, 0.6);
}

function heatColorFromT(gex, t) {
  return lerpHex("#000000", gex > 0 ? COLORS.cyan : COLORS.orange, t);
}

function strikeContinuousIndex(strikes, value) {
  if (!strikes.length || !Number.isFinite(value)) return null;
  if (value <= strikes[0]) return 0;
  if (value >= strikes[strikes.length - 1]) return strikes.length - 1;
  for (let i = 0; i < strikes.length - 1; i++) {
    if (value >= strikes[i] && value <= strikes[i + 1]) {
      const span = strikes[i + 1] - strikes[i];
      return span > 0 ? i + (value - strikes[i]) / span : i;
    }
  }
  return null;
}

function fmtStrikeLabel(v) {
  return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
}

function fmtHM(d) {
  return d.toLocaleTimeString("en-US", {timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false});
}

function clampView(view, minBound, maxBound) {
  let [v0, v1] = view;
  const span = v1 - v0;
  if (span >= maxBound - minBound) return [minBound, maxBound];
  if (v0 < minBound) { v0 = minBound; v1 = v0 + span; }
  if (v1 > maxBound) { v1 = maxBound; v0 = v1 - span; }
  return [v0, v1];
}

// Builds the (strike x time-bucket) grid: last-known net_gex per cell (not a
// delta), a 95th-percentile color scale robust to outliers, and the
// wall/flip level lines — ported from the previous Plotly implementation's
// selection heuristics, just rendered on canvas instead of as shapes.
function buildHeatGrid(state) {
  const {summary, session} = state;
  const sessionOpenForCutoff = session && session.market_open_utc ? new Date(session.market_open_utc) : null;
  const cutoffCandidate = session && session.collection_start_utc
    ? new Date(session.collection_start_utc)
    : (sessionOpenForCutoff ? new Date(sessionOpenForCutoff.getTime() - 5 * 60 * 1000) : null);
  const dataCutoffUtc = cutoffCandidate && !Number.isNaN(cutoffCandidate.getTime()) ? cutoffCandidate : null;
  let ribbon = state.ribbon || [];
  let points = state.points || [];
  if (dataCutoffUtc) {
    ribbon = ribbon.filter(s => s.time && new Date(s.time).getTime() >= dataCutoffUtc.getTime());
    points = points.filter(p => p.time && new Date(p.time).getTime() >= dataCutoffUtc.getTime());
  }
  ribbon = [...ribbon].sort((a, b) => new Date(a.time) - new Date(b.time));

  let rawMaxAbs = 1;
  for (const snap of ribbon) {
    for (const row of snap.rows || []) {
      const gex = Number(row.net_gex);
      if (Number.isFinite(Number(row.strike)) && Number.isFinite(gex) && gex !== 0) {
        rawMaxAbs = Math.max(rawMaxAbs, Math.abs(gex));
      }
    }
  }
  const minDrawAbs = Math.max(50000, rawMaxAbs * 0.002);

  const sessionOpenUtc = session && session.market_open_utc ? new Date(session.market_open_utc) : null;
  const sessionCloseUtc = session && session.market_close_utc ? new Date(session.market_close_utc) : null;
  const dataTimes = [...ribbon.map(s => s.time), ...points.map(p => p.time)]
    .filter(Boolean).map(t => new Date(t)).filter(d => !Number.isNaN(d.getTime()));
  let bucketStartUtc = null, bucketEndUtc = null;
  if (sessionOpenUtc && sessionCloseUtc && !Number.isNaN(sessionOpenUtc.getTime())) {
    bucketStartUtc = new Date(sessionOpenUtc.getTime() - 30 * 60 * 1000);
    const minEndUtc = new Date(sessionOpenUtc.getTime() + 5 * 60 * 1000);
    const lastDataUtc = dataTimes.length ? new Date(Math.max(...dataTimes.map(d => d.getTime()))) : minEndUtc;
    bucketEndUtc = new Date(Math.min(sessionCloseUtc.getTime(), Math.max(lastDataUtc.getTime(), minEndUtc.getTime())));
  } else if (dataTimes.length) {
    bucketStartUtc = new Date(Math.min(...dataTimes.map(d => d.getTime())));
    bucketEndUtc = new Date(Math.max(...dataTimes.map(d => d.getTime())));
  }
  const intervalMs = Math.max(1, Number(state.interval) || 1) * 60000;
  const bucketCount = bucketStartUtc && bucketEndUtc
    ? Math.max(1, Math.ceil((bucketEndUtc.getTime() - bucketStartUtc.getTime()) / intervalMs))
    : 1;
  const bucketIndexFor = (t) => {
    if (!bucketStartUtc) return 0;
    const idx = Math.floor((new Date(t).getTime() - bucketStartUtc.getTime()) / intervalMs);
    return Math.max(0, Math.min(bucketCount - 1, idx));
  };

  const exposureByStrike = new Map();
  const grid = new Map();
  const gridAbsValues = [];
  for (const snap of ribbon) {
    const bucketIdx = bucketIndexFor(snap.time);
    for (const row of snap.rows || []) {
      const strike = Number(row.strike);
      const gex = Number(row.net_gex);
      if (!Number.isFinite(strike) || !Number.isFinite(gex) || Math.abs(gex) < minDrawAbs) continue;
      const aggregate = exposureByStrike.get(strike) || {strike, posMax: 0, negMax: 0, posSum: 0, negSum: 0, posCount: 0, negCount: 0};
      if (gex > 0) {
        aggregate.posMax = Math.max(aggregate.posMax, gex);
        aggregate.posSum += gex;
        aggregate.posCount += 1;
      } else {
        aggregate.negMax = Math.max(aggregate.negMax, -gex);
        aggregate.negSum += -gex;
        aggregate.negCount += 1;
      }
      exposureByStrike.set(strike, aggregate);
      grid.set(bucketIdx + "|" + strike, {gex, time: snap.time});
      gridAbsValues.push(Math.abs(gex));
    }
  }
  const strikes = [...exposureByStrike.keys()].sort((a, b) => a - b);

  gridAbsValues.sort((a, b) => a - b);
  const maxAbs = gridAbsValues.length
    ? gridAbsValues[Math.min(gridAbsValues.length - 1, Math.ceil(gridAbsValues.length * 0.95) - 1)]
    : 1;

  const spot = Number(summary?.spot);
  const dynamicLevels = [...exposureByStrike.values()]
    .flatMap(level => [
      {strike: level.strike, net_gex: level.posMax, side: "call", score: level.posMax * 0.75 + (level.posCount ? level.posSum / level.posCount : 0) * 0.25},
      {strike: level.strike, net_gex: -level.negMax, side: "put", score: level.negMax * 0.75 + (level.negCount ? level.negSum / level.negCount : 0) * 0.25},
    ])
    .filter(level => level.score > rawMaxAbs * 0.08);
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
  const wallLines = [];
  if (Number.isFinite(spot)) {
    const callLabels = ["CALL RESISTANCE", "CALL WALL 2", "CALL WALL 3"];
    uniqueLevels(levels.filter(l => l.strike > spot && l.net_gex > 0), "call").forEach((l, i) => {
      wallLines.push({strike: l.strike, color: COLORS.cyan, dash: i === 0 ? "solid" : "dash", label: callLabels[i] + " " + l.strike.toFixed(0), side: "right"});
    });
    const putLabels = ["PUT SUPPORT", "PUT WALL 2", "PUT WALL 3"];
    uniqueLevels(levels.filter(l => l.strike < spot && l.net_gex < 0), "put").forEach((l, i) => {
      wallLines.push({strike: l.strike, color: COLORS.orange, dash: i === 0 ? "solid" : "dash", label: putLabels[i] + " " + l.strike.toFixed(0), side: "left"});
    });
  }
  if (summary?.gamma_flip) {
    wallLines.push({strike: Number(summary.gamma_flip), color: "#C084FC", dash: "dot", label: "GAMMA FLIP " + Number(summary.gamma_flip).toFixed(2), side: "right"});
  }

  return {strikes, bucketCount, bucketStartUtc, bucketEndUtc, intervalMs, grid, maxAbs, wallLines, points, spot};
}

function heatCanvasEl() {
  return document.getElementById("gexribbonCanvas");
}

function heatMetrics() {
  const wrap = document.getElementById("gexribbonWrap");
  const rect = wrap ? wrap.getBoundingClientRect() : {width: 600, height: 400};
  const cssW = Math.max(1, rect.width), cssH = Math.max(1, rect.height);
  const margin = {l: 62, r: 132, t: 10, b: 26};
  const plotW = Math.max(10, cssW - margin.l - margin.r);
  const plotH = Math.max(10, cssH - margin.t - margin.b);
  return {cssW, cssH, margin, plotW, plotH};
}

function drawHeatColorbar(ctx, cssW, margin) {
  const x = cssW - 26, yTop = margin.t + 2, h = 70, w = 10;
  const grad = ctx.createLinearGradient(0, yTop, 0, yTop + h);
  grad.addColorStop(0, COLORS.cyan);
  grad.addColorStop(0.5, "#000000");
  grad.addColorStop(1, COLORS.orange);
  ctx.fillStyle = grad;
  ctx.fillRect(x, yTop, w, h);
  ctx.strokeStyle = "rgba(148,163,184,0.4)";
  ctx.strokeRect(x, yTop, w, h);
  ctx.fillStyle = COLORS.muted;
  ctx.font = "9px Menlo, Consolas, monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.fillText("+", x - 4, yTop + 4);
  ctx.fillText("-", x - 4, yTop + h - 4);
}

function renderHeatCanvas() {
  const canvas = heatCanvasEl();
  if (!canvas) return;
  const g = heatState.grid;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const {cssW, cssH, margin, plotW, plotH} = heatMetrics();
  const targetW = Math.round(cssW * dpr), targetH = Math.round(cssH * dpr);
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width = targetW;
    canvas.height = targetH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = COLORS.panel;
  ctx.fillRect(0, 0, cssW, cssH);
  if (!g || !g.strikes.length || !g.bucketCount) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for data...", 16, 24);
    return;
  }

  const [vx0, vx1] = heatState.viewX;
  const [vy0, vy1] = heatState.viewY;
  const xToPx = (bucketPos) => margin.l + ((bucketPos - vx0) / (vx1 - vx0)) * plotW;
  const yToPx = (strikeIdxPos) => margin.t + (1 - (strikeIdxPos - vy0) / (vy1 - vy0)) * plotH;
  const cellW = plotW / (vx1 - vx0);
  const cellH = plotH / (vy1 - vy0);
  const floorFrac = heatState.intensity / 100;

  const bucketFrom = Math.max(0, Math.floor(vx0) - 1);
  const bucketTo = Math.min(g.bucketCount - 1, Math.ceil(vx1) + 1);
  const strikeFrom = Math.max(0, Math.floor(vy0) - 1);
  const strikeTo = Math.min(g.strikes.length - 1, Math.ceil(vy1) + 1);

  ctx.save();
  ctx.beginPath();
  ctx.rect(margin.l, margin.t, plotW, plotH);
  ctx.clip();
  for (let bi = bucketFrom; bi <= bucketTo; bi++) {
    const cx = xToPx(bi + 0.5);
    if (cx < margin.l - cellW || cx > margin.l + plotW + cellW) continue;
    for (let si = strikeFrom; si <= strikeTo; si++) {
      const cell = g.grid.get(bi + "|" + g.strikes[si]);
      if (!cell) continue;
      const cy = yToPx(si + 0.5);
      const t = heatIntensityT(Math.abs(cell.gex), g.maxAbs, floorFrac);
      ctx.fillStyle = heatColorFromT(cell.gex, t);
      if (heatState.mode === "dots") {
        const rx = Math.max(1.1, cellW * 0.5 * (0.55 + 0.35 * t));
        const ry = Math.max(1.1, cellH * 0.5 * (0.15 + 0.45 * t));
        ctx.beginPath();
        ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
        ctx.fill();
      } else {
        const bw = Math.max(1, cellW * 0.9);
        const bh = Math.max(1, cellH * 0.35);
        ctx.fillRect(cx - bw / 2, cy - bh / 2, bw, bh);
      }
    }
  }

  const spotPts = [];
  for (const p of g.points) {
    const spotV = Number(p.spot);
    if (!Number.isFinite(spotV)) continue;
    const bPos = (new Date(p.time).getTime() - g.bucketStartUtc.getTime()) / g.intervalMs;
    const sIdx = strikeContinuousIndex(g.strikes, spotV);
    if (sIdx === null) continue;
    spotPts.push([xToPx(bPos), yToPx(sIdx)]);
  }
  if (spotPts.length > 1) {
    ctx.strokeStyle = "#F8FAFC";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    spotPts.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
    ctx.stroke();
  }

  for (const w of g.wallLines) {
    const sIdx = strikeContinuousIndex(g.strikes, w.strike);
    if (sIdx === null) continue;
    const y = yToPx(sIdx);
    if (y < margin.t - 4 || y > margin.t + plotH + 4) continue;
    ctx.strokeStyle = w.color;
    ctx.lineWidth = w.dash === "solid" ? 2 : 1.4;
    ctx.setLineDash(w.dash === "solid" ? [] : w.dash === "dot" ? [2, 3] : [6, 4]);
    ctx.beginPath();
    ctx.moveTo(margin.l, y);
    ctx.lineTo(margin.l + plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.restore();

  ctx.fillStyle = COLORS.muted;
  ctx.font = "10px Menlo, Consolas, monospace";
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  const labelStep = Math.max(1, Math.ceil((vy1 - vy0) / (plotH / 22)));
  for (let si = strikeFrom; si <= strikeTo; si += labelStep) {
    const y = yToPx(si + 0.5);
    if (y < margin.t || y > margin.t + plotH) continue;
    ctx.fillText(fmtStrikeLabel(g.strikes[si]), margin.l - 8, y);
  }

  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const xLabelStep = Math.max(1, Math.ceil((vx1 - vx0) / (plotW / 60)));
  for (let bi = bucketFrom; bi <= bucketTo; bi += xLabelStep) {
    const x = xToPx(bi);
    if (x < margin.l || x > margin.l + plotW) continue;
    const t = new Date(g.bucketStartUtc.getTime() + bi * g.intervalMs);
    ctx.fillText(fmtHM(t), x, margin.t + plotH + 6);
  }

  ctx.font = "10px Menlo, Consolas, monospace";
  ctx.textBaseline = "middle";
  for (const w of g.wallLines) {
    const sIdx = strikeContinuousIndex(g.strikes, w.strike);
    if (sIdx === null) continue;
    const y = yToPx(sIdx);
    if (y < margin.t || y > margin.t + plotH) continue;
    const textW = ctx.measureText(w.label).width;
    ctx.fillStyle = "rgba(5,7,11,0.75)";
    if (w.side === "left") {
      ctx.fillRect(2, y - 8, textW + 8, 16);
      ctx.fillStyle = w.color;
      ctx.textAlign = "left";
      ctx.fillText(w.label, 6, y);
    } else {
      const x0 = margin.l + plotW + 4;
      ctx.fillRect(x0, y - 8, textW + 8, 16);
      ctx.fillStyle = w.color;
      ctx.textAlign = "left";
      ctx.fillText(w.label, x0 + 4, y);
    }
  }

  if (Number.isFinite(g.spot)) {
    const sIdx = strikeContinuousIndex(g.strikes, g.spot);
    if (sIdx !== null) {
      const y = yToPx(sIdx);
      if (y >= margin.t && y <= margin.t + plotH) {
        const label = "SPOT " + g.spot.toFixed(2);
        const textW = ctx.measureText(label).width;
        ctx.fillStyle = "rgba(203,213,225,0.18)";
        ctx.fillRect(margin.l + plotW + 4, y - 8, textW + 8, 16);
        ctx.fillStyle = "#CBD5E1";
        ctx.textAlign = "left";
        ctx.fillText(label, margin.l + plotW + 8, y);
      }
    }
  }

  drawHeatColorbar(ctx, cssW, margin);
}

function zoomAxis(view, cursorPx, originPx, spanPx, factor, minBound, maxBound, invert) {
  const frac = invert ? 1 - (cursorPx - originPx) / spanPx : (cursorPx - originPx) / spanPx;
  const cursorVal = view[0] + frac * (view[1] - view[0]);
  let newSpan = Math.max(2, Math.min(maxBound - minBound, (view[1] - view[0]) * factor));
  let v0 = cursorVal - frac * newSpan;
  let v1 = v0 + newSpan;
  if (v0 < minBound) { v0 = minBound; v1 = v0 + newSpan; }
  if (v1 > maxBound) { v1 = maxBound; v0 = v1 - newSpan; }
  return [v0, v1];
}

function onHeatWheel(e) {
  const g = heatState.grid;
  if (!g) return;
  e.preventDefault();
  const canvas = heatCanvasEl();
  const rect = canvas.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  const {margin, plotW, plotH} = heatMetrics();
  const factor = Math.exp((e.deltaY > 0 ? 1 : -1) * 0.15);
  heatState.viewX = zoomAxis(heatState.viewX, px, margin.l, plotW, factor, 0, g.bucketCount, false);
  heatState.viewY = zoomAxis(heatState.viewY, py, margin.t, plotH, factor, 0, g.strikes.length, true);
  renderHeatCanvas();
}

function onHeatMouseDown(e) {
  if (!heatState.grid) return;
  heatDrag = {startX: e.clientX, startY: e.clientY, viewX: [...heatState.viewX], viewY: [...heatState.viewY]};
}

function hideHeatTooltip() {
  const tooltip = document.getElementById("gexribbonTooltip");
  if (tooltip) tooltip.style.display = "none";
}

function showHeatTooltip(px, py) {
  const g = heatState.grid;
  const tooltip = document.getElementById("gexribbonTooltip");
  if (!g || !tooltip) return;
  const {margin, plotW, plotH} = heatMetrics();
  if (px < margin.l || px > margin.l + plotW || py < margin.t || py > margin.t + plotH) {
    tooltip.style.display = "none";
    return;
  }
  const bucketPos = heatState.viewX[0] + (px - margin.l) / plotW * (heatState.viewX[1] - heatState.viewX[0]);
  const strikePos = heatState.viewY[0] + (1 - (py - margin.t) / plotH) * (heatState.viewY[1] - heatState.viewY[0]);
  const bucketIdx = Math.max(0, Math.min(g.bucketCount - 1, Math.floor(bucketPos)));
  const strikeIdx = Math.max(0, Math.min(g.strikes.length - 1, Math.floor(strikePos)));
  const strike = g.strikes[strikeIdx];
  const cell = g.grid.get(bucketIdx + "|" + strike);
  if (!cell) {
    tooltip.style.display = "none";
    return;
  }
  const bucketOpen = new Date(g.bucketStartUtc.getTime() + bucketIdx * g.intervalMs);
  const bucketClose = new Date(bucketOpen.getTime() + g.intervalMs);
  tooltip.innerHTML = `<b>$${fmtStrikeLabel(strike)}</b><br>GEX&nbsp;&nbsp;${moneyM(cell.gex)}<br>` +
    `<span style="color:#94A3B8">${timeET(cell.time)} · O ${fmtHM(bucketOpen)} C ${fmtHM(bucketClose)}</span>`;
  tooltip.style.left = (px + 14) + "px";
  tooltip.style.top = (py + 10) + "px";
  tooltip.style.display = "block";
}

function onHeatMouseMove(e) {
  const canvas = heatCanvasEl();
  const rect = canvas.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  if (heatDrag) {
    const g = heatState.grid;
    if (!g) return;
    const {plotW, plotH} = heatMetrics();
    const dxDomain = (e.clientX - heatDrag.startX) / plotW * (heatDrag.viewX[1] - heatDrag.viewX[0]);
    const dyDomain = (e.clientY - heatDrag.startY) / plotH * (heatDrag.viewY[1] - heatDrag.viewY[0]);
    heatState.viewX = clampView([heatDrag.viewX[0] - dxDomain, heatDrag.viewX[1] - dxDomain], 0, g.bucketCount);
    heatState.viewY = clampView([heatDrag.viewY[0] + dyDomain, heatDrag.viewY[1] + dyDomain], 0, g.strikes.length);
    renderHeatCanvas();
    hideHeatTooltip();
    return;
  }
  showHeatTooltip(px, py);
}

function onHeatMouseUp() {
  heatDrag = null;
}

function onHeatMouseLeave() {
  heatDrag = null;
  hideHeatTooltip();
}

function initHeatTrackerControls() {
  const canvas = heatCanvasEl();
  const intervalSel = document.getElementById("heatInterval");
  const modeBtn = document.getElementById("heatModeToggle");
  const intensityInput = document.getElementById("heatIntensity");
  const resetBtn = document.getElementById("heatResetZoom");
  if (intervalSel) {
    intervalSel.value = String(heatState.interval);
    intervalSel.addEventListener("change", () => {
      heatState.interval = Number(intervalSel.value) || 1;
      try { localStorage.setItem("qqqHeatInterval", String(heatState.interval)); } catch (_err) {}
      heatState.grid = buildHeatGrid(heatState);
      heatState.viewX = [0, heatState.grid.bucketCount];
      heatState.viewY = [0, heatState.grid.strikes.length];
      renderHeatCanvas();
    });
  }
  if (modeBtn) {
    const syncModeLabel = () => {
      modeBtn.textContent = heatState.mode === "dots" ? "Dots" : "Blocks";
      modeBtn.classList.toggle("active", heatState.mode === "dots");
    };
    syncModeLabel();
    modeBtn.addEventListener("click", () => {
      heatState.mode = heatState.mode === "dots" ? "blocks" : "dots";
      try { localStorage.setItem("qqqHeatMode", heatState.mode); } catch (_err) {}
      syncModeLabel();
      renderHeatCanvas();
    });
  }
  if (intensityInput) {
    intensityInput.value = String(heatState.intensity);
    intensityInput.addEventListener("input", () => {
      heatState.intensity = Number(intensityInput.value) || 0;
      try { localStorage.setItem("qqqHeatIntensity", String(heatState.intensity)); } catch (_err) {}
      renderHeatCanvas();
    });
  }
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      const g = heatState.grid;
      if (!g) return;
      heatState.viewX = [0, g.bucketCount];
      heatState.viewY = [0, g.strikes.length];
      renderHeatCanvas();
    });
  }
  if (canvas) {
    canvas.addEventListener("wheel", onHeatWheel, {passive: false});
    canvas.addEventListener("mousedown", onHeatMouseDown);
    canvas.addEventListener("mousemove", onHeatMouseMove);
    window.addEventListener("mouseup", onHeatMouseUp);
    canvas.addEventListener("mouseleave", onHeatMouseLeave);
  }
  const wrap = document.getElementById("gexribbonWrap");
  if (wrap && window.ResizeObserver) {
    new ResizeObserver(() => renderHeatCanvas()).observe(wrap);
  }
}

function drawGexRibbon(ribbon, points, summary, session) {
  heatState.ribbon = ribbon || [];
  heatState.points = points || [];
  heatState.summary = summary || {};
  heatState.session = session || null;
  const sessionOpenUtc = session && session.market_open_utc ? new Date(session.market_open_utc) : null;
  const sessionKey = "session-" + (session?.trading_date || (sessionOpenUtc ? plotTimeNY(sessionOpenUtc).slice(0, 10) : "")) + (session?.history_snapshot_id ? "-" + session.history_snapshot_id : "");
  const grid = buildHeatGrid(heatState);
  heatState.grid = grid;
  if (sessionKey !== heatState.sessionKey) {
    heatState.sessionKey = sessionKey;
    heatState.viewX = [0, grid.bucketCount];
    heatState.viewY = [0, grid.strikes.length];
  } else {
    heatState.viewX = clampView(heatState.viewX, 0, grid.bucketCount);
    heatState.viewY = clampView(heatState.viewY, 0, grid.strikes.length);
  }
  renderHeatCanvas();
}

initHeatTrackerControls();

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
  const gexSession = state.session ? {...state.session, history_snapshot_id: state.history_snapshot_id || null} : state.session;
  drawGexRibbon(state.gex_ribbon || [], state.points || [], state.latest_summary || {}, gexSession);
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

async function loadHistoricalSnapshot(snapshotId, resetReplay = true) {
  const res = await fetch("/api/snapshot?id=" + encodeURIComponent(snapshotId) + "&ts=" + Date.now());
  const state = await res.json();
  if (state.error) throw new Error(state.error);
  const requestedId = snapshotId;
  selectedHistoryId = requestedId.startsWith("day:") ? requestedId : snapshotId;
  if (resetReplay) {
    replaySnapshots = state.replay_snapshots || [];
    replayIndex = Math.max(0, replaySnapshots.findIndex(item => item.id === snapshotId));
    if (replayIndex < 0 && replaySnapshots.length) replayIndex = replaySnapshots.length - 1;
    setReplayVisible(replaySnapshots.length > 0);
  }
  latestState = state;
  resetChartLocks();
  drawAll(state);
  updateReplayControls();
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
["flowInterval", "flowMoneyness", "flowExpiry"].forEach(id => {
  document.getElementById(id)?.addEventListener("change", () => {
    resetChartLocks();
    if (latestState) drawAll(latestState);
  });
});
document.getElementById("flowResetZoom")?.addEventListener("click", () => {
  resetChartLocks();
  if (latestState) drawAll(latestState);
});
window.addEventListener("resize", () => {
  if (latestState) drawAll(latestState);
});
loadHistoryChoices();
document.getElementById("historySelect")?.addEventListener("change", async event => {
  const value = event.target.value;
  if (value === "live") {
    selectedHistoryId = "live";
    replaySnapshots = [];
    replayIndex = 0;
    setReplayVisible(false);
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
document.getElementById("replaySlider")?.addEventListener("input", async event => {
  stopReplay();
  await loadReplayIndex(Number(event.target.value));
});
document.getElementById("replayPrev")?.addEventListener("click", async () => {
  stopReplay();
  await loadReplayIndex(replayIndex - 1);
});
document.getElementById("replayNext")?.addEventListener("click", async () => {
  stopReplay();
  await loadReplayIndex(replayIndex + 1);
});
document.getElementById("replayPlay")?.addEventListener("click", () => {
  if (replayTimer) {
    stopReplay();
    return;
  }
  const play = document.getElementById("replayPlay");
  if (play) play.textContent = "Pause";
  replayTimer = setInterval(async () => {
    if (!replaySnapshots.length || replayIndex >= replaySnapshots.length - 1) {
      stopReplay();
      return;
    }
    try {
      await loadReplayIndex(replayIndex + 1);
    } catch (err) {
      stopReplay();
      document.getElementById("status").textContent = "Replay error: " + err.message;
    }
  }, 900);
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
    parser.add_argument(
        "--collect-start-offset-min",
        type=float,
        default=COLLECT_START_OFFSET_MIN,
        help="Minutes before NY market open (9:30 ET) that Heat Tracker starts collecting. "
        "Anchored to NY time so it self-adjusts across US DST changes.",
    )
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


def history_summary_paths(ticker: str) -> list[Path]:
    return sorted(DATA_ROOT.glob(f"*/history/{ticker.upper()}_*_snapshots.parquet"))


def parse_history_json(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def summary_from_history_row(row: dict) -> dict:
    return {key: clean_value(parse_history_json(value)) for key, value in row.items() if not key.startswith("recon_")}


def history_by_strike_path(summary_history_path: Path) -> Path:
    return summary_history_path.with_name(summary_history_path.name.replace("_snapshots.parquet", "_by_strike_history.parquet"))


def history_snapshot_id(summary_history_path: Path, snapshot_utc: str) -> str:
    return "history:" + summary_history_path.relative_to(DATA_ROOT).as_posix() + "#" + quote(snapshot_utc, safe="")


def parse_history_snapshot_id(snapshot_id: str) -> tuple[Path, str]:
    payload = snapshot_id.removeprefix("history:")
    rel_path, encoded_ts = payload.rsplit("#", 1)
    candidate = (DATA_ROOT / unquote(rel_path)).resolve()
    root = DATA_ROOT.resolve()
    if root not in candidate.parents or candidate.suffix != ".parquet" or not candidate.name.endswith("_snapshots.parquet"):
        raise ValueError("invalid history snapshot id")
    if not candidate.exists():
        raise FileNotFoundError("history snapshot not found")
    return candidate, unquote(encoded_ts)


def latest_history_snapshot(ticker: str) -> tuple[Path, dict] | None:
    best: tuple[pd.Timestamp, Path, dict] | None = None
    for path in history_summary_paths(ticker):
        try:
            rows = pd.read_parquet(path)
        except Exception:
            continue
        if rows.empty:
            continue
        rows = rows[rows["ticker"].astype(str).str.upper() == ticker.upper()]
        if rows.empty:
            continue
        rows = rows.sort_values("snapshot_utc")
        row = rows.iloc[-1].to_dict()
        snapshot = pd.to_datetime(row.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot):
            continue
        if best is None or snapshot > best[0]:
            best = (snapshot, path, row)
    if best is None:
        return None
    return best[1], summary_from_history_row(best[2])


def nearest_atm_iv(by_strike: pd.DataFrame, spot: float) -> float | None:
    clean = by_strike[np.isfinite(by_strike["strike"])].copy()
    if clean.empty or "iv" not in clean:
        return None
    idx = (clean["strike"] - spot).abs().idxmin()
    iv = clean.loc[idx, "iv"]
    return float(iv) * 100 if pd.notna(iv) else None


def volume_totals(rows: pd.DataFrame | list[dict]) -> tuple[float, float]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return 0.0, 0.0
    call_volume = pd.to_numeric(frame.get("call_volume", 0), errors="coerce").fillna(0).sum()
    put_volume = pd.to_numeric(frame.get("put_volume", 0), errors="coerce").fillna(0).sum()
    return float(call_volume), float(put_volume)


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
    latest_history = latest_history_snapshot(ticker)
    if latest_history is not None:
        summary_history_path, _summary = latest_history
        summaries = pd.read_parquet(summary_history_path)
        summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
        summaries = summaries[
            summaries["_snapshot_ts"].notna()
            & (summaries["_snapshot_ts"] >= open_ts)
            & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
        ].sort_values("snapshot_utc")
        if not summaries.empty:
            points: list[dict] = []
            ribbon: list[dict] = []
            for row in summaries.to_dict(orient="records"):
                summary = summary_from_history_row(row)
                records, gex_snapshot = rows_for_history_snapshot(summary_history_path, summary, window)
                call_volume, put_volume = volume_totals(records)
                points.append({
                    "time": summary.get("snapshot_utc"),
                    "atm_iv": clean_value(nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))),
                    "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                    "spot": clean_value(float(summary["spot"])),
                    "call_volume": clean_value(call_volume),
                    "put_volume": clean_value(put_volume),
                })
                ribbon.append(gex_snapshot)
            return points, ribbon
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
        call_volume = 0.0
        put_volume = 0.0
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            call_volume, put_volume = volume_totals(chart_rows)
            ribbon.append(gex_snapshot_from_chart_rows(time_key, chart_rows))
        except Exception:
            pass
        points.append({
            "time": time_key,
            "atm_iv": clean_value(atm_iv),
            "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
            "spot": clean_value(spot),
            "call_volume": clean_value(call_volume),
            "put_volume": clean_value(put_volume),
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
    latest_history = latest_history_snapshot(ticker)
    if latest_history is not None:
        summary_history_path, _summary = latest_history
        summaries = pd.read_parquet(summary_history_path)
        summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
        summaries = summaries[
            summaries["_snapshot_ts"].notna()
            & (summaries["_snapshot_ts"] >= open_ts)
            & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
        ].sort_values("snapshot_utc")
        if not summaries.empty:
            return summary_from_history_row(summaries.iloc[0].to_dict())
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
        session = market_session_utc()
    else:
        trading_date = snapshot.tz_convert(NY_TZ).date()
        open_ny = pd.Timestamp.combine(trading_date, dt_time(9, 30)).tz_localize(NY_TZ)
        close_ny = pd.Timestamp.combine(trading_date, dt_time(16, 0)).tz_localize(NY_TZ)
        session = {
            "trading_date": trading_date.isoformat(),
            "market_open_utc": open_ny.tz_convert("UTC").isoformat(),
            "market_close_utc": close_ny.tz_convert("UTC").isoformat(),
        }
    session["collection_start_utc"] = collection_start_utc(session["market_open_utc"])
    return session


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
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    for path in history_summary_paths(ticker):
        try:
            rows = pd.read_parquet(path)
        except Exception:
            continue
        for row in rows.sort_values("snapshot_utc", ascending=False).to_dict(orient="records"):
            summary = summary_from_history_row(row)
            snapshot_utc = summary.get("snapshot_utc")
            if not snapshot_utc:
                continue
            item_id = history_snapshot_id(path, snapshot_utc)
            if item_id in seen_ids:
                continue
            key = (str(snapshot_utc), str(summary.get("expiry") or ""))
            if key in seen_keys:
                continue
            seen_ids.add(item_id)
            seen_keys.add(key)
            choices.append(
                {
                    "id": item_id,
                    "label": "Snapshot · "
                    + (
                        pd.to_datetime(snapshot_utc, errors="coerce", utc=True)
                        .tz_convert(VN_TZ)
                        .strftime("%Y-%m-%d %H:%M VN")
                        if pd.notna(pd.to_datetime(snapshot_utc, errors="coerce", utc=True))
                        else str(snapshot_utc)
                    )
                    + " · "
                    + str(summary.get("expiry") or ""),
                    "snapshot_utc": snapshot_utc,
                    "expiry": summary.get("expiry"),
                }
            )
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        item_id = snapshot_id_for_path(path)
        if item_id in seen_ids:
            continue
        key = (str(summary.get("snapshot_utc") or ""), str(summary.get("expiry") or ""))
        if key in seen_keys:
            continue
        seen_ids.add(item_id)
        seen_keys.add(key)
        choices.append(
            {
                "id": item_id,
                "label": snapshot_label(path, summary),
                "snapshot_utc": summary.get("snapshot_utc"),
                "expiry": summary.get("expiry"),
            }
        )
    return sorted(choices, key=lambda item: item.get("snapshot_utc") or "", reverse=True)[:2000]


def list_trading_days(ticker: str) -> list[dict]:
    days: dict[str, dict] = {}
    for path in history_summary_paths(ticker):
        try:
            rows = pd.read_parquet(path)
        except Exception:
            continue
        if rows.empty or "snapshot_utc" not in rows:
            continue
        rows = rows[rows["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        rows["_snapshot_ts"] = pd.to_datetime(rows["snapshot_utc"], errors="coerce", utc=True)
        rows = rows[rows["_snapshot_ts"].notna()].sort_values("snapshot_utc")
        for trading_day, group in rows.groupby(rows["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str)):
            if group.empty:
                continue
            latest = group.iloc[-1].to_dict()
            current = days.get(trading_day)
            if current is None:
                days[trading_day] = {
                    "id": "day:" + trading_day,
                    "label": trading_day,
                    "trading_date": trading_day,
                    "snapshot_count": 0,
                    "latest_snapshot_utc": latest.get("snapshot_utc"),
                    "latest_snapshot_id": history_snapshot_id(path, str(latest.get("snapshot_utc"))),
                    "expiry": latest.get("expiry"),
                    "expiries": set(),
                }
                current = days[trading_day]
            current["snapshot_count"] += int(group["snapshot_utc"].nunique())
            for expiry in group.get("expiry", pd.Series(dtype=object)).dropna().astype(str).unique():
                current["expiries"].add(expiry)
            if str(latest.get("snapshot_utc") or "") > str(current.get("latest_snapshot_utc") or ""):
                current["latest_snapshot_utc"] = latest.get("snapshot_utc")
                current["latest_snapshot_id"] = history_snapshot_id(path, str(latest.get("snapshot_utc")))
                current["expiry"] = latest.get("expiry")
    for day_dir in sorted(path for path in DATA_ROOT.iterdir() if path.is_dir()):
        if day_dir.name in days:
            continue
        candidates = []
        for path in sorted(day_dir.glob(f"{ticker.upper()}_*_summary.json"), key=lambda p: p.stat().st_mtime):
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
            if pd.isna(snapshot):
                continue
            candidates.append((snapshot, path, summary))
        if not candidates:
            continue
        snapshot, path, summary = max(candidates, key=lambda item: item[0])
        trading_day = snapshot.tz_convert(NY_TZ).date().isoformat()
        if trading_day in days:
            continue
        expiry = summary.get("expiry")
        days[trading_day] = {
            "id": "day:" + trading_day,
            "label": trading_day,
            "trading_date": trading_day,
            "snapshot_count": len(candidates),
            "latest_snapshot_utc": summary.get("snapshot_utc"),
            "latest_snapshot_id": snapshot_id_for_path(path),
            "expiry": expiry,
            "expiries": {str(expiry)} if expiry else set(),
        }
    out = []
    for day in days.values():
        expiries = sorted(day.pop("expiries"))
        expiry_label = ", ".join(expiries[:2]) + ("..." if len(expiries) > 2 else "")
        day["label"] = f"{day['trading_date']} · {day['snapshot_count']} mốc"
        if expiry_label:
            day["label"] += f" · {expiry_label}"
        out.append(day)
    return sorted(out, key=lambda item: item["trading_date"], reverse=True)


def latest_snapshot_id_for_trading_day(day_id: str, ticker: str) -> str:
    trading_day = day_id.removeprefix("day:")
    for item in list_trading_days(ticker):
        if item.get("trading_date") == trading_day and item.get("latest_snapshot_id"):
            return str(item["latest_snapshot_id"])
    raise FileNotFoundError(f"No snapshots found for trading day {trading_day}")


def rows_for_history_snapshot(
    summary_history_path: Path,
    summary: dict,
    window: float,
    source_rows: pd.DataFrame | None = None,
) -> tuple[list[dict], dict]:
    if source_rows is None:
        by_strike_path = history_by_strike_path(summary_history_path)
        if not by_strike_path.exists():
            raise FileNotFoundError(f"Missing history by-strike store: {by_strike_path}")
        rows = pd.read_parquet(by_strike_path)
    else:
        rows = source_rows
    snapshot_utc = summary.get("snapshot_utc")
    rows = rows[
        (rows["snapshot_utc"].astype(str) == str(snapshot_utc))
        & (rows["ticker"].astype(str).str.upper() == str(summary.get("ticker", "")).upper())
        & (rows["expiry"].astype(str) == str(summary.get("expiry")))
    ].copy()
    spot = float(summary["spot"])
    chart_rows = rows[(rows["strike"] >= spot - window) & (rows["strike"] <= spot + window)].copy()
    if chart_rows.empty:
        chart_rows = rows.sort_values("abs_net_gex", ascending=False).head(40)
    keep_cols = [
        "strike",
        "net_gex",
        "call_gex",
        "put_gex",
        "net_dex",
        "call_dex",
        "put_dex",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "iv",
        "call_iv",
        "put_iv",
    ]
    for col in keep_cols:
        if col not in chart_rows.columns:
            chart_rows[col] = np.nan
    chart_rows = chart_rows.sort_values("strike")[[col for col in keep_cols if col in chart_rows.columns]].copy()
    chart_rows["iv_pct"] = chart_rows["iv"] * 100
    chart_rows["call_iv_pct"] = chart_rows["call_iv"] * 100
    chart_rows["put_iv_pct"] = chart_rows["put_iv"] * 100
    records = chart_rows.replace([np.inf, -np.inf], np.nan).where(pd.notna(chart_rows), None).to_dict(orient="records")
    return clean_records(records), gex_snapshot_from_chart_rows(snapshot_utc, chart_rows)


def chart_payload_from_history(summary_history_path: Path, snapshot_utc: str, ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
    rows = pd.read_parquet(summary_history_path)
    rows = rows[
        (rows["snapshot_utc"].astype(str) == str(snapshot_utc))
        & (rows["ticker"].astype(str).str.upper() == ticker.upper())
    ]
    if rows.empty:
        raise FileNotFoundError("history snapshot row not found")
    summary = summary_from_history_row(rows.iloc[-1].to_dict())
    point = {
        "time": summary.get("snapshot_utc"),
        "atm_iv": None,
        "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
        "spot": clean_value(float(summary["spot"])),
    }
    records, gex_snapshot = rows_for_history_snapshot(summary_history_path, summary, window)
    call_volume, put_volume = volume_totals(records)
    point["atm_iv"] = nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))
    point["call_volume"] = clean_value(call_volume)
    point["put_volume"] = clean_value(put_volume)
    return summary, point, records, load_history(ticker), gex_snapshot


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
    keep_cols = [
        "strike",
        "net_gex",
        "call_gex",
        "put_gex",
        "net_dex",
        "call_dex",
        "put_dex",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "iv",
        "call_iv",
        "put_iv",
    ]
    for col in keep_cols:
        if col not in chart_rows.columns:
            chart_rows[col] = np.nan
    rows = chart_rows.sort_values("strike")[keep_cols].copy()
    rows["iv_pct"] = rows["iv"] * 100
    rows["call_iv_pct"] = rows["call_iv"] * 100
    rows["put_iv_pct"] = rows["put_iv"] * 100
    call_volume, put_volume = volume_totals(rows)
    point["call_volume"] = clean_value(call_volume)
    point["put_volume"] = clean_value(put_volume)
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
        call_volume = 0.0
        put_volume = 0.0
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            call_volume, put_volume = volume_totals(chart_rows)
            ribbon.append(gex_snapshot_from_chart_rows(time_key, chart_rows))
        except Exception:
            pass
        points.append(
            {
                "time": time_key,
                "atm_iv": clean_value(atm_iv),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(spot),
                "call_volume": clean_value(call_volume),
                "put_volume": clean_value(put_volume),
            }
        )
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def filter_replay_series(points: list[dict], ribbon: list[dict], session: dict, selected_utc: str) -> tuple[list[dict], list[dict]]:
    start_ts = pd.Timestamp(session["collection_start_utc"])
    end_ts = pd.to_datetime(selected_utc, errors="coerce", utc=True)
    if pd.isna(end_ts):
        return points, ribbon

    def in_window(value: str | None) -> bool:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
        return pd.notna(ts) and start_ts <= ts <= end_ts

    return (
        [point for point in points if in_window(point.get("time"))],
        [snap for snap in ribbon if in_window(snap.get("time"))],
    )


def replay_snapshots_from_history(summary_history_path: Path, selected_summary: dict, ticker: str) -> list[dict]:
    summaries = pd.read_parquet(summary_history_path)
    session = session_from_summary(selected_summary)
    start_ts = pd.Timestamp(session["collection_start_utc"])
    summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
    summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
    summaries = summaries[
        summaries["_snapshot_ts"].notna()
        & (summaries["_snapshot_ts"] >= start_ts)
        & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
    ].sort_values("snapshot_utc")
    out = []
    seen: set[str] = set()
    for row in summaries.to_dict(orient="records"):
        summary = summary_from_history_row(row)
        snapshot_utc = summary.get("snapshot_utc")
        if not snapshot_utc or snapshot_utc in seen:
            continue
        seen.add(snapshot_utc)
        out.append(
            {
                "id": history_snapshot_id(summary_history_path, snapshot_utc),
                "snapshot_utc": snapshot_utc,
                "label": snapshot_label(summary_history_path, summary),
            }
        )
    return out


def replay_snapshots_from_summary_path(summary_path: Path, ticker: str) -> list[dict]:
    selected_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    session = session_from_summary(selected_summary)
    start_ts = pd.Timestamp(session["collection_start_utc"])
    out = []
    seen: set[str] = set()
    for path in sorted(summary_path.parent.glob(f"{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if (
            pd.isna(snapshot)
            or snapshot < start_ts
            or snapshot.tz_convert(NY_TZ).date().isoformat() != session["trading_date"]
        ):
            continue
        snapshot_utc = summary.get("snapshot_utc")
        if not snapshot_utc or snapshot_utc in seen:
            continue
        seen.add(snapshot_utc)
        out.append(
            {
                "id": snapshot_id_for_path(path),
                "snapshot_utc": snapshot_utc,
                "label": snapshot_label(path, summary),
            }
        )
    return sorted(out, key=lambda item: item["snapshot_utc"])


def day_series_from_history(summary_history_path: Path, selected_summary: dict, ticker: str, window: float) -> tuple[list[dict], list[dict]]:
    summaries = pd.read_parquet(summary_history_path)
    selected_session = session_from_summary(selected_summary)
    start_ts = pd.Timestamp(selected_session["collection_start_utc"])
    summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
    summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
    summaries = summaries[
        summaries["_snapshot_ts"].notna()
        & (summaries["_snapshot_ts"] >= start_ts)
        & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == selected_session["trading_date"])
    ].sort_values("snapshot_utc")
    by_strike_path = history_by_strike_path(summary_history_path)
    by_strike_rows = pd.read_parquet(by_strike_path) if by_strike_path.exists() else None
    rows_by_snapshot: dict[str, pd.DataFrame] = {}
    if by_strike_rows is not None and not by_strike_rows.empty:
        by_strike_rows = by_strike_rows[
            (by_strike_rows["ticker"].astype(str).str.upper() == ticker.upper())
            & (by_strike_rows["expiry"].astype(str) == str(selected_summary.get("expiry")))
        ].copy()
        for time_key, frame in by_strike_rows.groupby(by_strike_rows["snapshot_utc"].astype(str), sort=False):
            rows_by_snapshot[str(time_key)] = frame
    points: list[dict] = []
    ribbon: list[dict] = []
    for row in summaries.to_dict(orient="records"):
        summary = summary_from_history_row(row)
        time_key = summary.get("snapshot_utc")
        if not time_key:
            continue
        records, gex_snapshot = rows_for_history_snapshot(
            summary_history_path,
            summary,
            window,
            rows_by_snapshot.get(str(time_key), by_strike_rows),
        )
        call_volume, put_volume = volume_totals(records)
        points.append(
            {
                "time": time_key,
                "atm_iv": clean_value(nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(float(summary["spot"])),
                "call_volume": clean_value(call_volume),
                "put_volume": clean_value(put_volume),
            }
        )
        ribbon.append(gex_snapshot)
    return points, ribbon


def load_snapshot_state(snapshot_id: str, ticker: str, window: float) -> dict:
    if snapshot_id.startswith("day:"):
        snapshot_id = latest_snapshot_id_for_trading_day(snapshot_id, ticker)
    if snapshot_id.startswith("history:"):
        summary_history_path, snapshot_utc = parse_history_snapshot_id(snapshot_id)
        summary, point, rows, history, gex_snapshot = chart_payload_from_history(summary_history_path, snapshot_utc, ticker, window)
        points, ribbon = day_series_from_history(summary_history_path, summary, ticker, window)
        replay_snapshots = replay_snapshots_from_history(summary_history_path, summary, ticker)
        points, ribbon = filter_replay_series(points, ribbon, session_from_summary(summary), summary["snapshot_utc"])
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
            "replay_snapshots": replay_snapshots,
        }
    summary_path = summary_path_from_id(snapshot_id)
    summary, point, rows, history, gex_snapshot = chart_payload_from_summary_path(summary_path, ticker, window)
    points, ribbon = day_series_from_summary_path(summary_path, ticker, window)
    replay_snapshots = replay_snapshots_from_summary_path(summary_path, ticker)
    points, ribbon = filter_replay_series(points, ribbon, session_from_summary(summary), summary["snapshot_utc"])
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
        "replay_snapshots": replay_snapshots,
    }


def load_latest(ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
    latest_history = latest_history_snapshot(ticker)
    if latest_history is not None:
        summary_history_path, summary = latest_history
        return chart_payload_from_history(summary_history_path, summary["snapshot_utc"], ticker, window)
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
            payload = {"snapshots": list_trading_days(self.ticker)}
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
    global COLLECT_START_OFFSET_MIN
    args = parse_args()
    COLLECT_START_OFFSET_MIN = args.collect_start_offset_min
    state = LiveState()
    state.session["collection_start_utc"] = collection_start_utc(state.session["market_open_utc"])
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
    collect_start_vn = pd.Timestamp(state.session["collection_start_utc"]).tz_convert(VN_TZ).strftime("%H:%M")
    print(
        f"Volatility Flow + Heat Tracker start at {collect_start_vn} Vietnam time "
        f"({args.collect_start_offset_min:g} min before NY open).",
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
