#!/usr/bin/env python3
"""Local realtime dashboard server for intraday Volatility Flow.

The browser does not reload. A background worker collects snapshots on a
cadence, while the page polls `/api/state` and updates Plotly charts in place.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
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
import requests
from dotenv import load_dotenv

from exposure import nearest_atm_iv
from render_gex_interactive import build_tenor_curves, load_multi_tenor_skew
from sources import yahoo

load_dotenv()

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


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
    """Current NY session, or the latest weekday session when NY is closed.

    Snapshots are stored in folders named by the machine's local date, which can
    differ from the US session date. The dashboard must therefore anchor its
    session boundary to America/New_York rather than to the storage folder.
    """
    now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
    now_ny = now_utc.tz_convert(NY_TZ)
    trading_date = now_ny.date()
    while trading_date.weekday() >= 5:
        trading_date -= timedelta(days=1)
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
  .panel-date-picker {
    height: 28px; border: 1px solid #263244; border-radius: 6px;
    background: #0F172A; color: #E5E7EB; padding: 3px 8px; font-size: 12px;
    color-scheme: dark; font-family: inherit; margin-left: auto;
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
  .exposure-tabs {
    margin-left: auto; display: flex; gap: 3px; background: #0F172A;
    border: 1px solid #263244; border-radius: 8px; padding: 3px;
  }
  .exposure-tab {
    height: 24px; padding: 0 10px; border: none; border-radius: 6px;
    background: transparent; color: #94A3B8; font-size: 11px; font-weight: 700;
    letter-spacing: .03em; cursor: pointer;
  }
  .exposure-tab.active { background: rgba(34,211,238,0.15); color: var(--accent-call); }
  .flow-wrap { height: 430px; position: relative; padding: 0; }
  .flow-controls {
    margin-left: auto; display: flex; align-items: center; gap: 8px;
    color: #94A3B8; font-size: 11px; font-weight: 600;
  }
  .flow-controls select, .flow-controls button {
    height: 24px; border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 0 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .skew-controls {
    margin-left: auto; display: flex; align-items: center; gap: 8px;
    color: #94A3B8; font-size: 11px; font-weight: 600;
  }
  .skew-controls select, .skew-series button {
    height: 24px; border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 0 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .skew-readout {
    height: 24px; display: inline-flex; align-items: center; border: 1px solid #263244;
    border-radius: 6px; background: #0F172A; color: #CBD5E1; padding: 0 8px;
    font: 600 11px Menlo, Consolas, monospace; white-space: nowrap;
  }
  .skew-series { position: relative; }
  .skew-series-menu {
    position: absolute; top: 28px; right: 0; z-index: 20; min-width: 110px;
    background: #0F172A; border: 1px solid #263244; border-radius: 8px;
    padding: 6px; display: flex; flex-direction: column; gap: 4px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.45);
  }
  .skew-series-menu label {
    display: flex; align-items: center; gap: 6px; padding: 4px 6px;
    border-radius: 5px; color: #E5E7EB; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .skew-series-menu label:hover { background: rgba(148,163,184,0.12); }
  .skew-series-menu input[type="checkbox"] { accent-color: var(--accent-call); cursor: pointer; }
  .skew-panel .panel-header { min-height: 40px; }
  .skew-panel .body { padding: 12px 16px 16px; }
  .skew-wrap { height: 340px; position: relative; }
  .skew-mini-legend {
    position: absolute; top: 44px; right: 78px; z-index: 6; pointer-events: none;
    display: grid; grid-template-columns: 24px auto; column-gap: 8px; row-gap: 5px;
    align-items: center; color: #7C8798; font: 12px/1 Menlo, Consolas, monospace;
    letter-spacing: 0;
  }
  .skew-mini-legend .sample {
    width: 24px; height: 0; border-top: 2px solid #CBD5E1; opacity: 0.95;
  }
  .skew-mini-legend .sample.puts { border-top-style: dashed; }
  .skew-mini-legend .sample.iv { border-top-style: dotted; border-top-width: 3px; }
  .skew-tooltip {
    position: absolute; z-index: 12; display: none; pointer-events: none;
    min-width: 168px; padding: 9px 11px; border: 1px solid #334155;
    border-radius: 7px; background: rgba(5, 7, 11, 0.96); color: #E5E7EB;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
    font: 12px/1.45 Menlo, Consolas, monospace; white-space: nowrap;
  }
  .skew-tooltip .strike { color: #CBD5E1; font-weight: 700; margin-bottom: 3px; }
  .skew-tooltip .muted { color: #94A3B8; }
  .levels-controls {
    margin-left: auto; display: flex; align-items: center; gap: 8px;
    color: #94A3B8; font-size: 11px; font-weight: 600;
  }
  .levels-controls button {
    height: 24px; border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 0 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .levels-controls button.copied { color: var(--accent-call); border-color: rgba(34,211,238,0.45); }
  .levels-tag {
    font-size: 10px; font-weight: 700; letter-spacing: 0.04em; padding: 2px 6px;
    border-radius: 5px; border: 1px solid #263244; color: #94A3B8;
  }
  .levels-tag.locked { color: var(--accent-call); border-color: rgba(34,211,238,0.45); }
  .levels-body { display: flex; flex-direction: column; gap: 10px; }
  .levels-row-header {
    display: flex; align-items: center; gap: 8px;
    color: #94A3B8; font-size: 11px; font-weight: 600;
  }
  .levels-row-header button {
    height: 24px; border: 1px solid #263244; border-radius: 6px; background: #0F172A;
    color: #E5E7EB; padding: 0 8px; font-size: 11px; font-weight: 600; cursor: pointer;
  }
  .levels-row-header button.copied { color: var(--accent-call); border-color: rgba(34,211,238,0.45); }
  .skew-series-menu[hidden] { display: none; }
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
    .skew-panel .panel-header { align-items: flex-start; }
    .skew-controls { flex-wrap: wrap; justify-content: flex-end; }
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
        <button id="flowSpotToggle" type="button">Spot Candles</button>
        <button id="flowResetZoom" type="button">Reset zoom</button>
      </div>
    </div>
    <div class="body flow-wrap" id="flowWrap">
      <canvas id="flowCanvas" style="width:100%; height:100%; display:block;"></canvas>
      <div id="flowTooltip" class="heat-tooltip" style="display:none;"></div>
    </div>
  </div>
  <div class="panel wide">
    <div class="panel-header">
      <span class="dot"></span>Flow Tracker
      <div class="flow-controls">
        <select id="trackerMode">
          <option value="CALL_PUT" selected>Call / Put</option>
          <option value="PREMIUM">Premium $ (beta)</option>
        </select>
        <select id="trackerInterval">
          <option value="1" selected>1m</option>
          <option value="5">5m</option>
          <option value="15">15m</option>
          <option value="30">30m</option>
        </select>
        <select id="trackerMoneyness">
          <option value="ALL">ALL</option>
          <option value="ITM">ITM</option>
          <option value="ATM" selected>ATM</option>
          <option value="OTM">OTM</option>
        </select>
        <select id="trackerExpiry">
          <option value="ALL">ALL</option>
          <option value="0DTE" selected>0DTE</option>
        </select>
        <span id="trackerTicker" class="skew-readout">QQQ</span>
        <button id="trackerResetZoom" type="button">reset zoom</button>
      </div>
    </div>
    <div class="body flow-wrap" id="trackerWrap">
      <canvas id="trackerCanvas" style="width:100%; height:100%; display:block;"></canvas>
      <div id="trackerTooltip" class="heat-tooltip" style="display:none;"></div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot"></span>IV Rank<input type="date" class="panel-date-picker" id="ivRankDate"></div>
    <div class="body"><div id="ivrank" style="height:340px;"></div></div>
  </div>
  <div class="panel skew-panel">
    <div class="panel-header"><span class="dot" style="background:#FACC15"></span>Volatility Skew
      <div class="skew-controls">
        <span id="skewDate" class="skew-readout">--</span>
        <select id="skewExpiry"></select>
        <div class="skew-series" id="skewSeries">
          <button type="button" id="skewSeriesBtn">Calls · Puts · IV</button>
          <div class="skew-series-menu" id="skewSeriesMenu" hidden>
            <label><input type="checkbox" data-series="call" checked> Calls</label>
            <label><input type="checkbox" data-series="put" checked> Puts</label>
            <label><input type="checkbox" data-series="iv" checked> IV</label>
          </div>
        </div>
        <span id="skewTicker" class="skew-readout">QQQ</span>
      </div>
    </div>
    <div class="body skew-wrap">
      <div id="skew" style="height:100%;"></div>
      <div class="skew-mini-legend" aria-hidden="true">
        <span class="sample calls"></span><span>calls</span>
        <span class="sample puts"></span><span>puts</span>
        <span class="sample iv"></span><span>iv</span>
      </div>
      <div id="skewTooltip" class="skew-tooltip"></div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot"></span>OI × IV by Strike<input type="date" class="panel-date-picker" id="oiIvDate"></div>
    <div class="body"><div id="oiiv" style="height:360px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header"><span class="dot put-color"></span>OI by Strike<input type="date" class="panel-date-picker" id="oiDate"></div>
    <div class="body"><div id="oi" style="height:360px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header">
      <span class="dot"></span>Exposure
      <div class="exposure-tabs" data-panel="gex">
        <button type="button" class="exposure-tab" data-metric="net_gex">GEX</button>
        <button type="button" class="exposure-tab" data-metric="net_dex">DEX</button>
        <button type="button" class="exposure-tab" data-metric="net_vex">VEX</button>
        <button type="button" class="exposure-tab" data-metric="net_chex">CHEX</button>
      </div>
      <input type="date" class="panel-date-picker" id="exposureGexDate">
    </div>
    <div class="body"><div id="gex" style="height:520px;"></div></div>
  </div>
  <div class="panel">
    <div class="panel-header">
      <span class="dot put-color"></span>Exposure
      <div class="exposure-tabs" data-panel="dex">
        <button type="button" class="exposure-tab" data-metric="net_gex">GEX</button>
        <button type="button" class="exposure-tab" data-metric="net_dex">DEX</button>
        <button type="button" class="exposure-tab" data-metric="net_vex">VEX</button>
        <button type="button" class="exposure-tab" data-metric="net_chex">CHEX</button>
      </div>
      <input type="date" class="panel-date-picker" id="exposureDexDate">
    </div>
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
        <button id="heatSpotToggle" type="button">Spot Line</button>
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
    <div class="panel-header">
      <span class="dot" style="background:#FACC15"></span>Levels Export
      <input type="date" class="panel-date-picker" id="levelsDate">
    </div>
    <div class="body levels-body">
      <div class="levels-row">
        <div class="levels-row-header">
          <span class="levels-tag" id="levelsTag">LIVE</span>
          <button type="button" id="levelsCopyBtn">Copy</button>
        </div>
        <pre class="export-line" id="levelsExport">$QQQ: loading...</pre>
      </div>
      <div class="levels-row">
        <div class="levels-row-header">
          <span class="levels-tag" id="levelsTagB">LIVE</span>
          <button type="button" id="levelsCopyBtnB">Copy</button>
        </div>
        <pre class="export-line" id="levelsExportB">$NDX: loading...</pre>
      </div>
    </div>
  </div>
</div>
<script>
const COLORS = {
  bg: "#05070B", panel: "#000000", grid: "#1F2937", text: "#E5E7EB",
  muted: "#94A3B8", cyan: "#22D3EE", yellow: "#FACC15", spot: "#CBD5E1",
  orange: "#F59E0B", green: "#4ADE80", red: "#F87171"
};
const DEFAULT_ACCENTS = {cyan: COLORS.cyan, orange: COLORS.orange};
let latestState = null;
let lastChartKey = "";
let flowHasInitialized = false;
let skewExpirySelected = "nearest";
let skewSeriesVisible = {call: true, put: true, iv: true};
let skewExpiryOptionsKey = "";

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
  const basis = Number.isFinite(Number(summary?.futures_basis)) ? Number(summary.futures_basis) : 0;
  const adj = v => (v === null || v === undefined || Number.isNaN(Number(v))) ? v : Number(v) + basis;
  const label = summary?.futures_ticker ? String(summary.futures_ticker) : `$${ticker}`;
  const top = Array.isArray(summary?.top_abs_gex_levels) ? summary.top_abs_gex_levels : [];
  const gex = top.slice(0, 10).map(item => adj(item?.strike));
  while (gex.length < 10) gex.push(null);
  const parts = [
    `${label}: Call Resistance`, fmtLevel(adj(summary?.call_resistance)),
    "Put Support", fmtLevel(adj(summary?.put_support)),
    "HVL", fmtLevel(adj(summary?.spot), 2),
    "1D Min", fmtLevel(adj(summary?.one_day_min)),
    "1D Max", fmtLevel(adj(summary?.one_day_max)),
    "Call Resistance 0DTE", fmtLevel(adj(summary?.call_resistance)),
    "Put Support 0DTE", fmtLevel(adj(summary?.put_support)),
    "HVL 0DTE", fmtLevel(adj(summary?.gamma_flip)),
    "Gamma Wall 0DTE", fmtLevel(adj(summary?.gamma_wall_abs)),
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

function nyMinutes(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23"
  }).formatToParts(d).map(part => [part.type, part.value]));
  return Number(parts.hour) * 60 + Number(parts.minute);
}

function flowIvStatus(value) {
  const mins = nyMinutes(value);
  if (!Number.isFinite(mins)) return "";
  if (mins >= 9 * 60 + 25 && mins < 9 * 60 + 30) return "pre-open ref";
  if (mins >= 9 * 60 + 30 && mins < 9 * 60 + 35) return "warming";
  if (mins >= 9 * 60 + 35 && mins < 9 * 60 + 40) return "intraday";
  if (mins >= 9 * 60 + 40) return "reliable";
  return "pre-open ref";
}

function flowArvAllowed(value) {
  const mins = nyMinutes(value);
  return Number.isFinite(mins) && mins >= 9 * 60 + 40;
}

function moneynessAggregate(point, moneyness) {
  const spot = Number(point.spot);
  const chain = point.chain || [];
  if (!chain.length || !Number.isFinite(spot)) {
    return {iv: Number(point.atm_iv), call: Number(point.call_volume || 0), put: Number(point.put_volume || 0), callMid: null, putMid: null};
  }
  const strikes = [...new Set(chain.map(r => r.k))].sort((a, b) => a - b);
  let step = 1;
  if (strikes.length > 1) {
    let sum = 0;
    for (let i = 1; i < strikes.length; i++) sum += Math.abs(strikes[i] - strikes[i - 1]);
    step = sum / (strikes.length - 1) || 1;
  }
  // Strikes within one strike-step of spot count as the ATM band; beyond
  // that, ITM/OTM is judged per option leg since a strike is simultaneously
  // ITM for one side and OTM for the other.
  const ivSamples = [];
  let call = 0, put = 0;
  let callNotional = 0, callVolW = 0, putNotional = 0, putVolW = 0;
  const callMidSamples = [], putMidSamples = [];
  chain.forEach(r => {
    const dist = r.k - spot;
    const isAtm = Math.abs(dist) <= step;
    const callIn = moneyness === "ALL" || (moneyness === "ATM" && isAtm) ||
      (moneyness === "ITM" && !isAtm && dist < 0) || (moneyness === "OTM" && !isAtm && dist > 0);
    const putIn = moneyness === "ALL" || (moneyness === "ATM" && isAtm) ||
      (moneyness === "ITM" && !isAtm && dist > 0) || (moneyness === "OTM" && !isAtm && dist < 0);
    if (callIn) {
      call += r.cv || 0;
      if (Number.isFinite(r.ci)) ivSamples.push(Number(r.ci));
      if (Number.isFinite(r.cm)) {
        callMidSamples.push(Number(r.cm));
        if (r.cv > 0) { callNotional += r.cv * r.cm; callVolW += r.cv; }
      }
    }
    if (putIn) {
      put += r.pv || 0;
      if (Number.isFinite(r.pi)) ivSamples.push(Number(r.pi));
      if (Number.isFinite(r.pm)) {
        putMidSamples.push(Number(r.pm));
        if (r.pv > 0) { putNotional += r.pv * r.pm; putVolW += r.pv; }
      }
    }
  });
  const robustAtm = Number(point.atm_iv);
  const iv = moneyness === "ATM" && Number.isFinite(robustAtm)
    ? robustAtm
    : medianFinite(ivSamples);
  const callMid = callVolW > 0 ? callNotional / callVolW : medianFinite(callMidSamples);
  const putMid = putVolW > 0 ? putNotional / putVolW : medianFinite(putMidSamples);
  return {iv, call, put, callMid, putMid};
}

function medianFinite(values) {
  const clean = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!clean.length) return null;
  const mid = Math.floor(clean.length / 2);
  return clean.length % 2 ? clean[mid] : (clean[mid - 1] + clean[mid]) / 2;
}

function cleanFlowIvRows(rows) {
  const raw = rows.map(r => Number.isFinite(r.iv) ? Number(r.iv) : null);
  const cleaned = raw.slice();
  for (let i = 0; i < raw.length; i++) {
    if (!Number.isFinite(raw[i])) continue;
    const start = Math.max(0, i - 4);
    const window = raw.slice(start, i + 1).filter(Number.isFinite);
    if (window.length < 4) continue;
    const med = medianFinite(window);
    const threshold = Math.max(4.0, Math.abs(med) * 0.28);
    if (Number.isFinite(med) && Math.abs(raw[i] - med) > threshold) {
      cleaned[i] = med;
    }
  }
  rows.forEach((row, i) => {
    row.iv = cleaned[i];
  });
}

function flowMetrics() {
  const canvas = document.getElementById("flowCanvas");
  const wrap = document.getElementById("flowWrap");
  const rect = wrap ? wrap.getBoundingClientRect() : {width: 900, height: 430};
  const cssW = Math.max(1, rect.width), cssH = Math.max(1, rect.height);
  const margin = {l: 64, r: 62, t: 22, b: 28};
  const gap = 6;
  const plotW = cssW - margin.l - margin.r;
  const plotH = cssH - margin.t - margin.b;
  const topH = Math.floor(plotH * 0.65);
  const bottomH = plotH - topH - gap;
  const top = {x: margin.l, y: margin.t, w: plotW, h: topH};
  const bot = {x: margin.l, y: margin.t + topH + gap, w: plotW, h: bottomH};
  return {canvas, margin, plotW, plotH, top, bot, cssW, cssH};
}

function loadFlowPrefs() {
  let spotMode = "candles";
  try {
    const storedSpotMode = localStorage.getItem("qqqFlowSpotMode");
    if (storedSpotMode === "line" || storedSpotMode === "candles") spotMode = storedSpotMode;
  } catch (_err) {}
  return {spotMode};
}

const flowPrefs = loadFlowPrefs();
const flowState = {
  points: [], candles: [], session: null, sessionKey: "",
  viewX: [0, 1], bucketCount: 1, spotMode: flowPrefs.spotMode
};
let flowDrag = null;

function drawFlow(points, session, candles = []) {
  const flowStartUtc = session && session.market_open_utc
    ? new Date(session.market_open_utc)
    : (session && session.collection_start_utc ? new Date(session.collection_start_utc) : null);
  if (flowStartUtc && !Number.isNaN(flowStartUtc.getTime())) {
    points = (points || []).filter(p => p.time && new Date(p.time).getTime() >= flowStartUtc.getTime());
  }
  const moneyness = document.getElementById("flowMoneyness")?.value || "ATM";
  const expiryFilter = document.getElementById("flowExpiry")?.value || "0DTE";
  points = expiryFilter === "0DTE" ? points.filter(p => p.expiry !== "ALL") : points;
  const {canvas, margin, top, bot, cssW, cssH} = flowMetrics();
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cssW, cssH);
  if (!points.length && !(candles || []).length) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for data...", 16, 24);
    return;
  }

  const intervalMin = Number(document.getElementById("flowInterval")?.value || 1);
  const intervalMs = intervalMin * 60 * 1000;
  const startUtc = flowStartUtc || (points[0]?.time ? new Date(points[0].time) : new Date());
  // Cap the bucket range to the actual data extent (like buildHeatGrid does for
  // the Heat Tracker) instead of always stretching to market close: otherwise
  // every bucket after "now" has no backing data and would render as a flat
  // zero/placeholder line instead of simply not being drawn.
  const candlePoints = (candles || [])
    .map(c => ({
      time: c.t,
      open: Number(c.o),
      high: Number(c.h),
      low: Number(c.l),
      close: Number(c.c)
    }))
    .filter(c => {
      const ts = new Date(c.time).getTime();
      return !Number.isNaN(ts)
        && [c.open, c.high, c.low, c.close].every(Number.isFinite);
    });
  const dataTimes = [
    ...points.map(p => new Date(p.time)),
    ...candlePoints.map(c => new Date(c.time))
  ].filter(d => !Number.isNaN(d.getTime()));
  const lastDataUtc = dataTimes.length ? new Date(Math.max(...dataTimes.map(d => d.getTime()))) : startUtc;
  const closeUtc = session?.market_close_utc && new Date(session.market_close_utc).getTime() < lastDataUtc.getTime()
    ? new Date(session.market_close_utc)
    : lastDataUtc;
  const bucketCount = Math.max(1, Math.ceil((closeUtc.getTime() - startUtc.getTime()) / intervalMs));
  const buckets = Array.from({length: bucketCount}, (_, i) => new Date(startUtc.getTime() + i * intervalMs));
  const rows = buckets.map((bucket, i) => ({bucket, i, iv: null, arv: null, spot: null, spotOpen: null, spotHigh: null, spotLow: null, call: 0, put: 0, net: null, hasData: false}));
  const fallbackArv = computeArvPct(points);
  points.forEach((p, i) => {
    const t = new Date(p.time).getTime();
    const idx = Math.floor((t - startUtc.getTime()) / intervalMs);
    if (idx < 0 || idx >= rows.length) return;
    const row = rows[idx];
    const agg = moneynessAggregate(p, moneyness);
    const spot = Number(p.spot);
    if (Number.isFinite(agg.iv)) row.iv = agg.iv;
    if (flowArvAllowed(row.bucket) && Number.isFinite(Number(fallbackArv[i]))) row.arv = Number(fallbackArv[i]);
    if (Number.isFinite(spot)) {
      row.spot = spot;
      if (row.spotOpen == null) row.spotOpen = spot;
      row.spotHigh = row.spotHigh == null ? spot : Math.max(row.spotHigh, spot);
      row.spotLow = row.spotLow == null ? spot : Math.min(row.spotLow, spot);
    }
    row.call += Number(agg.call || 0);
    row.put += Number(agg.put || 0);
    row.hasData = true;
  });
  candlePoints.forEach(c => {
    const t = new Date(c.time).getTime();
    const idx = Math.floor((t - startUtc.getTime()) / intervalMs);
    if (idx < 0 || idx >= rows.length) return;
    const row = rows[idx];
    row.spotOpen = c.open;
    row.spotHigh = c.high;
    row.spotLow = c.low;
    row.spot = c.close;
  });
  if (candlePoints.length) {
    const candleArv = computeArvPct(rows.map(r => ({time: r.bucket, spot: r.spot})));
    rows.forEach((row, i) => {
      if (flowArvAllowed(row.bucket) && Number.isFinite(Number(candleArv[i]))) row.arv = Number(candleArv[i]);
    });
  }
  cleanFlowIvRows(rows);
  let cum = 0;
  rows.forEach(row => {
    if (!row.hasData) return;
    cum += row.call - row.put;
    row.net = cum / 1000;
  });
  const validIv = rows.flatMap(r => [r.iv, r.arv]).filter(Number.isFinite);
  const validSpotLow = rows.map(r => r.spotLow).filter(Number.isFinite);
  const validSpotHigh = rows.map(r => r.spotHigh).filter(Number.isFinite);
  const validNet = rows.map(r => r.net).filter(Number.isFinite);
  const ivMax = Math.max(20, ...validIv) * 1.15;
  const spotMin = Math.min(...validSpotLow);
  const spotMax = Math.max(...validSpotHigh);
  const spotPad = Math.max(0.5, (spotMax - spotMin) * 0.18);
  const netAbs = Math.max(5, ...validNet.map(v => Math.abs(v))) * 1.15;
  const refSpot = rows.find(r => Number.isFinite(r.spot))?.spot ?? null;
  flowState.points = points;
  flowState.candles = candles || [];
  flowState.session = session;
  flowState.bucketCount = rows.length;
  const flowSessionKey = "session-" + (session?.trading_date || (startUtc ? plotTimeNY(startUtc).slice(0, 10) : "")) + (session?.history_snapshot_id ? "-" + session.history_snapshot_id : "") + "-" + intervalMin;
  if (flowSessionKey !== flowState.sessionKey) {
    flowState.sessionKey = flowSessionKey;
    flowState.viewX = [0, rows.length];
  } else {
    flowState.viewX = clampView(flowState.viewX, 0, rows.length);
  }
  const domainSpan = flowState.viewX[1] - flowState.viewX[0];
  const x = i => top.x + (i + 0.5 - flowState.viewX[0]) / domainSpan * top.w;
  const yPct = v => top.y + top.h - (v / ivMax) * top.h;
  const ySpot = v => top.y + top.h - ((v - (spotMin - spotPad)) / ((spotMax + spotPad) - (spotMin - spotPad))) * top.h;
  const yNet = v => bot.y + bot.h / 2 - (v / netAbs) * (bot.h / 2);

  ctx.strokeStyle = "rgba(148,163,184,0.18)";
  ctx.setLineDash([5, 5]);
  ctx.beginPath(); ctx.moveTo(top.x, yPct(20)); ctx.lineTo(top.x + top.w, yPct(20)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = "rgba(148,163,184,0.28)";
  ctx.beginPath(); ctx.moveTo(bot.x, yNet(0)); ctx.lineTo(bot.x + bot.w, yNet(0)); ctx.stroke();
  if (Number.isFinite(refSpot)) {
    const py = ySpot(refSpot);
    ctx.strokeStyle = "rgba(203,213,225,0.22)";
    ctx.setLineDash([5, 5]);
    ctx.beginPath(); ctx.moveTo(top.x, py); ctx.lineTo(top.x + top.w, py); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = COLORS.muted;
  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.textAlign = "right";
  ctx.fillText(Math.round(ivMax) + "%", top.x - 8, top.y + 12);
  if (Number.isFinite(refSpot)) ctx.fillText(refSpot.toFixed(2), top.x + top.w + margin.r - 8, ySpot(refSpot) + 4);
  ctx.fillText(Math.round(netAbs) + "K", bot.x - 8, bot.y + 12);
  ctx.fillText("-" + Math.round(netAbs) + "K", bot.x - 8, bot.y + bot.h - 2);
  if (Number.isFinite(refSpot)) ctx.fillText(refSpot.toFixed(2), bot.x + bot.w + margin.r - 8, bot.y + 12);
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
  function spotCandles() {
    const bodyW = Math.min(7, Math.max(2.4, (top.w / domainSpan) * 0.42));
    rows.forEach((r, i) => {
      if (!Number.isFinite(r.spotOpen) || !Number.isFinite(r.spot)) return;
      const px = x(i);
      const yOpen = ySpot(r.spotOpen);
      const yClose = ySpot(r.spot);
      const yHigh = ySpot(r.spotHigh);
      const yLow = ySpot(r.spotLow);
      const color = r.spot >= r.spotOpen ? COLORS.cyan : COLORS.orange;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(px, yHigh);
      ctx.lineTo(px, yLow);
      ctx.stroke();
      const bodyTop = Math.min(yOpen, yClose);
      const bodyH = Math.max(1.2, Math.abs(yClose - yOpen));
      ctx.fillStyle = "#000";
      ctx.fillRect(px - bodyW / 2, bodyTop, bodyW, bodyH);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.strokeRect(px - bodyW / 2, bodyTop, bodyW, bodyH);
    });
  }
  function spotLine() {
    ctx.strokeStyle = "#F8FAFC";
    ctx.lineWidth = 1.45;
    ctx.beginPath();
    let started = false;
    rows.forEach((r, i) => {
      if (!Number.isFinite(r.spot)) return;
      const px = x(i), py = ySpot(r.spot);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(top.x, top.y, top.w, top.h);
  ctx.clip();
  line("iv", yPct, COLORS.cyan, 1.7);
  line("arv", yPct, COLORS.orange, 1.5);
  if (flowState.spotMode === "line") spotLine();
  else spotCandles();
  ctx.restore();

  ctx.save();
  ctx.beginPath();
  ctx.rect(bot.x, bot.y, bot.w, bot.h);
  ctx.clip();
  const zeroY = yNet(0);
  for (let i = 0; i < rows.length - 1; i++) {
    const r0 = rows[i], r1 = rows[i + 1];
    if (!Number.isFinite(r0.net) || !Number.isFinite(r1.net)) continue;
    const x0 = x(i), x1 = x(i + 1);
    const y0 = yNet(r0.net), y1 = yNet(r1.net);
    const fillSegment = (px0, py0, px1, py1, color) => {
      ctx.beginPath();
      ctx.moveTo(px0, zeroY);
      ctx.lineTo(px0, py0);
      ctx.lineTo(px1, py1);
      ctx.lineTo(px1, zeroY);
      ctx.closePath();
      ctx.fillStyle = rgbaFromHex(color, 0.36);
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(px0, py0);
      ctx.lineTo(px1, py1);
      ctx.stroke();
    };
    if ((r0.net >= 0) === (r1.net >= 0)) {
      fillSegment(x0, y0, x1, y1, r0.net >= 0 ? COLORS.cyan : COLORS.orange);
    } else {
      const t = r0.net / (r0.net - r1.net);
      const xm = x0 + t * (x1 - x0);
      fillSegment(x0, y0, xm, zeroY, r0.net >= 0 ? COLORS.cyan : COLORS.orange);
      fillSegment(xm, zeroY, x1, y1, r1.net >= 0 ? COLORS.cyan : COLORS.orange);
    }
  }
  ctx.restore();

  const latest = [...rows].reverse().find(r => Number.isFinite(r.iv) || Number.isFinite(r.arv) || Number.isFinite(r.spot));
  ctx.textAlign = "left";
  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.fillStyle = COLORS.cyan;
  ctx.fillText("IV " + (latest?.iv == null ? "NA" : latest.iv.toFixed(1) + "%"), top.x + 2, top.y + 12);
  ctx.fillStyle = COLORS.orange;
  ctx.fillText(" · ARV " + (latest?.arv == null ? "NA" : latest.arv.toFixed(1) + "%"), top.x + 72, top.y + 12);
  const status = latest ? flowIvStatus(latest.bucket) : "";
  if (status) {
    ctx.fillStyle = COLORS.muted;
    ctx.fillText(" · " + status, top.x + 150, top.y + 12);
  }
  if (latest && Number.isFinite(latest.spot)) {
    const py = ySpot(latest.spot);
    const labelX = top.x + top.w + 4;
    const labelW = Math.max(30, margin.r - 8);
    ctx.fillStyle = "#F8FAFC";
    ctx.fillRect(labelX, py - 9, labelW, 18);
    ctx.fillStyle = "#020617";
    ctx.textAlign = "center";
    ctx.fillText(latest.spot.toFixed(2), labelX + labelW / 2, py + 4);
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
    if (flowDrag) {
      const dxDomain = (ev.clientX - flowDrag.startX) / top.w * (flowDrag.viewX[1] - flowDrag.viewX[0]);
      flowState.viewX = clampView([flowDrag.viewX[0] - dxDomain, flowDrag.viewX[1] - dxDomain], 0, flowState.bucketCount);
      drawFlow(points, session, candles);
      const tip = document.getElementById("flowTooltip");
      if (tip) tip.style.display = "none";
      return;
    }
    const idx = Math.max(0, Math.min(rows.length - 1, Math.floor(flowState.viewX[0] + (mx - top.x) / top.w * domainSpan)));
    const r = rows[idx];
    drawFlow(points, session, candles);
    const c = canvas.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.strokeStyle = "rgba(248,250,252,0.45)";
    c.beginPath(); c.moveTo(x(idx), top.y); c.lineTo(x(idx), bot.y + bot.h); c.stroke();
    const tip = document.getElementById("flowTooltip");
    if (tip) {
      tip.style.display = "block";
      tip.style.left = Math.min(cssW - 250, mx + 14) + "px";
      tip.style.top = Math.max(4, ev.clientY - rect.top + 12) + "px";
      tip.innerHTML = `IV ${Number.isFinite(r.iv) ? r.iv.toFixed(1) + "%" : "NA"} (${flowIvStatus(r.bucket) || "NA"}) &nbsp; ARV ${Number.isFinite(r.arv) ? r.arv.toFixed(1) + "%" : "NA"} &nbsp; Call - Put ${Number.isFinite(r.net) ? r.net.toFixed(1) + "K" : "NA"}<br>` +
        `TRADED IN BUCKET: Call contracts ${(r.call / 1000).toFixed(1)}K · Put contracts ${(r.put / 1000).toFixed(1)}K · price ${Number.isFinite(r.spot) ? r.spot.toFixed(2) : "NA"}`;
    }
  };
  canvas.onmouseleave = () => {
    flowDrag = null;
    const tip = document.getElementById("flowTooltip");
    if (tip) tip.style.display = "none";
    drawFlow(points, session, candles);
  };
}

function onFlowWheel(e) {
  if (flowState.bucketCount <= 1) return;
  e.preventDefault();
  const {canvas, top} = flowMetrics();
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const factor = Math.exp((e.deltaY > 0 ? 1 : -1) * 0.15);
  flowState.viewX = zoomAxis(flowState.viewX, px, top.x, top.w, factor, 0, flowState.bucketCount, false);
  drawFlow(flowState.points, flowState.session, flowState.candles);
}

function onFlowMouseDown(e) {
  if (!flowState.bucketCount) return;
  flowDrag = {startX: e.clientX, viewX: [...flowState.viewX]};
}

function onFlowMouseUp() {
  flowDrag = null;
}

function initFlowControls() {
  const canvas = document.getElementById("flowCanvas");
  const spotBtn = document.getElementById("flowSpotToggle");
  if (spotBtn) {
    const syncSpotLabel = () => {
      spotBtn.textContent = flowState.spotMode === "line" ? "Spot Line" : "Spot Candles";
      spotBtn.classList.toggle("active", flowState.spotMode === "line");
    };
    syncSpotLabel();
    spotBtn.addEventListener("click", () => {
      flowState.spotMode = flowState.spotMode === "line" ? "candles" : "line";
      try { localStorage.setItem("qqqFlowSpotMode", flowState.spotMode); } catch (_err) {}
      syncSpotLabel();
      if (latestState) drawFlow(latestState.points || [], latestState.session || null, latestState.candles || []);
    });
  }
  if (canvas) {
    canvas.addEventListener("wheel", onFlowWheel, {passive: false});
    canvas.addEventListener("mousedown", onFlowMouseDown);
    window.addEventListener("mouseup", onFlowMouseUp);
  }
}
initFlowControls();

function trackerMetrics() {
  const canvas = document.getElementById("trackerCanvas");
  const wrap = document.getElementById("trackerWrap");
  const rect = wrap ? wrap.getBoundingClientRect() : {width: 900, height: 430};
  const cssW = Math.max(1, rect.width), cssH = Math.max(1, rect.height);
  const margin = {l: 68, r: 40, t: 36, b: 28};
  const plot = {
    x: margin.l,
    y: margin.t,
    w: cssW - margin.l - margin.r,
    h: cssH - margin.t - margin.b
  };
  return {canvas, margin, plot, cssW, cssH};
}

const trackerState = {
  points: [], session: null, sessionKey: "",
  viewX: [0, 1], bucketCount: 1
};
let trackerDrag = null;

function formatTrackerK(v) {
  if (!Number.isFinite(v)) return "NA";
  const sign = v < 0 ? "-" : "";
  return sign + Math.round(Math.abs(v)) + "K";
}

function buildTrackerRows(points, session) {
  const flowStartUtc = session && session.market_open_utc
    ? new Date(session.market_open_utc)
    : (session && session.collection_start_utc ? new Date(session.collection_start_utc) : null);
  if (flowStartUtc && !Number.isNaN(flowStartUtc.getTime())) {
    points = (points || []).filter(p => p.time && new Date(p.time).getTime() >= flowStartUtc.getTime());
  }
  const moneyness = document.getElementById("trackerMoneyness")?.value || "ATM";
  const expiryFilter = document.getElementById("trackerExpiry")?.value || "0DTE";
  points = expiryFilter === "0DTE" ? points.filter(p => p.expiry !== "ALL") : points;
  points = [...points].sort((a, b) => new Date(a.time) - new Date(b.time));
  const intervalMin = Number(document.getElementById("trackerInterval")?.value || 1);
  const intervalMs = intervalMin * 60 * 1000;
  const startUtc = flowStartUtc || (points[0]?.time ? new Date(points[0].time) : new Date());
  const dataTimes = points.map(p => new Date(p.time)).filter(d => !Number.isNaN(d.getTime()));
  const lastDataUtc = dataTimes.length ? new Date(Math.max(...dataTimes.map(d => d.getTime()))) : startUtc;
  const closeUtc = session?.market_close_utc && new Date(session.market_close_utc).getTime() < lastDataUtc.getTime()
    ? new Date(session.market_close_utc)
    : lastDataUtc;
  const bucketCount = Math.max(1, Math.ceil((closeUtc.getTime() - startUtc.getTime()) / intervalMs));
  const rows = Array.from({length: bucketCount}, (_, i) => ({
    bucket: new Date(startUtc.getTime() + i * intervalMs),
    callDelta: 0,
    putDelta: 0,
    callPrem: 0,
    putPrem: 0,
    callNet: 0,
    putNet: 0,
    callCum: null,
    putCum: null,
    callGrossCumK: null,
    putGrossCumK: null,
    callNetCumK: null,
    putNetCumK: null,
    hasData: false
  }));
  let prevCall = null, prevPut = null;
  let prevCallMid = null, prevPutMid = null;
  points.forEach(p => {
    const ts = new Date(p.time).getTime();
    const idx = Math.floor((ts - startUtc.getTime()) / intervalMs);
    if (idx < 0 || idx >= rows.length) return;
    const agg = moneynessAggregate(p, moneyness);
    const call = Number(agg.call || 0);
    const put = Number(agg.put || 0);
    const callMid = Number.isFinite(agg.callMid) ? Number(agg.callMid) : null;
    const putMid = Number.isFinite(agg.putMid) ? Number(agg.putMid) : null;
    if (prevCall !== null && prevPut !== null) {
      const callVolDelta = Math.max(0, call - prevCall);
      const putVolDelta = Math.max(0, put - prevPut);
      rows[idx].callDelta += callVolDelta;
      rows[idx].putDelta += putVolDelta;
      rows[idx].hasData = true;
      // Premium delta = volume delta x current mid price x contract multiplier (100).
      // Sign is a tick-rule heuristic (mid up since last poll = buy pressure, down = sell
      // pressure) — approximate, not real trade-side classification.
      const callMidNow = callMid !== null ? callMid : prevCallMid;
      const putMidNow = putMid !== null ? putMid : prevPutMid;
      if (callMidNow !== null) {
        const grossCall = callVolDelta * callMidNow * 100;
        rows[idx].callPrem += grossCall;
        const sign = (prevCallMid !== null && callMidNow < prevCallMid) ? -1 : 1;
        rows[idx].callNet += sign * grossCall;
      }
      if (putMidNow !== null) {
        const grossPut = putVolDelta * putMidNow * 100;
        rows[idx].putPrem += grossPut;
        const sign = (prevPutMid !== null && putMidNow < prevPutMid) ? -1 : 1;
        rows[idx].putNet += sign * grossPut;
      }
    }
    prevCall = Number.isFinite(call) ? call : prevCall;
    prevPut = Number.isFinite(put) ? put : prevPut;
    if (callMid !== null) prevCallMid = callMid;
    if (putMid !== null) prevPutMid = putMid;
  });
  let callCum = 0, putCum = 0, callGrossCum = 0, putGrossCum = 0, callNetCum = 0, putNetCum = 0;
  rows.forEach(row => {
    callCum += row.callDelta;
    putCum += row.putDelta;
    callGrossCum += row.callPrem;
    putGrossCum += row.putPrem;
    callNetCum += row.callNet;
    putNetCum += row.putNet;
    if (row.hasData) {
      row.callCum = -callCum / 1000;
      row.putCum = putCum / 1000;
      row.callGrossCumK = callGrossCum / 1000;
      row.putGrossCumK = putGrossCum / 1000;
      row.callNetCumK = callNetCum / 1000;
      row.putNetCumK = putNetCum / 1000;
    }
    row.callDeltaK = row.callDelta / 1000;
    row.putDeltaK = row.putDelta / 1000;
    row.callPremK = row.callPrem / 1000;
    row.putPremK = row.putPrem / 1000;
  });
  return {rows, intervalMin, startUtc};
}

function drawFlowTracker(points, session) {
  const {canvas, margin, plot, cssW, cssH} = trackerMetrics();
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#0B0F14";
  ctx.fillRect(0, 0, cssW, cssH);

  const {rows, intervalMin, startUtc} = buildTrackerRows(points, session);
  trackerState.points = points || [];
  trackerState.session = session;
  trackerState.bucketCount = rows.length;
  if (!(points || []).length) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for data...", 16, 24);
    return;
  }
  const metric = document.getElementById("trackerMode")?.value || "CALL_PUT";
  const isPremium = metric === "PREMIUM";
  const barCallField = isPremium ? "callPremK" : "callDeltaK";
  const barPutField = isPremium ? "putPremK" : "putDeltaK";
  const lineCallField = isPremium ? "callNetCumK" : "callCum";
  const linePutField = isPremium ? "putNetCumK" : "putCum";
  const fmtAxis = v => isPremium ? "$" + formatTrackerK(v) : formatTrackerK(v);
  const dateEl = document.getElementById("trackerDate");
  if (dateEl) {
    const d = session?.trading_date || (startUtc ? startUtc.toISOString().slice(0, 10) : "--");
    const [yyyy, mm, dd] = String(d).split("-");
    dateEl.textContent = yyyy && mm && dd ? `${dd}/${mm}/${yyyy}` : "--";
  }
  const tickerEl = document.getElementById("trackerTicker");
  if (tickerEl) tickerEl.textContent = latestState?.latest_summary?.ticker || "QQQ";
  const sessionKey = "tracker-" + (session?.trading_date || "") + "-" + intervalMin + "-" +
    (document.getElementById("trackerMoneyness")?.value || "ATM") + "-" +
    (document.getElementById("trackerExpiry")?.value || "0DTE") +
    (session?.history_snapshot_id ? "-" + session.history_snapshot_id : "");
  if (sessionKey !== trackerState.sessionKey) {
    trackerState.sessionKey = sessionKey;
    trackerState.viewX = [0, rows.length];
  } else {
    trackerState.viewX = clampView(trackerState.viewX, 0, rows.length);
  }
  const domainSpan = Math.max(1, trackerState.viewX[1] - trackerState.viewX[0]);
  const x = i => plot.x + (i + 0.5 - trackerState.viewX[0]) / domainSpan * plot.w;
  const valid = rows.flatMap(r => [
    r[barCallField],
    r[barPutField],
    r[lineCallField],
    r[linePutField]
  ]).filter(Number.isFinite);
  const maxPos = Math.max(10, ...valid.filter(v => v >= 0), 10);
  const maxNeg = Math.min(-10, ...valid.filter(v => v < 0), -10);
  const topPad = Math.max(5, maxPos * 0.18);
  const botPad = Math.max(5, Math.abs(maxNeg) * 0.18);
  const yMax = maxPos + topPad;
  const yMin = maxNeg - botPad;
  const y = v => plot.y + plot.h - ((v - yMin) / (yMax - yMin)) * plot.h;
  const zeroY = y(0);

  ctx.strokeStyle = "rgba(148,163,184,0.18)";
  ctx.lineWidth = 1;
  [yMax - topPad, 0, yMin + botPad].forEach(v => {
    const py = y(v);
    ctx.beginPath(); ctx.moveTo(plot.x, py); ctx.lineTo(plot.x + plot.w, py); ctx.stroke();
  });
  ctx.fillStyle = "rgba(148,163,184,0.55)";
  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.textAlign = "right";
  ctx.fillText(fmtAxis(yMax - topPad), plot.x - 8, y(yMax - topPad) + 4);
  ctx.fillText(fmtAxis(0), plot.x - 8, zeroY + 4);
  ctx.fillText(fmtAxis(yMin + botPad), plot.x - 8, y(yMin + botPad) + 4);

  ctx.save();
  ctx.beginPath();
  ctx.rect(plot.x, plot.y, plot.w, plot.h);
  ctx.clip();
  const barSlot = Math.max(1.2, plot.w / domainSpan);
  const barW = Math.max(1, Math.min(3.2, barSlot * 0.22));
  rows.forEach((r, i) => {
    const px = x(i);
    const callBar = r[barCallField];
    const putBar = r[barPutField];
    if (callBar > 0) {
      const h = Math.max(1, Math.abs(y(callBar) - zeroY));
      ctx.fillStyle = rgbaFromHex(COLORS.cyan, 0.72);
      ctx.fillRect(px - barW - 0.6, zeroY - h, barW, h);
    }
    if (putBar > 0) {
      const h = Math.max(1, Math.abs(y(putBar) - zeroY));
      ctx.fillStyle = rgbaFromHex(COLORS.orange, 0.72);
      ctx.fillRect(px + 0.6, zeroY - h, barW, h);
    }
  });
  function trackerLine(series, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.65;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    let started = false;
    rows.forEach((r, i) => {
      const v = r[series];
      if (!Number.isFinite(v)) return;
      const px = x(i), py = y(v);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }
  trackerLine(linePutField, COLORS.orange);
  trackerLine(lineCallField, COLORS.cyan);
  ctx.restore();

  ctx.fillStyle = COLORS.text;
  ctx.font = "700 16px Inter, system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("Flow Tracker" + (isPremium ? " · Premium $ (beta)" : ""), 14, 24);
  ctx.fillStyle = "rgba(148,163,184,0.62)";
  ctx.font = "11px Menlo, Consolas, monospace";
  const tickEvery = Math.max(1, Math.floor(rows.length / 8));
  rows.forEach((r, i) => {
    if (i % tickEvery !== 0 && i !== rows.length - 1) return;
    ctx.textAlign = "center";
    ctx.fillText(fmtHM(r.bucket), x(i), cssH - 8);
  });

  canvas.onmousemove = ev => {
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    if (trackerDrag) {
      const dxDomain = (ev.clientX - trackerDrag.startX) / plot.w * (trackerDrag.viewX[1] - trackerDrag.viewX[0]);
      trackerState.viewX = clampView([trackerDrag.viewX[0] - dxDomain, trackerDrag.viewX[1] - dxDomain], 0, trackerState.bucketCount);
      drawFlowTracker(points, session);
      document.getElementById("trackerTooltip")?.style.setProperty("display", "none");
      return;
    }
    const idx = Math.max(0, Math.min(rows.length - 1, Math.floor(trackerState.viewX[0] + (mx - plot.x) / plot.w * domainSpan)));
    const r = rows[idx];
    drawFlowTracker(points, session);
    const c = canvas.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.strokeStyle = "rgba(203,213,225,0.45)";
    c.setLineDash([4, 4]);
    c.beginPath(); c.moveTo(x(idx), plot.y); c.lineTo(x(idx), plot.y + plot.h); c.stroke();
    c.setLineDash([]);
    const tip = document.getElementById("trackerTooltip");
    if (tip) {
      tip.style.display = "block";
      const tipW = isPremium ? 240 : 230;
      tip.style.left = Math.min(cssW - tipW, mx + 14) + "px";
      tip.style.top = Math.max(4, ev.clientY - rect.top + 12) + "px";
      tip.style.whiteSpace = isPremium ? "normal" : "nowrap";
      tip.style.maxWidth = isPremium ? tipW + "px" : "";
      if (isPremium) {
        const fmtD = v => "$" + formatTrackerK(v);
        const netTotal = (r.callNetCumK || 0) + (r.putNetCumK || 0);
        tip.innerHTML = `<b>${fmtHM(r.bucket)} ET</b><br>` +
          `<b>PREMIUM</b><br>` +
          `<span style="color:${COLORS.cyan}">Call ${fmtD(r.callPremK)}</span> ` +
          `<span style="color:${COLORS.orange}">Put ${fmtD(r.putPremK)}</span><br>` +
          `<b>RUNNING GROSS</b><br>` +
          `<span style="color:${COLORS.cyan}">Calls ${fmtD(r.callGrossCumK)}</span> ` +
          `<span style="color:${COLORS.orange}">Puts ${fmtD(r.putGrossCumK)}</span><br>` +
          `<b>SESSION FLOW (est.)</b><br>` +
          `<span style="color:${COLORS.cyan}">Calls ${fmtD(r.callNetCumK)}</span> ` +
          `<span style="color:${COLORS.orange}">Puts ${fmtD(r.putNetCumK)}</span><br>` +
          `<span style="color:${COLORS.text}">Running total ${fmtD(netTotal)}</span><br>` +
          `<span style="color:rgba(148,163,184,0.75); font-size:10px;">Ước tính theo tick-rule (mid tăng/giảm), không phải phân loại giao dịch thực</span>`;
      } else {
        tip.innerHTML = `<b>${fmtHM(r.bucket)} ET</b><br>` +
          `<span style="color:${COLORS.cyan}">Call flow ${formatTrackerK(r.callDeltaK)}</span><br>` +
          `<span style="color:${COLORS.orange}">Put flow ${formatTrackerK(r.putDeltaK)}</span><br>` +
          `<span style="color:${COLORS.text}">Net ${formatTrackerK(r.callDeltaK - r.putDeltaK)}</span>`;
      }
    }
  };
  canvas.onmouseleave = () => {
    trackerDrag = null;
    const tip = document.getElementById("trackerTooltip");
    if (tip) tip.style.display = "none";
    drawFlowTracker(points, session);
  };
}

function onTrackerWheel(e) {
  if (trackerState.bucketCount <= 1) return;
  e.preventDefault();
  const {canvas, plot} = trackerMetrics();
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const factor = Math.exp((e.deltaY > 0 ? 1 : -1) * 0.15);
  trackerState.viewX = zoomAxis(trackerState.viewX, px, plot.x, plot.w, factor, 0, trackerState.bucketCount, false);
  drawFlowTracker(trackerState.points, trackerState.session);
}

function onTrackerMouseDown(e) {
  if (!trackerState.bucketCount) return;
  trackerDrag = {startX: e.clientX, viewX: [...trackerState.viewX]};
}

function onTrackerMouseUp() {
  trackerDrag = null;
}

function initTrackerControls() {
  const canvas = document.getElementById("trackerCanvas");
  if (canvas) {
    canvas.addEventListener("wheel", onTrackerWheel, {passive: false});
    canvas.addEventListener("mousedown", onTrackerMouseDown);
    window.addEventListener("mouseup", onTrackerMouseUp);
  }
}
initTrackerControls();

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

function ensureSvgGradient(svg, id, stops, x1, y1, x2, y2) {
  if (!svg) return;
  let defs = svg.querySelector("defs");
  if (!defs) {
    defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    svg.prepend(defs);
  }
  let grad = defs.querySelector("#" + id);
  if (!grad) {
    grad = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
    grad.setAttribute("id", id);
    defs.appendChild(grad);
  }
  grad.setAttribute("gradientUnits", "objectBoundingBox");
  grad.setAttribute("x1", x1);
  grad.setAttribute("y1", y1);
  grad.setAttribute("x2", x2);
  grad.setAttribute("y2", y2);
  grad.replaceChildren();
  stops.forEach(([offset, color, opacity]) => {
    const stop = document.createElementNS("http://www.w3.org/2000/svg", "stop");
    stop.setAttribute("offset", offset);
    stop.setAttribute("stop-color", color);
    stop.setAttribute("stop-opacity", String(opacity));
    grad.appendChild(stop);
  });
}

function applyBarFade(id, mode) {
  const gd = document.getElementById(id);
  const svg = gd?.querySelector("svg.main-svg");
  if (!gd || !svg) return;
  const prefix = id.replace(/[^a-zA-Z0-9_-]/g, "");
  ensureSvgGradient(svg, `${prefix}-h-pos`, [["0%", COLORS.cyan, 1], ["62%", COLORS.cyan, 0.72], ["100%", "#000000", 0.05]], 0, 0, 1, 0);
  ensureSvgGradient(svg, `${prefix}-h-neg`, [["0%", "#000000", 0.05], ["38%", COLORS.orange, 0.72], ["100%", COLORS.orange, 1]], 0, 0, 1, 0);
  ensureSvgGradient(svg, `${prefix}-v-call`, [["0%", "#000000", 0.05], ["38%", COLORS.cyan, 0.72], ["100%", COLORS.cyan, 1]], 0, 0, 0, 1);
  ensureSvgGradient(svg, `${prefix}-v-put`, [["0%", "#000000", 0.05], ["38%", COLORS.orange, 0.72], ["100%", COLORS.orange, 1]], 0, 0, 0, 1);
  const renderedTraces = (gd.data || []).filter(trace => trace.type === "bar" && Array.isArray(trace.x) && trace.x.length);
  gd.querySelectorAll(".barlayer .trace.bars").forEach((trace, traceIdx) => {
    let fill = "";
    const side = renderedTraces[traceIdx]?.meta?.fadeSide;
    if (mode === "exposure") fill = `url(#${prefix}-${side === "neg" ? "h-neg" : "h-pos"})`;
    else if (mode === "oi") fill = `url(#${prefix}-${side === "put" ? "v-put" : "v-call"})`;
    if (!fill) return;
    trace.querySelectorAll("path").forEach(path => {
      path.setAttribute("fill", fill);
      path.style.fill = fill;
      path.style.opacity = "1";
    });
  });
}

function reactWithBarFade(id, data, layout, config, mode) {
  return Plotly.react(id, data, layout, config).then(() => {
    const gd = document.getElementById(id);
    const apply = () => applyBarFade(id, mode);
    apply();
    requestAnimationFrame(apply);
    if (gd && !gd.dataset.fadeHooked) {
      gd.dataset.fadeHooked = "1";
      gd.on?.("plotly_afterplot", apply);
    }
  });
}

const EXPOSURE_CONFIG = {
  net_gex: {label: "GEX Exposure", callKey: "call_gex", putKey: "put_gex", wallAbsKey: "gamma_wall_abs", flipKey: "gamma_flip", wallLabel: "GAMMA WALL", flipLabel: "GAMMA FLIP"},
  net_dex: {label: "DEX Exposure", callKey: "call_dex", putKey: "put_dex", wallAbsKey: "dex_wall_abs", flipKey: "delta_flip", wallLabel: "DELTA WALL", flipLabel: "DELTA FLIP"},
  net_vex: {label: "VEX Exposure", callKey: "call_vex", putKey: "put_vex", wallAbsKey: "vanna_wall_abs", flipKey: "vanna_flip", wallLabel: "VANNA WALL", flipLabel: "VANNA FLIP"},
  net_chex: {label: "CHEX Exposure", callKey: "call_chex", putKey: "put_chex", wallAbsKey: "charm_wall_abs", flipKey: "charm_flip", wallLabel: "CHARM WALL", flipLabel: "CHARM FLIP"},
};

const exposurePanelMetric = { gex: "net_gex", dex: "net_dex" };
let lastDrawState = null;

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
  const cfg = EXPOSURE_CONFIG[key];
  const label = cfg.label;
  const callKey = cfg.callKey;
  const putKey = cfg.putKey;
  const buildExposureTrace = (sideRows, color, name, fadeSide) => ({
    x: sideRows.map(r => Number(r[key]) || 0),
    y: sideRows.map(r => Number(r.strike)),
    type: "bar",
    orientation: "h",
    width: 0.52,
    marker: {
      color,
      opacity: 1,
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
    meta: {fadeSide},
    name
  });
  const data = [
    buildExposureTrace(cleanRows.filter(r => Number(r[key]) >= 0), positiveColor, label + " +", "pos"),
    buildExposureTrace(cleanRows.filter(r => Number(r[key]) < 0), negativeColor, label + " -", "neg"),
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

  const wallAbs = summary?.[cfg.wallAbsKey];
  if (wallAbs) {
    shapes.push(exposureLine(Number(wallAbs), "#A78BFA", "dashdot"));
    annotations.push(exposureLabel(cfg.wallLabel + " " + Number(wallAbs).toFixed(0), Number(wallAbs), "#A78BFA", "right"));
  }
  const flip = summary?.[cfg.flipKey];
  if (flip) {
    shapes.push(exposureLine(Number(flip), "#C084FC", "dot"));
    annotations.push(exposureLabel(cfg.flipLabel + " " + Number(flip).toFixed(2), Number(flip), "#C084FC", "right"));
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
  reactWithBarFade(id, data, layout, {displayModeBar: false, scrollZoom: true, responsive: true}, "exposure");
}

function drawOi(rows, summary) {
  const x = rows.map(r => r.strike);
  const data = [
    {x, y: rows.map(r => r.call_oi), type: "bar", name: "Calls", marker: {color: COLORS.cyan}, meta: {fadeSide: "call"}},
    {x, y: rows.map(r => r.put_oi), type: "bar", name: "Puts", marker: {color: COLORS.orange}, meta: {fadeSide: "put"}},
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
  reactWithBarFade("oi", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true}, "oi");
}

function drawOiIv(rows, summary) {
  const x = rows.map(r => r.strike);
  const data = [
    {x, y: rows.map(r => r.call_oi), type: "bar", name: "Calls", marker: {color: COLORS.cyan}, meta: {fadeSide: "call"}},
    {x, y: rows.map(r => r.put_oi), type: "bar", name: "Puts", marker: {color: COLORS.orange}, meta: {fadeSide: "put"}},
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
  reactWithBarFade("oiiv", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true}, "oi");
}

const TENOR_COLORS = [COLORS.orange, COLORS.cyan, COLORS.green, "#7C3AED", "#F472B6", COLORS.yellow];

function drawSkew(rows, summary, tenors) {
  syncSkewContext(summary);
  if ((tenors || []).length >= 1) {
    drawSkewTenors(tenors, Number(summary?.spot));
    return;
  }
  drawSkewSingle(rows, summary);
}

function syncSkewContext(summary) {
  const dateNode = document.getElementById("skewDate");
  const tickerNode = document.getElementById("skewTicker");
  const rawDate = String(summary?.requested_snapshot_date || summary?.effective_snapshot_date || "");
  if (dateNode) {
    const parsed = rawDate ? new Date(`${rawDate.slice(0, 10)}T00:00:00`) : null;
    dateNode.textContent = parsed && !Number.isNaN(parsed.getTime())
      ? parsed.toLocaleDateString("en-GB")
      : "--";
  }
  if (tickerNode) tickerNode.textContent = String(summary?.ticker || "QQQ").toUpperCase();
}

// Rebuild the #skewExpiry <select> from the tenors currently in the live
// state ("Nearest" plus one option per available tenor). Only touches the
// DOM when the set of expiries actually changed, so an in-progress user
// selection survives normal polling redraws. Falls back to "nearest" if the
// previously selected expiry disappears from the data.
function syncSkewExpiryOptions(tenors) {
  const sel = document.getElementById("skewExpiry");
  if (!sel) return;
  const key = tenors.map(t => t.expiry).join("|");
  if (key !== skewExpiryOptionsKey) {
    skewExpiryOptionsKey = key;
    const options = ['<option value="nearest">Nearest</option>'];
    tenors.forEach(tenor => {
      const name = Number.isFinite(tenor.dte) ? `${tenor.dte}DTE` : "Expiry";
      const dateLabel = tenor.expiry
        ? new Date(tenor.expiry).toLocaleDateString("en-US", {month: "short", day: "2-digit"})
        : tenor.expiry;
      options.push(`<option value="${tenor.expiry}">${name} · ${dateLabel}</option>`);
    });
    sel.innerHTML = options.join("");
    const stillValid = skewExpirySelected === "nearest" || tenors.some(t => t.expiry === skewExpirySelected);
    if (!stillValid) skewExpirySelected = "nearest";
    sel.value = skewExpirySelected;
  }
}

function nearestSkewPoint(trace, strike) {
  const xs = trace.x || [];
  const ys = trace.y || [];
  let best = null;
  xs.forEach((rawX, index) => {
    const x = Number(rawX);
    const y = Number(ys[index]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    const distance = Math.abs(x - strike);
    if (!best || distance < best.distance) best = {x, y, distance};
  });
  return best;
}

function skewStrikeStep(traces) {
  const xs = [...new Set(traces.flatMap(trace => (trace.x || []).map(Number).filter(Number.isFinite)))].sort((a, b) => a - b);
  const gaps = xs.slice(1).map((x, index) => x - xs[index]).filter(gap => gap > 0);
  if (!gaps.length) return 1;
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)] || 1;
}

function skewTooltipHtml(traces, strike) {
  const groups = new Map();
  const tolerance = Math.max(0.01, skewStrikeStep(traces) * 0.45);
  traces.forEach(trace => {
    if (!trace.meta || !trace.meta.skew) return;
    const point = nearestSkewPoint(trace, strike);
    if (!point || point.distance > tolerance) return;
    const {tenor, side, color} = trace.meta.skew;
    if (!groups.has(tenor)) groups.set(tenor, {color, values: []});
    groups.get(tenor).values.push({side, value: point.y});
  });
  const order = {C: 0, P: 1, IV: 2};
  const lines = [];
  groups.forEach((group, tenor) => {
    group.values
      .sort((a, b) => order[a.side] - order[b.side])
      .forEach(item => lines.push(`<div><span style="color:${group.color}">${tenor} ${item.side}</span> <span>${item.value.toFixed(1)}% IV</span></div>`));
  });
  return lines.length
    ? `<div class="strike">strike ${Number(strike).toFixed(Number.isInteger(Number(strike)) ? 0 : 1)}</div>${lines.join("")}`
    : "";
}

function installSkewHover(data, baseShapes) {
  const gd = document.getElementById("skew");
  const tooltip = document.getElementById("skewTooltip");
  if (!gd || !tooltip || !gd.on) return;
  if (typeof gd.removeAllListeners === "function") {
    gd.removeAllListeners("plotly_hover");
    gd.removeAllListeners("plotly_unhover");
  }
  const restore = () => {
    tooltip.style.display = "none";
    Plotly.relayout(gd, {shapes: baseShapes});
  };
  gd.on("plotly_hover", event => {
    const point = (event.points || []).find(item => Number.isFinite(Number(item.x)) && Number.isFinite(Number(item.y)));
    if (!point) return;
    const strike = Number(point.x);
    const iv = Number(point.y);
    const html = skewTooltipHtml(data, strike);
    if (!html) return;
    tooltip.innerHTML = html;
    const box = gd.parentElement.getBoundingClientRect();
    const width = 196;
    const height = Math.max(62, 27 + html.split("<div").length * 18);
    const rawLeft = Number(event.event?.clientX || box.left) - box.left + 14;
    const rawTop = Number(event.event?.clientY || box.top) - box.top + 14;
    tooltip.style.left = `${Math.max(8, Math.min(rawLeft, box.width - width - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(rawTop, box.height - height - 8))}px`;
    tooltip.style.display = "block";
    const step = skewStrikeStep(data);
    const hoverShapes = [
      ...baseShapes,
      {type: "rect", x0: strike - step * 0.34, x1: strike + step * 0.34, y0: 0, y1: 1, xref: "x", yref: "paper", fillcolor: "rgba(148,163,184,0.10)", line: {width: 0}, layer: "below"},
      {type: "line", x0: strike, x1: strike, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: "#64748B", width: 1, dash: "dot"}},
      {type: "line", x0: 0, x1: 1, y0: iv, y1: iv, xref: "paper", yref: "y", line: {color: "#64748B", width: 1, dash: "dot"}}
    ];
    Plotly.relayout(gd, {shapes: hoverShapes});
  });
  gd.on("plotly_unhover", restore);
}

function renderSkewChart(data, layout) {
  const baseShapes = (layout.shapes || []).map(shape => ({...shape}));
  return Plotly.react("skew", data, layout, {
    displayModeBar: false, scrollZoom: true, responsive: true
  }).then(() => installSkewHover(data, baseShapes));
}

// quantdecay-style: one color per tenor (0DTE, 1DTE), three line styles per
// tenor - Calls solid, Puts dashed, blended IV dotted - instead of a single
// blended line. No legend box; the header annotation carries the per-tenor
// ATM IV labels the same way drawIvRankChart does for its own header.
// The expiry dropdown filters which tenor(s) are drawn ("nearest" = all
// currently available near-term tenors, same as the original default); the
// Calls/Puts/IV toggle filters which line styles are drawn within that set.
function drawSkewTenors(tenors, spot) {
  syncSkewExpiryOptions(tenors);
  const tenorsToShow = skewExpirySelected === "nearest"
    ? tenors
    : tenors.filter(t => t.expiry === skewExpirySelected);
  const labels = [];
  const data = [];
  tenorsToShow.forEach((tenor, i) => {
    const color = tenor.color || TENOR_COLORS[i % TENOR_COLORS.length];
    const name = Number.isFinite(tenor.dte) ? `${tenor.dte}DTE` : tenor.expiry;
    if (Number.isFinite(Number(tenor.atm_iv))) {
      labels.push(`<span style="color:${color}">${name}</span> <span style="color:${COLORS.muted}">${(Number(tenor.atm_iv) * 100).toFixed(1)}%</span>`);
    }
    const sides = [
      {key: "call", label: "C", dash: "solid"},
      {key: "put", label: "P", dash: "dash"},
      {key: "iv", label: "IV", dash: "dot"}
    ].filter(side => skewSeriesVisible[side.key]);
    sides.forEach(side => {
      const curve = tenor[side.key];
      if (!curve || !curve.strike || !curve.strike.length) return;
      data.push({
        x: curve.strike,
        y: curve.iv.map(v => Number(v) * 100),
        type: "scatter",
        mode: "lines",
        name: `${name} ${side.label}`,
        showlegend: false,
        meta: {skew: {tenor: name, side: side.label, color}},
        line: {color, width: 2, dash: side.dash},
        hovertemplate: "<extra></extra>"
      });
    });
  });
  const layout = baseLayout(340);
  layout.margin = {l: 70, r: 28, t: 55, b: 48};
  layout.showlegend = false;
  layout.hovermode = "closest";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  if (Number.isFinite(spot)) {
    layout.shapes = [{type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
  }
  if (labels.length) {
    layout.annotations = [{x: 0, y: 1.16, xref: "paper", yref: "paper", text: labels.join(" · "), showarrow: false, font: {color: COLORS.muted, size: 11}, xanchor: "left"}];
  }
  renderSkewChart(data, layout);
}

function drawSkewSingle(rows, summary) {
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
  // Strikes with no open interest and no traded volume have no real quote behind
  // them; their IV is a stale/extrapolated BSM back-solve, not a market price.
  const hasLiquidity = (row, side) => {
    const oi = Number(row[side + "_oi"]) || 0;
    const vol = Number(row[side + "_volume"]) || 0;
    return oi > 0 || vol > 0;
  };
  const atmValues = [];
  if (atmRow && hasLiquidity(atmRow, "call") && Number.isFinite(Number(atmRow.call_iv_pct))) atmValues.push(Number(atmRow.call_iv_pct));
  if (atmRow && hasLiquidity(atmRow, "put") && Number.isFinite(Number(atmRow.put_iv_pct))) atmValues.push(Number(atmRow.put_iv_pct));
  const atmIv = atmValues.length ? atmValues.reduce((a, b) => a + b, 0) / atmValues.length : NaN;
  const atmPoint = Number.isFinite(atmIv) ? [{strike: atmStrike, iv: atmIv}] : [];
  const callRows = [
    ...atmPoint,
    ...clean
      .filter(r => Number(r.strike) > atmStrike && hasLiquidity(r, "call") && Number.isFinite(Number(r.call_iv_pct)))
      .map(r => ({strike: Number(r.strike), iv: Number(r.call_iv_pct)}))
  ];
  const putRows = [
    ...clean
      .filter(r => Number(r.strike) < atmStrike && hasLiquidity(r, "put") && Number.isFinite(Number(r.put_iv_pct)))
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
  const data = [];
  if (skewSeriesVisible.call) {
    data.push({
      x: smoothCallRows.map(r => r.strike),
      y: smoothCallRows.map(r => r.iv),
      type: "scatter",
      mode: "lines",
      name: "Calls",
      line: {color: COLORS.cyan, width: 2},
      meta: {skew: {tenor: "0DTE", side: "C", color: COLORS.cyan}},
      hovertemplate: "<extra></extra>"
    });
  }
  if (skewSeriesVisible.put) {
    data.push({
      x: smoothPutRows.map(r => r.strike),
      y: smoothPutRows.map(r => r.iv),
      type: "scatter",
      mode: "lines",
      name: "Puts",
      line: {color: COLORS.orange, width: 2, dash: "dash"},
      meta: {skew: {tenor: "0DTE", side: "P", color: COLORS.orange}},
      hovertemplate: "<extra></extra>"
    });
  }
  const layout = baseLayout(340);
  layout.margin = {l: 70, r: 28, t: 55, b: 48};
  layout.legend = rightLegend();
  layout.hovermode = "closest";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  if (Number.isFinite(spot)) {
    layout.shapes = [{type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
  }
  renderSkewChart(data, layout);
}

function drawIvRank(history, summary) {
  const rows = (history || [])
    .filter(r => Number.isFinite(Number(r.avg_iv_pct)) && Number.isFinite(Number(r.spot)))
    .sort((a, b) => new Date(a.snapshot_utc || a.snapshot_vn) - new Date(b.snapshot_utc || b.snapshot_vn));
  drawIvRankChart(rows);
}

// Single quantdecay-style renderer: rank line (0-100 axis, 0-20%/80-100%
// shaded bands, 50% reference line) with Spot normalized into the same
// 0-100 space instead of a secondary $ axis, no legend box (header
// annotation identifies the lines), and an end-of-line marker + value
// label. Works with as few as 1 day of history — early points before a
// real min-max rank exists (see build_iv_rank_history_rows) just leave a
// gap in the rank line rather than switching to a different chart.
function drawIvRankChart(rows) {
  if (!rows.length) {
    const layout = baseLayout(340);
    layout.margin.t = 55;
    layout.annotations = [{
      x: 0.5, y: 0.5, xref: "paper", yref: "paper", showarrow: false,
      text: "Not enough local daily snapshots yet for IV Rank",
      font: {color: COLORS.muted, size: 13}
    }];
    Plotly.react("ivrank", [], layout, {displayModeBar: false, scrollZoom: false, responsive: true});
    return;
  }
  const x = rows.map(r => r.snapshot_vn || r.snapshot_utc);
  const y = rows.map(r => Number.isFinite(Number(r.iv_rank_pct)) ? Number(r.iv_rank_pct) : null);
  const hasRank = y.some(v => v !== null);
  const spotRaw = rows.map(r => Number(r.spot));
  const spotMin = Math.min(...spotRaw);
  const spotMax = Math.max(...spotRaw);
  const spotSpan = spotMax - spotMin || 1;
  const spotNorm = spotRaw.map(v => ((v - spotMin) / spotSpan) * 100);
  const tickText = x.map(v => new Date(v).toLocaleDateString("en-US", {month: "short", day: "2-digit"}));
  const current = rows[rows.length - 1];
  const formatHeader = row => hasRank
    ? `rank ${Number(row.iv_rank_pct ?? 0).toFixed(1)}% · IV ${Number(row.avg_iv_pct || 0).toFixed(1)}% · $${Number(row.spot || 0).toFixed(2)}`
    : `rank warming up · IV ${Number(row.avg_iv_pct || 0).toFixed(1)}% · $${Number(row.spot || 0).toFixed(2)}`;
  const lastIdx = rows.length - 1;
  const data = [
    {
      x, y, type: "scatter", mode: "lines", name: "IV Rank",
      line: {color: COLORS.cyan, width: 2.5}, connectgaps: true,
      customdata: rows.map(r => [Number(r.avg_iv_pct), Number(r.spot), r.iv_rank_pct]),
      hovertemplate: "%{x|%b %d}<extra></extra>"
    },
    {
      x, y: spotNorm, type: "scatter", mode: "lines", name: "Spot",
      line: {color: COLORS.spot, width: 1.25, dash: "dot"}, hoverinfo: "skip"
    },
    {
      x: [x[lastIdx]], y: [y[lastIdx]], type: "scatter", mode: "markers+text", showlegend: false,
      marker: {color: COLORS.cyan, size: 7},
      text: [Number(spotRaw[lastIdx]).toFixed(2)],
      textposition: "middle right",
      textfont: {color: COLORS.cyan, size: 11},
      hoverinfo: "skip"
    }
  ];
  const layout = baseLayout(340);
  layout.margin.t = 55;
  layout.margin.r = 45;
  layout.showlegend = false;
  layout.dragmode = false;
  // category axis (not date) so we can pad range in category units and
  // leave room for the end-of-line price label without it clipping at the
  // plot edge.
  layout.xaxis = {
    type: "category", showgrid: false, zeroline: false, color: COLORS.muted,
    tickmode: "array", tickvals: x, ticktext: tickText, tickangle: 0, fixedrange: true,
    range: [-0.5, rows.length - 0.35]
  };
  layout.yaxis = {
    showgrid: false, zeroline: false, color: COLORS.muted, range: [0, 100], fixedrange: true,
    tickmode: "array", tickvals: [0, 20, 50, 80, 100], ticktext: ["0%", "20%", "50%", "80%", "100%"]
  };
  layout.shapes = [
    {type: "rect", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 0, y1: 20, fillcolor: "rgba(34,211,238,0.10)", line: {width: 0}, layer: "below"},
    {type: "rect", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 80, y1: 100, fillcolor: "rgba(245,158,11,0.12)", line: {width: 0}, layer: "below"},
    {type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 50, y1: 50, line: {color: COLORS.muted, width: 1, dash: "dot"}, layer: "below"}
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

function drawCandles(candles, candlesError) {
  if (!candles || !candles.length) {
    const layout = baseLayout(380);
    layout.margin.t = 55;
    layout.annotations = [{
      x: 0.5, y: 0.5, xref: "paper", yref: "paper", showarrow: false,
      text: candlesError || "Waiting for Alpaca candles...",
      font: {color: COLORS.muted, size: 13}
    }];
    Plotly.react("candles", [], layout, {displayModeBar: false, scrollZoom: false, responsive: true});
    return;
  }
  const data = [{
    x: candles.map(c => c.t),
    open: candles.map(c => c.o),
    high: candles.map(c => c.h),
    low: candles.map(c => c.l),
    close: candles.map(c => c.c),
    type: "candlestick",
    increasing: {line: {color: COLORS.cyan}},
    decreasing: {line: {color: COLORS.orange}},
    showlegend: false
  }];
  const layout = baseLayout(380);
  layout.xaxis = {...layout.xaxis, rangeslider: {visible: false}};
  Plotly.react("candles", data, layout, {displayModeBar: false, scrollZoom: false, responsive: true});
}

// ---- Heat Tracker: hand-rolled canvas 2D renderer ----
// Band-scale axes (real strikes / fixed time buckets), snapshot-per-bucket
// GEX values, diverging intensity-floor color map, and view-only zoom/pan —
// see tasks context: rebuilt per user's own architecture spec of the
// Quantdecay heat tracker (Plotly could not express band scales/pure view
// transforms, so this chart no longer goes through Plotly.react at all).

function loadHeatPrefs() {
  let interval = 1, mode = "blocks", spotMode = "line", intensity = 35;
  try {
    const storedInterval = Number(localStorage.getItem("qqqHeatInterval"));
    if ([1, 5, 15, 30].includes(storedInterval)) interval = storedInterval;
    const storedMode = localStorage.getItem("qqqHeatMode");
    if (storedMode === "dots" || storedMode === "blocks") mode = storedMode;
    const storedSpotMode = localStorage.getItem("qqqHeatSpotMode");
    if (storedSpotMode === "line" || storedSpotMode === "candles") spotMode = storedSpotMode;
    const storedIntensity = Number(localStorage.getItem("qqqHeatIntensity"));
    if (Number.isFinite(storedIntensity) && storedIntensity >= 0 && storedIntensity <= 100) intensity = storedIntensity;
  } catch (_err) {}
  return {interval, mode, spotMode, intensity};
}

const heatPrefs = loadHeatPrefs();
const heatState = {
  ribbon: [], points: [], candles: [], summary: {}, session: null,
  interval: heatPrefs.interval, mode: heatPrefs.mode, spotMode: heatPrefs.spotMode, intensity: heatPrefs.intensity,
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

function nearestStrikeIndex(strikes, value) {
  if (!strikes.length || !Number.isFinite(value)) return null;
  let bestIdx = 0, bestDist = Infinity;
  for (let i = 0; i < strikes.length; i++) {
    const dist = Math.abs(strikes[i] - value);
    if (dist < bestDist) { bestDist = dist; bestIdx = i; }
  }
  return bestIdx;
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
  const dataCutoffUtc = sessionOpenForCutoff && !Number.isNaN(sessionOpenForCutoff.getTime())
    ? sessionOpenForCutoff
    : (cutoffCandidate && !Number.isNaN(cutoffCandidate.getTime()) ? cutoffCandidate : null);
  let ribbon = state.ribbon || [];
  let points = state.points || [];
  let candles = state.candles || [];
  if (dataCutoffUtc) {
    ribbon = ribbon.filter(s => s.time && new Date(s.time).getTime() >= dataCutoffUtc.getTime());
    points = points.filter(p => p.time && new Date(p.time).getTime() >= dataCutoffUtc.getTime());
    candles = candles.filter(c => c.t && new Date(c.t).getTime() >= dataCutoffUtc.getTime());
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
  const dataTimes = [...ribbon.map(s => s.time), ...points.map(p => p.time), ...candles.map(c => c.t)]
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
      wallLines.push({strike: l.strike, color: COLORS.cyan, lineColor: "rgba(34,211,238,0.45)", dash: i === 0 ? "solid" : "dash", label: callLabels[i] + " " + l.strike.toFixed(0), side: "right"});
    });
    const putLabels = ["PUT SUPPORT", "PUT WALL 2", "PUT WALL 3"];
    uniqueLevels(levels.filter(l => l.strike < spot && l.net_gex < 0), "put").forEach((l, i) => {
      wallLines.push({strike: l.strike, color: COLORS.orange, lineColor: "rgba(245,158,11,0.45)", dash: i === 0 ? "solid" : "dash", label: putLabels[i] + " " + l.strike.toFixed(0), side: "left"});
    });
  }
  if (summary?.gamma_flip) {
    wallLines.push({strike: Number(summary.gamma_flip), color: "#C084FC", lineColor: "rgba(192,132,252,0.4)", dash: "dot", label: "GAMMA FLIP " + Number(summary.gamma_flip).toFixed(2), side: "right"});
  }

  return {strikes, bucketCount, bucketStartUtc, bucketEndUtc, intervalMs, grid, maxAbs, wallLines, points, candles, spot};
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
        const rx = Math.max(1.1, cellW * 0.5 * (0.58 + 0.37 * t));
        const ry = Math.max(1.1, cellH * 0.5 * (0.09 + 0.27 * t));
        ctx.beginPath();
        ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
        ctx.fill();
      } else {
        const bw = Math.max(1, cellW * 0.95);
        const bh = Math.max(1, cellH * 0.35);
        ctx.fillRect(cx - bw / 2, cy - bh / 2, bw, bh);
      }
    }
  }

  const spotRows = (g.candles && g.candles.length)
    ? g.candles.map(c => ({time: c.t, open: Number(c.o), high: Number(c.h), low: Number(c.l), close: Number(c.c)}))
    : g.points.map(p => ({time: p.time, open: Number(p.spot), high: Number(p.spot), low: Number(p.spot), close: Number(p.spot)}));
  if (heatState.spotMode === "candles") {
    const bodyW = Math.min(7, Math.max(2.4, cellW * 0.42));
    for (const c of spotRows) {
      if (![c.open, c.high, c.low, c.close].every(Number.isFinite)) continue;
      const bPos = (new Date(c.time).getTime() - g.bucketStartUtc.getTime()) / g.intervalMs;
      const x = xToPx(bPos);
      const yOpenIdx = strikeContinuousIndex(g.strikes, c.open);
      const yHighIdx = strikeContinuousIndex(g.strikes, c.high);
      const yLowIdx = strikeContinuousIndex(g.strikes, c.low);
      const yCloseIdx = strikeContinuousIndex(g.strikes, c.close);
      if ([yOpenIdx, yHighIdx, yLowIdx, yCloseIdx].some(v => v === null)) continue;
      const yOpen = yToPx(yOpenIdx);
      const yHigh = yToPx(yHighIdx);
      const yLow = yToPx(yLowIdx);
      const yClose = yToPx(yCloseIdx);
      const color = c.close >= c.open ? COLORS.cyan : COLORS.orange;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      ctx.moveTo(x, yHigh);
      ctx.lineTo(x, yLow);
      ctx.stroke();
      const bodyTop = Math.min(yOpen, yClose);
      const bodyH = Math.max(1.2, Math.abs(yClose - yOpen));
      ctx.fillStyle = "#000";
      ctx.fillRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
      ctx.strokeRect(x - bodyW / 2, bodyTop, bodyW, bodyH);
    }
  } else {
    const spotPts = [];
    for (const c of spotRows) {
      const spotV = Number(c.close);
      if (!Number.isFinite(spotV)) continue;
      const bPos = (new Date(c.time).getTime() - g.bucketStartUtc.getTime()) / g.intervalMs;
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
  }

  for (const w of g.wallLines) {
    // Snap to the actual strike row (same row the GEX blocks/dots are drawn
    // on), not an interpolated price level, so the wall line sits exactly on
    // that row like quantdecay's heat tracker.
    const sIdx = nearestStrikeIndex(g.strikes, w.strike);
    if (sIdx === null) continue;
    const y = yToPx(sIdx + 0.5);
    if (y < margin.t - 4 || y > margin.t + plotH + 4) continue;
    ctx.strokeStyle = w.lineColor || w.color;
    ctx.lineWidth = w.dash === "solid" ? 3.5 : 2.4;
    ctx.setLineDash(w.dash === "solid" ? [] : w.dash === "dot" ? [2, 3] : [8, 5]);
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
    const sIdx = nearestStrikeIndex(g.strikes, w.strike);
    if (sIdx === null) continue;
    const y = yToPx(sIdx + 0.5);
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
  const spotBtn = document.getElementById("heatSpotToggle");
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
  if (spotBtn) {
    const syncSpotLabel = () => {
      spotBtn.textContent = heatState.spotMode === "candles" ? "Spot Candles" : "Spot Line";
      spotBtn.classList.toggle("active", heatState.spotMode === "candles");
    };
    syncSpotLabel();
    spotBtn.addEventListener("click", () => {
      heatState.spotMode = heatState.spotMode === "candles" ? "line" : "candles";
      try { localStorage.setItem("qqqHeatSpotMode", heatState.spotMode); } catch (_err) {}
      syncSpotLabel();
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

const EXPOSURE_PANEL_STORAGE_KEY = { gex: "qqqExposurePanelGex", dex: "qqqExposurePanelDex" };

function initExposureTabs() {
  document.querySelectorAll(".exposure-tabs").forEach((group) => {
    const panelId = group.getAttribute("data-panel");
    if (!panelId) return;
    const storageKey = EXPOSURE_PANEL_STORAGE_KEY[panelId];
    let saved = null;
    try { saved = storageKey ? localStorage.getItem(storageKey) : null; } catch (_err) {}
    if (saved && EXPOSURE_CONFIG[saved]) {
      exposurePanelMetric[panelId] = saved;
    }
    const buttons = Array.from(group.querySelectorAll(".exposure-tab"));
    const syncActive = () => {
      buttons.forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-metric") === exposurePanelMetric[panelId]);
      });
    };
    syncActive();
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const metric = btn.getAttribute("data-metric");
        if (!metric || !EXPOSURE_CONFIG[metric] || metric === exposurePanelMetric[panelId]) return;
        exposurePanelMetric[panelId] = metric;
        try { if (storageKey) localStorage.setItem(storageKey, metric); } catch (_err) {}
        syncActive();
        const stateKey = panelId === "gex" ? "exposureGex" : "exposureDex";
        if (panelDayState[stateKey] === "live") {
          drawExposure(panelId, latestState?.by_strike || [], metric, latestState?.latest_summary || {});
        } else if (panelPayload[stateKey]) {
          drawExposure(panelId, panelPayload[stateKey].by_strike || [], metric, panelPayload[stateKey].latest_summary || {});
        }
      });
    });
  });
}

function drawGexRibbon(ribbon, points, summary, session, candles = []) {
  heatState.ribbon = ribbon || [];
  heatState.points = points || [];
  heatState.candles = candles || [];
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
initExposureTabs();

function initSkewControls() {
  const expirySel = document.getElementById("skewExpiry");
  const seriesBtn = document.getElementById("skewSeriesBtn");
  const seriesMenu = document.getElementById("skewSeriesMenu");
  if (expirySel) {
    expirySel.addEventListener("change", () => {
      skewExpirySelected = expirySel.value;
      if (latestState) drawAll(latestState);
    });
  }
  const syncSeriesLabel = () => {
    const active = ["call", "put", "iv"]
      .filter(key => skewSeriesVisible[key])
      .map(key => key === "call" ? "Calls" : key === "put" ? "Puts" : "IV");
    if (seriesBtn) seriesBtn.textContent = active.length ? active.join(" · ") : "None";
  };
  syncSeriesLabel();
  if (seriesBtn && seriesMenu) {
    seriesBtn.addEventListener("click", (evt) => {
      evt.stopPropagation();
      seriesMenu.hidden = !seriesMenu.hidden;
    });
    seriesMenu.querySelectorAll("input[type=checkbox]").forEach(box => {
      box.addEventListener("change", () => {
        skewSeriesVisible[box.dataset.series] = box.checked;
        syncSeriesLabel();
        if (latestState) drawAll(latestState);
      });
    });
    document.addEventListener("click", (evt) => {
      if (!seriesMenu.hidden && !seriesMenu.contains(evt.target) && evt.target !== seriesBtn) {
        seriesMenu.hidden = true;
      }
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") seriesMenu.hidden = true;
    });
  }
}
initSkewControls();

function initLevelsCopy(exportId, copyBtnId) {
  const btn = document.getElementById(copyBtnId);
  const levelsExport = document.getElementById(exportId);
  if (!btn || !levelsExport) return;
  let resetTimer = null;
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(levelsExport.textContent || "");
      btn.textContent = "Copied!";
      btn.classList.add("copied");
    } catch (err) {
      btn.textContent = "Failed";
    }
    if (resetTimer) clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      btn.textContent = "Copy";
      btn.classList.remove("copied");
    }, 1200);
  });
}
initLevelsCopy("levelsExport", "levelsCopyBtn");
initLevelsCopy("levelsExportB", "levelsCopyBtnB");

function renderLevelsRow(exportId, tagId, summary, locked, fallbackTicker) {
  const levelsExport = document.getElementById(exportId);
  if (levelsExport) {
    levelsExport.textContent = summary
      ? buildLevelsLine(summary)
      : "$" + String(fallbackTicker).toUpperCase() + ": waiting for first snapshot...";
  }
  const levelsTag = document.getElementById(tagId);
  if (levelsTag) {
    levelsTag.textContent = locked ? "EOD" : "LIVE";
    levelsTag.classList.toggle("locked", !!locked);
  }
}

function nyDateISO(date = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit"
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

const panelDayState = {
  levels: "live", ivRank: "live", oiIv: "live", oi: "live", exposureGex: "live", exposureDex: "live"
};
const panelPayload = {
  levels: null, ivRank: null, oiIv: null, oi: null, exposureGex: null, exposureDex: null
};
const daySnapshotCache = new Map();

async function fetchDaySnapshot(ticker, dayId) {
  const key = ticker + "::" + dayId;
  if (!daySnapshotCache.has(key)) {
    daySnapshotCache.set(key, fetch("/api/snapshot?id=" + encodeURIComponent(dayId) + "&ticker=" + encodeURIComponent(ticker) + "&ts=" + Date.now())
      .then(res => res.json())
      .then(payload => {
        if (payload.error) throw new Error(payload.error);
        return payload;
      })
      .catch(err => {
        daySnapshotCache.delete(key);
        throw err;
      }));
  }
  return daySnapshotCache.get(key);
}

function drawLevelsPanel() {
  if (panelDayState.levels === "live") {
    if (!latestState) return;
    renderLevelsRow("levelsExport", "levelsTag", latestState.levels_summary, latestState.levels_locked, latestState.latest_summary?.ticker || "QQQ");
    renderLevelsRow("levelsExportB", "levelsTagB", latestState.levels_summary_secondary, latestState.levels_locked_secondary, latestState.secondary_ticker || "NDX");
  } else if (panelPayload.levels) {
    const { primary, secondary } = panelPayload.levels;
    renderLevelsRow("levelsExport", "levelsTag", primary.levels_summary, true, primary.latest_summary?.ticker || "QQQ");
    renderLevelsRow("levelsExportB", "levelsTagB", secondary.levels_summary, true, secondary.latest_summary?.ticker || (latestState?.secondary_ticker || "NDX"));
  }
}

function drawIvRankPanel() {
  if (panelDayState.ivRank === "live") {
    if (!latestState) return;
    drawIvRank(latestState.history || [], latestState.latest_summary || {});
  } else if (panelPayload.ivRank) {
    drawIvRank(panelPayload.ivRank.history || [], panelPayload.ivRank.latest_summary || {});
  }
}

function drawOiIvPanel() {
  if (panelDayState.oiIv === "live") {
    if (!latestState) return;
    drawOiIv(latestState.by_strike || [], latestState.latest_summary || {});
  } else if (panelPayload.oiIv) {
    drawOiIv(panelPayload.oiIv.by_strike || [], panelPayload.oiIv.latest_summary || {});
  }
}

function drawOiPanel() {
  if (panelDayState.oi === "live") {
    if (!latestState) return;
    drawOi(latestState.by_strike || [], latestState.latest_summary || {});
  } else if (panelPayload.oi) {
    drawOi(panelPayload.oi.by_strike || [], panelPayload.oi.latest_summary || {});
  }
}

function drawExposureGexPanel() {
  if (panelDayState.exposureGex === "live") {
    if (!latestState) return;
    drawExposure("gex", latestState.by_strike || [], exposurePanelMetric.gex, latestState.latest_summary || {});
  } else if (panelPayload.exposureGex) {
    drawExposure("gex", panelPayload.exposureGex.by_strike || [], exposurePanelMetric.gex, panelPayload.exposureGex.latest_summary || {});
  }
}

function drawExposureDexPanel() {
  if (panelDayState.exposureDex === "live") {
    if (!latestState) return;
    drawExposure("dex", latestState.by_strike || [], exposurePanelMetric.dex, latestState.latest_summary || {});
  } else if (panelPayload.exposureDex) {
    drawExposure("dex", panelPayload.exposureDex.by_strike || [], exposurePanelMetric.dex, panelPayload.exposureDex.latest_summary || {});
  }
}

function redrawPinnablePanels() {
  drawLevelsPanel();
  drawIvRankPanel();
  drawOiIvPanel();
  drawOiPanel();
  drawExposureGexPanel();
  drawExposureDexPanel();
}

function bindPanelDatePicker(key, inputId, onPick) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.value = nyDateISO();
  input.addEventListener("change", async () => {
    const value = input.value;
    if (!value || value === nyDateISO()) {
      input.value = nyDateISO();
      panelDayState[key] = "live";
      panelPayload[key] = null;
      redrawPinnablePanels();
      return;
    }
    try {
      await onPick(value);
      panelDayState[key] = value;
      redrawPinnablePanels();
    } catch (err) {
      document.getElementById("status").textContent = "History error: " + err.message;
    }
  });
}

function initPanelDatePickers() {
  bindPanelDatePicker("levels", "levelsDate", async value => {
    const dayId = "day:" + value;
    const primaryTicker = latestState?.latest_summary?.ticker || "QQQ";
    const secondaryTicker = latestState?.secondary_ticker || "NDX";
    const [primary, secondary] = await Promise.all([
      fetchDaySnapshot(primaryTicker, dayId),
      fetchDaySnapshot(secondaryTicker, dayId),
    ]);
    panelPayload.levels = { primary, secondary };
  });
  bindPanelDatePicker("ivRank", "ivRankDate", async value => {
    panelPayload.ivRank = await fetchDaySnapshot(latestState?.latest_summary?.ticker || "QQQ", "day:" + value);
  });
  bindPanelDatePicker("oiIv", "oiIvDate", async value => {
    panelPayload.oiIv = await fetchDaySnapshot(latestState?.latest_summary?.ticker || "QQQ", "day:" + value);
  });
  bindPanelDatePicker("oi", "oiDate", async value => {
    panelPayload.oi = await fetchDaySnapshot(latestState?.latest_summary?.ticker || "QQQ", "day:" + value);
  });
  bindPanelDatePicker("exposureGex", "exposureGexDate", async value => {
    panelPayload.exposureGex = await fetchDaySnapshot(latestState?.latest_summary?.ticker || "QQQ", "day:" + value);
  });
  bindPanelDatePicker("exposureDex", "exposureDexDate", async value => {
    panelPayload.exposureDex = await fetchDaySnapshot(latestState?.latest_summary?.ticker || "QQQ", "day:" + value);
  });
}
initPanelDatePickers();

function drawAll(state) {
  lastDrawState = state;
  drawFlow(state.points || [], state.session || null, state.candles || []);
  drawFlowTracker(state.points || [], state.session || null);
  drawSkew(state.skew_by_strike || [], state.skew_summary || {}, state.skew_tenors || []);
  const gexSession = state.session ? {...state.session, history_snapshot_id: state.history_snapshot_id || null} : state.session;
  drawGexRibbon(state.gex_ribbon || [], state.points || [], state.latest_summary || {}, gexSession, state.candles || []);
  redrawPinnablePanels();
}

async function update() {
  const res = await fetch("/api/state?ts=" + Date.now());
  const state = await res.json();
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
  const candles = state.candles || [];
  const latestPoint = points.length ? points[points.length - 1].time : "";
  const latestRibbon = ribbon.length ? ribbon[ribbon.length - 1].time : "";
  const latestCandle = candles.length ? candles[candles.length - 1].t : "";
  const chartKey = [
    state.latest_summary?.snapshot_utc || "",
    state.levels_summary?.snapshot_utc || "",
    points.length,
    latestPoint,
    ribbon.length,
    latestRibbon,
    candles.length,
    latestCandle,
    state.by_strike?.length || 0,
  ].join("|");
  if (chartKey === lastChartKey) return;
  lastChartKey = chartKey;
  drawAll(state);
}

applyAccentColors();
bindColorControls("callColor", "callHex", "cyan");
bindColorControls("putColor", "putHex", "orange");
["flowInterval", "flowMoneyness", "flowExpiry", "trackerInterval", "trackerMoneyness", "trackerExpiry", "trackerMode"].forEach(id => {
  document.getElementById(id)?.addEventListener("change", () => {
    resetChartLocks();
    trackerState.sessionKey = "";
    if (latestState) drawAll(latestState);
  });
});
document.getElementById("flowResetZoom")?.addEventListener("click", () => {
  resetChartLocks();
  flowState.viewX = [0, flowState.bucketCount || 1];
  if (latestState) drawAll(latestState);
});
document.getElementById("trackerResetZoom")?.addEventListener("click", () => {
  trackerState.sessionKey = "";
  trackerState.viewX = [0, trackerState.bucketCount || 1];
  if (latestState) drawAll(latestState);
});
window.addEventListener("resize", () => {
  if (latestState) drawAll(latestState);
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
        self.session_locked: bool = False
        self.history: list[dict] = []
        self.by_strike: list[dict] = []
        self.skew_tenors: list[dict] = []
        self.skew_summary: dict | None = None
        self.skew_by_strike: list[dict] = []
        self.latest_summary: dict | None = None
        self.levels_summary: dict | None = None
        self.levels_locked: bool = False
        self.levels_summary_secondary: dict | None = None
        self.levels_locked_secondary: bool = False
        self.secondary_ticker: str = ""
        self.candles: list[dict] = []
        self.candles_error: str | None = None
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
                "session_locked": self.session_locked,
                "history": self.history,
                "by_strike": self.by_strike,
                "skew_tenors": self.skew_tenors,
                "skew_summary": self.skew_summary,
                "skew_by_strike": self.skew_by_strike,
                "latest_summary": self.latest_summary,
                "levels_summary": self.levels_summary,
                "levels_locked": self.levels_locked,
                "levels_summary_secondary": self.levels_summary_secondary,
                "levels_locked_secondary": self.levels_locked_secondary,
                "secondary_ticker": self.secondary_ticker,
                "candles": self.candles,
                "candles_error": self.candles_error,
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


def skew_tenors_payload(ticker: str, spot: float, effective_day: str) -> list[dict]:
    """Multi-expiry skew curves for the live Volatility Skew panel, mirroring
    render_gex_interactive.build_volatility_skew_chart's multi-tenor branch."""
    try:
        raw, capture_ts = load_multi_tenor_skew(DATA_ROOT, ticker)
        if raw.empty:
            return []
        # DTE must be measured from when this multi-tenor snapshot was actually
        # captured, not from the single-expiry poll's effective_snapshot_date -
        # that date reflects Yahoo's last-trade staleness (e.g. pinned to Friday
        # over a weekend/pre-open) while the multi-tenor expiries are always
        # real calendar dates, so using the stale day here silently zeroed out
        # every tenor.
        dte_reference_day = str(capture_ts)[:10] if capture_ts else effective_day
        tenors = build_tenor_curves(raw, spot, dte_reference_day)
    except Exception:
        return []
    payload = []
    for tenor in tenors:
        payload.append(
            {
                "expiry": tenor["expiry"],
                "dte": tenor["dte"],
                "atm_iv": clean_value(tenor["atm_iv"]),
                "color": tenor["color"],
                "call": {
                    "strike": [clean_value(v) for v in tenor["call"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["call"]["iv"].tolist()],
                },
                "put": {
                    "strike": [clean_value(v) for v in tenor["put"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["put"]["iv"].tolist()],
                },
                "iv": {
                    "strike": [clean_value(v) for v in tenor["iv"]["strike"].tolist()],
                    "iv": [clean_value(v) for v in tenor["iv"]["iv"].tolist()],
                },
            }
        )
    return payload


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
    parser.add_argument(
        "--secondary-ticker",
        default="NDX",
        help="Second ticker whose levels are shown in the Levels Export panel's second row. "
        "Empty string disables the second row.",
    )
    parser.add_argument(
        "--secondary-futures-ticker",
        default="NQ1!",
        help="Futures ticker whose live price is used to compute the basis added to the "
        "secondary ticker's exported levels (e.g. NQ1! for NDX), correcting for the cash/"
        "futures spread when those levels are pasted onto a futures chart. "
        "Empty string disables basis adjustment.",
    )
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
    parser.add_argument(
        "--no-github-pull",
        action="store_true",
        help="Skip the startup git pull that syncs GitHub Actions history back to this machine.",
    )
    return parser.parse_args()


def pull_github_updates_on_startup(enabled: bool = True) -> None:
    if not enabled:
        print("GitHub sync skipped (--no-github-pull).", flush=True)
        return
    if not (PROJECT_ROOT / ".git").exists():
        print("GitHub sync skipped: this folder is not a git repo.", flush=True)
        return
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except Exception as exc:
        print(f"GitHub sync warning: could not run git pull ({exc}).", flush=True)
        return
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode == 0:
        first_line = output.splitlines()[0] if output else "Already up to date."
        print(f"GitHub sync ok: {first_line}", flush=True)
    else:
        print("GitHub sync warning: git pull --ff-only failed; continuing with local data.", flush=True)
        if output:
            print(output, flush=True)


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


def volume_totals(rows: pd.DataFrame | list[dict]) -> tuple[float, float]:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return 0.0, 0.0
    call_volume = pd.to_numeric(frame.get("call_volume", 0), errors="coerce").fillna(0).sum()
    put_volume = pd.to_numeric(frame.get("put_volume", 0), errors="coerce").fillna(0).sum()
    return float(call_volume), float(put_volume)


def chain_payload(rows: list[dict]) -> list[dict]:
    """Compact per-strike call/put IV+volume for client-side moneyness filtering."""
    out: list[dict] = []
    for row in rows:
        try:
            strike = float(row.get("strike"))
        except (TypeError, ValueError):
            continue
        if not np.isfinite(strike):
            continue

        def pct(key: str) -> float | None:
            try:
                val = float(row.get(key))
            except (TypeError, ValueError):
                return None
            return val * 100 if np.isfinite(val) else None

        def vol(key: str) -> float:
            try:
                val = float(row.get(key))
            except (TypeError, ValueError):
                return 0.0
            return val if np.isfinite(val) else 0.0

        def price(key: str) -> float | None:
            try:
                val = float(row.get(key))
            except (TypeError, ValueError):
                return None
            return val if np.isfinite(val) and val > 0 else None

        out.append({
            "k": strike, "ci": pct("call_iv"), "pi": pct("put_iv"),
            "cv": vol("call_volume"), "pv": vol("put_volume"),
            "cm": price("call_mid"), "pm": price("put_mid"),
        })
    return out


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
                atm_pct = nearest_atm_iv(by_strike, float(spot))
                if atm_pct is not None:
                    iv_value = atm_pct / 100
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
        # A min-max rank needs at least 2 points to define a range at all.
        # It's noisy with few sessions (matches quantdecay's own behavior,
        # which doesn't gate on a minimum either) but still meaningful.
        if len(window) < 2:
            row["iv_rank_pct"] = None
            continue
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
    Files are grouped by New York trading date, not their local output folder,
    because an overnight VN session can span more than one local folder."""
    open_ts = pd.Timestamp(session["collection_start_utc"])
    close_ts = pd.Timestamp(session["market_close_utc"])
    points: list[dict] = []
    ribbon: list[dict] = []
    seen_times: set[str] = set()
    for summary_history_path in history_summary_paths(ticker):
        try:
            summaries = pd.read_parquet(summary_history_path)
        except Exception:
            continue
        if summaries.empty or "ticker" not in summaries or "snapshot_utc" not in summaries:
            continue
        by_strike_path = history_by_strike_path(summary_history_path)
        if not by_strike_path.exists():
            continue
        try:
            by_strike_rows = pd.read_parquet(by_strike_path)
        except Exception:
            continue
        summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
        summaries = summaries[
            summaries["_snapshot_ts"].notna()
            & (summaries["_snapshot_ts"] >= open_ts)
            & (summaries["_snapshot_ts"] <= close_ts)
            & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
        ].sort_values("snapshot_utc")
        for row in summaries.to_dict(orient="records"):
            summary = summary_from_history_row(row)
            time_key = str(summary.get("snapshot_utc") or "")
            if not time_key or time_key in seen_times:
                continue
            try:
                records, gex_snapshot = rows_for_history_snapshot(
                    summary_history_path, summary, window, source_rows=by_strike_rows
                )
            except Exception:
                continue
            seen_times.add(time_key)
            call_volume, put_volume = volume_totals(records)
            points.append({
                "time": time_key,
                "atm_iv": clean_value(nearest_atm_iv(pd.DataFrame(records), float(summary["spot"]))),
                "avg_iv": clean_value(float(summary.get("avg_iv") or np.nan) * 100),
                "spot": clean_value(float(summary["spot"])),
                "call_volume": clean_value(call_volume),
                "put_volume": clean_value(put_volume),
                "chain": chain_payload(records),
                "expiry": clean_value(summary.get("expiry")),
            })
            ribbon.append(gex_snapshot)
    if points:
        points.sort(key=lambda point: point["time"])
        ribbon.sort(key=lambda snap: snap["time"])
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
        if pd.isna(snapshot) or snapshot < open_ts or snapshot > close_ts:
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
        chain: list[dict] = []
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            call_volume, put_volume = volume_totals(chart_rows)
            chain = chain_payload(chart_rows.to_dict(orient="records"))
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
            "chain": chain,
            "expiry": clean_value(summary.get("expiry")),
        })
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def seed_prev_day_eod_summary(ticker: str, today_trading_date: str) -> dict | None:
    """Return the last recorded snapshot strictly before today's trading
    date - the previous completed session's actual closing chain.

    Levels Export is meant to always show a true EOD reference: on
    2026-08-27 it shows the 2026-08-26 close, all day, regardless of time -
    never an early poll of *today* recomputed with Yahoo's live, moving
    premarket spot. Falls back through JSON summaries on disk if no
    Parquet history exists yet for the ticker.
    """
    latest_history = latest_history_snapshot(ticker)
    if latest_history is not None:
        summary_history_path, _summary = latest_history
        summaries = pd.read_parquet(summary_history_path)
        summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
        summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
        summaries = summaries[summaries["_snapshot_ts"].notna()].copy()
        summaries["_ny_date"] = summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str)
        summaries = summaries[summaries["_ny_date"] < today_trading_date].sort_values("snapshot_utc")
        if not summaries.empty:
            return summary_from_history_row(summaries.iloc[-1].to_dict())
    candidates: list[tuple[pd.Timestamp, dict]] = []
    for path in sorted(DATA_ROOT.glob(f"*/{ticker.upper()}_*_*_summary.json")):
        if len(path.stem.split("_")) != 4:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snapshot = pd.to_datetime(summary.get("snapshot_utc"), errors="coerce", utc=True)
        if pd.isna(snapshot):
            continue
        if snapshot.tz_convert(NY_TZ).date().isoformat() >= today_trading_date:
            continue
        candidates.append((snapshot, summary))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def seed_locked_snapshot(
    ticker: str, session: dict, lock_ts: pd.Timestamp, window: float
) -> tuple[dict | None, list[dict], list[dict]]:
    """Return (summary, by_strike rows, iv-rank history) for the last snapshot
    of today strictly before lock_ts.

    DEX/GEX/VEX/CHEX, OI by Strike, OIxIV by Strike and IV Rank must show
    true EOD data until 09:00 NY, and Volatility Skew until 09:30 NY - but
    Yahoo returns a live `preMarketPrice` for any poll made before the open,
    so a naive "latest poll" would already recompute exposure/skew off a
    moving premarket spot before the intended cutoff. Seeding from the last
    pre-cutoff snapshot on disk avoids that leak. Returns (None, [], []) if
    no such snapshot exists yet today (e.g. server started before any poll
    has landed) - the caller keeps its empty/placeholder state in that case.
    """
    latest_history = latest_history_snapshot(ticker)
    if latest_history is None:
        return None, [], []
    summary_history_path, _summary = latest_history
    try:
        summaries = pd.read_parquet(summary_history_path)
    except Exception:
        return None, [], []
    summaries = summaries[summaries["ticker"].astype(str).str.upper() == ticker.upper()].copy()
    summaries["_snapshot_ts"] = pd.to_datetime(summaries["snapshot_utc"], errors="coerce", utc=True)
    summaries = summaries[
        summaries["_snapshot_ts"].notna()
        & (summaries["_snapshot_ts"] < lock_ts)
        & (summaries["_snapshot_ts"].dt.tz_convert(NY_TZ).dt.date.astype(str) == session["trading_date"])
    ].sort_values("snapshot_utc")
    if summaries.empty:
        return None, [], []
    summary = summary_from_history_row(summaries.iloc[-1].to_dict())
    try:
        rows, _gex_snapshot = rows_for_history_snapshot(summary_history_path, summary, window)
    except Exception:
        rows = []
    return summary, rows, load_history(ticker)


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
        "net_vex",
        "call_vex",
        "put_vex",
        "net_chex",
        "call_chex",
        "put_chex",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "iv",
        "call_iv",
        "put_iv",
        "call_mid",
        "put_mid",
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
    point["chain"] = chain_payload(records)
    point["expiry"] = clean_value(summary.get("expiry"))
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
        "net_vex",
        "call_vex",
        "put_vex",
        "net_chex",
        "call_chex",
        "put_chex",
        "call_oi",
        "put_oi",
        "call_volume",
        "put_volume",
        "iv",
        "call_iv",
        "put_iv",
        "call_mid",
        "put_mid",
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
    point["chain"] = chain_payload(records)
    point["expiry"] = clean_value(summary.get("expiry"))
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
        chain: list[dict] = []
        try:
            by_strike_path = path.with_name(path.name.replace("_summary.json", "_by_strike.parquet"))
            by_strike = pd.read_parquet(by_strike_path)
            atm_iv = nearest_atm_iv(by_strike, spot)
            chart_rows = by_strike[(by_strike["strike"] >= spot - window) & (by_strike["strike"] <= spot + window)]
            if chart_rows.empty:
                chart_rows = by_strike.sort_values("abs_net_gex", ascending=False).head(40)
            call_volume, put_volume = volume_totals(chart_rows)
            chain = chain_payload(chart_rows.to_dict(orient="records"))
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
                "chain": chain,
                "expiry": clean_value(summary.get("expiry")),
            }
        )
    points.sort(key=lambda p: p["time"])
    ribbon.sort(key=lambda r: r["time"])
    return points, ribbon


def filter_replay_series(points: list[dict], ribbon: list[dict], session: dict, selected_utc: str) -> tuple[list[dict], list[dict]]:
    start_ts = pd.Timestamp(session["collection_start_utc"])
    close_ts = pd.Timestamp(session["market_close_utc"])
    end_ts = pd.to_datetime(selected_utc, errors="coerce", utc=True)
    if pd.isna(end_ts):
        return points, ribbon
    end_ts = min(end_ts, close_ts)

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
                "chain": chain_payload(records),
                "expiry": clean_value(summary.get("expiry")),
            }
        )
        ribbon.append(gex_snapshot)
    return points, ribbon


def candles_for_session(ticker: str, session: dict) -> tuple[list[dict], str | None]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return [], "Alpaca API key not configured"
    try:
        return fetch_alpaca_bars(
            ticker,
            session["market_open_utc"],
            end_iso=session.get("market_close_utc"),
        ), None
    except Exception as exc:
        return [], str(exc)


def load_snapshot_state(snapshot_id: str, ticker: str, window: float) -> dict:
    if snapshot_id.startswith("day:"):
        snapshot_id = latest_snapshot_id_for_trading_day(snapshot_id, ticker)
    if snapshot_id.startswith("history:"):
        summary_history_path, snapshot_utc = parse_history_snapshot_id(snapshot_id)
        summary, point, rows, history, gex_snapshot = chart_payload_from_history(summary_history_path, snapshot_utc, ticker, window)
        history = [row for row in history if str(row.get("snapshot_utc") or "") <= str(summary.get("snapshot_utc") or "")]
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
        session = session_from_summary(summary)
        candles, candles_error = candles_for_session(ticker, session)
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
            "session": session,
            "candles": candles,
            "candles_error": candles_error,
            "history_snapshot_id": snapshot_id,
            "replay_snapshots": replay_snapshots,
        }
    summary_path = summary_path_from_id(snapshot_id)
    summary, point, rows, history, gex_snapshot = chart_payload_from_summary_path(summary_path, ticker, window)
    history = [row for row in history if str(row.get("snapshot_utc") or "") <= str(summary.get("snapshot_utc") or "")]
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
    session = session_from_summary(summary)
    candles, candles_error = candles_for_session(ticker, session)
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
        "session": session,
        "candles": candles,
        "candles_error": candles_error,
        "history_snapshot_id": snapshot_id,
        "replay_snapshots": replay_snapshots,
    }


def load_latest(ticker: str, window: float) -> tuple[dict, dict, list[dict], list[dict], dict]:
    latest_history = latest_history_snapshot(ticker)
    if latest_history is not None:
        summary_history_path, summary = latest_history
        return chart_payload_from_history(summary_history_path, summary["snapshot_utc"], ticker, window)
    return chart_payload_from_summary_path(latest_summary_path(ticker), ticker, window)


# Multi-expiry Volatility Skew is heavier than the single-expiry flow pull.
# With the default 60s interval, this refreshes skew roughly every 5 minutes.
TENOR_REFRESH_EVERY_N_PULLS = 5
TENOR_REFRESH_HORIZON_DAYS = 10


def run_snapshot(args: argparse.Namespace, *, fetch_tenor: bool = False) -> subprocess.CompletedProcess:
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
    if fetch_tenor:
        cmd += ["--all-expiries", "--expiry-horizon-days", str(TENOR_REFRESH_HORIZON_DAYS)]
    elif args.expiry:
        cmd += ["--expiry", args.expiry]
    return subprocess.run(cmd, cwd=PROJECT_ROOT, check=False, text=True, capture_output=True)


def collector(args: argparse.Namespace, state: LiveState) -> None:
    deadline = None
    if args.duration_minutes is not None:
        deadline = time.monotonic() + max(0, args.duration_minutes) * 60
    next_run = time.monotonic()
    pull_count = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            break
        with state.lock:
            already_locked = state.session_locked
        if already_locked:
            time.sleep(60)
            continue
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_fetch_dt = datetime.now() + timedelta(seconds=args.interval_seconds)
        with state.lock:
            state.next_fetch = next_fetch_dt.isoformat()

        pull_count += 1
        fetch_tenor = pull_count % TENOR_REFRESH_EVERY_N_PULLS == 0
        result = run_snapshot(args, fetch_tenor=fetch_tenor)
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
                    market_close = pd.Timestamp(state.session["market_close_utc"])
                    if pd.notna(point_ts) and point_ts < market_close:
                        if collect_start <= point_ts:
                            if not any(p.get("time") == point.get("time") for p in state.points):
                                state.points.append(point)
                            if not any(r.get("time") == gex_snapshot.get("time") for r in state.gex_ribbon):
                                state.gex_ribbon.append(gex_snapshot)
                    elif pd.notna(point_ts) and not state.session_locked:
                        # First poll at/after 16:00 NY close: this is the
                        # closing-chain snapshot, so collapse the intraday
                        # series down to just this EOD point and stop
                        # collecting further data for the rest of the day.
                        state.points = [point]
                        state.gex_ribbon = [gex_snapshot]
                        state.session_locked = True
                    # Levels Export always shows the previous completed
                    # trading day's actual EOD close (seeded once at startup
                    # in main()), never today's live/recomputed chain. Only
                    # 1D Min/Max refresh live on top of that frozen base.
                    if state.levels_summary is not None:
                        day_low, day_high = fetch_day_high_low(args.ticker)
                        if day_low is not None:
                            state.levels_summary["one_day_min"] = day_low
                        if day_high is not None:
                            state.levels_summary["one_day_max"] = day_high
                    # DEX/GEX/VEX/CHEX, OI by Strike, OIxIV by Strike and IV
                    # Rank must show true EOD data until 09:00 NY (Yahoo
                    # returns a live premarket spot before then, which would
                    # otherwise leak into a "frozen" snapshot). Skew has its
                    # own, later, 09:30 NY cutoff and its own state fields so
                    # it can go live independently of the exposure group.
                    market_open = pd.Timestamp(state.session["market_open_utc"])
                    if pd.notna(point_ts) and point_ts >= collect_start:
                        state.latest_summary = summary
                        state.by_strike = rows
                        state.history = history
                    if pd.notna(point_ts) and point_ts >= market_open:
                        state.skew_summary = summary
                        state.skew_by_strike = rows
                        state.skew_tenors = skew_tenors_payload(
                            args.ticker,
                            float(summary.get("spot") or 0.0),
                            summary.get("effective_snapshot_date") or summary.get("requested_snapshot_date") or "",
                        )
                    state.latest_error = None
                    state.successes += 1
                except Exception as exc:
                    state.failures += 1
                    state.latest_error = str(exc)
        next_run += args.interval_seconds
    with state.lock:
        state.running = False
        state.next_fetch = None


def fetch_futures_basis(futures_ticker: str, cash_spot: float) -> float | None:
    """Live futures price minus cash spot, for adjusting cash-index-derived
    levels so they line up on a futures chart (e.g. NDX levels on NQ1!)."""
    if not futures_ticker:
        return None
    try:
        yf = yahoo.import_yfinance()
        futures_spot = yahoo.get_spot(yf.Ticker(yahoo._yahoo_symbol(futures_ticker)))
        return float(futures_spot) - float(cash_spot)
    except Exception:
        return None


def fetch_day_high_low(ticker: str) -> tuple[float | None, float | None]:
    """Live 1D low/high so far from Yahoo Finance 1-minute bars, for the
    Levels Export panel's "1D Min"/"1D Max" fields."""
    try:
        yf = yahoo.import_yfinance()
        ticker_obj = yf.Ticker(yahoo._yahoo_symbol(ticker))
        return yahoo.get_day_high_low(ticker_obj)
    except Exception:
        return None, None


def levels_collector(
    ticker: str,
    base_args: argparse.Namespace,
    state: LiveState,
    futures_ticker: str = "",
) -> None:
    """Lightweight sibling of collector(): keeps polling `ticker` so its local
    Parquet history keeps growing (needed so a *future* day's Levels Export
    can seed from today's actual close), while the displayed
    levels_summary_secondary stays frozen at the previous session's EOD
    (seeded once in main()) with only its live 1D Min/Max and NQ1! futures
    basis refreshed in place on top of that frozen base."""
    secondary_args = copy.copy(base_args)
    secondary_args.ticker = ticker
    secondary_args.expiry = None
    next_run = time.monotonic()
    while True:
        with state.lock:
            if not state.running:
                break
        delay = next_run - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        run_snapshot(secondary_args, fetch_tenor=False)
        with state.lock:
            summary = state.levels_summary_secondary
        if summary is not None:
            day_low, day_high = fetch_day_high_low(ticker)
            if day_low is not None:
                summary["one_day_min"] = day_low
            if day_high is not None:
                summary["one_day_max"] = day_high
            if futures_ticker and summary.get("spot") is not None:
                basis = fetch_futures_basis(futures_ticker, summary["spot"])
                if basis is not None:
                    summary["futures_basis"] = basis
                    summary["futures_ticker"] = futures_ticker
        next_run += base_args.interval_seconds


def fetch_alpaca_bars(ticker: str, start_iso: str, timeframe: str = "1Min", end_iso: str | None = None) -> list[dict]:
    params = {
        "timeframe": timeframe,
        "start": start_iso,
        "limit": 1000,
        "feed": "iex",
        "adjustment": "raw",
    }
    if end_iso:
        params["end"] = end_iso
    resp = requests.get(
        ALPACA_DATA_URL.format(symbol=ticker),
        headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        },
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    bars = resp.json().get("bars") or []
    return [
        {
            "t": bar["t"],
            "o": float(bar["o"]),
            "h": float(bar["h"]),
            "l": float(bar["l"]),
            "c": float(bar["c"]),
            "v": float(bar["v"]),
        }
        for bar in bars
    ]


def alpaca_candles_collector(ticker: str, state: LiveState) -> None:
    """Independent poller for the Alpaca candlestick panel. Never crashes the
    process: missing credentials or a failed fetch just surface as
    state.candles_error while the last good state.candles is kept."""
    poll_seconds = 30
    while True:
        with state.lock:
            if not state.running:
                break
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            with state.lock:
                state.candles_error = "Alpaca API key not configured"
            time.sleep(poll_seconds)
            continue
        try:
            start_iso = market_session_utc()["market_open_utc"]
            bars = fetch_alpaca_bars(ticker, start_iso)
            with state.lock:
                state.candles = bars
                state.candles_error = None
        except Exception as exc:
            with state.lock:
                state.candles_error = str(exc)
        time.sleep(poll_seconds)


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
            ticker = parse_qs(parsed.query).get("ticker", [self.ticker])[0] or self.ticker
            payload = {"snapshots": list_trading_days(ticker)}
            self.send(json.dumps(payload, default=str, allow_nan=False), "application/json")
            return
        if parsed.path == "/api/snapshot":
            ticker = parse_qs(parsed.query).get("ticker", [self.ticker])[0] or self.ticker
            snapshot_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                payload = load_snapshot_state(snapshot_id, ticker, self.window)
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
    pull_github_updates_on_startup(not args.no_github_pull)
    COLLECT_START_OFFSET_MIN = args.collect_start_offset_min
    state = LiveState()
    state.session["collection_start_utc"] = collection_start_utc(state.session["market_open_utc"])
    state.points, state.gex_ribbon = seed_session_data(args.ticker, state.session, args.window)
    state.levels_summary = seed_prev_day_eod_summary(args.ticker, state.session["trading_date"])
    state.levels_locked = True
    collect_start_ts = pd.Timestamp(state.session["collection_start_utc"])
    market_open_ts = pd.Timestamp(state.session["market_open_utc"])
    state.latest_summary, state.by_strike, state.history = seed_locked_snapshot(
        args.ticker, state.session, collect_start_ts, args.window
    )
    state.skew_summary, state.skew_by_strike, _skew_history = seed_locked_snapshot(
        args.ticker, state.session, market_open_ts, args.window
    )
    if state.skew_summary:
        state.skew_tenors = skew_tenors_payload(
            args.ticker,
            float(state.skew_summary.get("spot") or 0.0),
            state.skew_summary.get("effective_snapshot_date") or state.skew_summary.get("requested_snapshot_date") or "",
        )
    state.secondary_ticker = args.secondary_ticker.upper() if args.secondary_ticker else ""
    if state.secondary_ticker:
        state.levels_summary_secondary = seed_prev_day_eod_summary(
            state.secondary_ticker, state.session["trading_date"]
        )
        state.levels_locked_secondary = True
        secondary_futures_ticker = (args.secondary_futures_ticker or "").upper()
        if (
            state.levels_summary_secondary is not None
            and secondary_futures_ticker
            and state.levels_summary_secondary.get("spot") is not None
        ):
            basis = fetch_futures_basis(secondary_futures_ticker, state.levels_summary_secondary["spot"])
            if basis is not None:
                state.levels_summary_secondary["futures_basis"] = basis
                state.levels_summary_secondary["futures_ticker"] = secondary_futures_ticker
    Handler.state = state
    Handler.ticker = args.ticker.upper()
    Handler.window = args.window
    worker = threading.Thread(target=collector, args=(args, state), daemon=True)
    worker.start()
    if state.secondary_ticker:
        levels_worker = threading.Thread(
            target=levels_collector,
            args=(state.secondary_ticker, args, state),
            kwargs={"futures_ticker": (args.secondary_futures_ticker or "").upper()},
            daemon=True,
        )
        levels_worker.start()
    candles_worker = threading.Thread(
        target=alpaca_candles_collector, args=(args.ticker, state), daemon=True
    )
    candles_worker.start()

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
