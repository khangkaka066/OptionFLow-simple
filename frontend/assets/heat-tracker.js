import { COLORS, EXPOSURE_CONFIG } from "./config.js";
import { hexToRgb, moneyM, plotTimeNY, rgbaFromHex, timeET } from "./utils.js";

// ---- Heat Tracker: hand-rolled canvas 2D renderer ----
// Band-scale axes (real strikes / fixed time buckets), snapshot-per-bucket
// exposure values, diverging intensity-floor color map, and view-only zoom/pan.

function loadHeatPrefs() {
  let interval = 1, mode = "blocks", spotMode = "line", intensity = 35, metric = "net_gex", ticker = "QQQ";
  try {
    const storedTicker = String(localStorage.getItem("qqqHeatTicker") || "").toUpperCase();
    if (["QQQ", "NDX"].includes(storedTicker)) ticker = storedTicker;
    const storedInterval = Number(localStorage.getItem("qqqHeatInterval"));
    if ([1, 5, 15, 30].includes(storedInterval)) interval = storedInterval;
    const storedMetric = localStorage.getItem("qqqHeatMetric");
    if (EXPOSURE_CONFIG[storedMetric]) metric = storedMetric;
    const storedMode = localStorage.getItem("qqqHeatMode");
    if (storedMode === "dots" || storedMode === "blocks") mode = storedMode;
    const storedSpotMode = localStorage.getItem("qqqHeatSpotMode");
    if (storedSpotMode === "line" || storedSpotMode === "candles") spotMode = storedSpotMode;
    const storedIntensity = Number(localStorage.getItem("qqqHeatIntensity"));
    if (Number.isFinite(storedIntensity) && storedIntensity >= 0 && storedIntensity <= 100) intensity = storedIntensity;
  } catch (_err) {}
  return {interval, mode, spotMode, intensity, metric, ticker};
}

const heatPrefs = loadHeatPrefs();

export const heatState = {

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

function followOrClampView(view, minBound, maxBound, shouldFollow) {
  const span = Math.max(1, view[1] - view[0]);
  if (!shouldFollow) return clampView(view, minBound, maxBound);
  if (span >= maxBound - minBound) return [minBound, maxBound];
  return [maxBound - span, maxBound];
}

function buildHeatGrid(state) {
  const {summary, session} = state;
  const metricKey = EXPOSURE_CONFIG[state.metric] ? state.metric : "net_gex";
  const cfg = EXPOSURE_CONFIG[metricKey] || EXPOSURE_CONFIG.net_gex;
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
      const exposure = Number(row[metricKey]);
      if (Number.isFinite(Number(row.strike)) && Number.isFinite(exposure) && exposure !== 0) {
        rawMaxAbs = Math.max(rawMaxAbs, Math.abs(exposure));
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
      const exposure = Number(row[metricKey]);
      if (!Number.isFinite(strike) || !Number.isFinite(exposure) || Math.abs(exposure) < minDrawAbs) continue;
      const aggregate = exposureByStrike.get(strike) || {strike, posMax: 0, negMax: 0, posSum: 0, negSum: 0, posCount: 0, negCount: 0};
      if (exposure > 0) {
        aggregate.posMax = Math.max(aggregate.posMax, exposure);
        aggregate.posSum += exposure;
        aggregate.posCount += 1;
      } else {
        aggregate.negMax = Math.max(aggregate.negMax, -exposure);
        aggregate.negSum += -exposure;
        aggregate.negCount += 1;
      }
      exposureByStrike.set(strike, aggregate);
      grid.set(bucketIdx + "|" + strike, {exposure, time: snap.time});
      gridAbsValues.push(Math.abs(exposure));
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
      {strike: level.strike, exposure: level.posMax, side: "call", score: level.posMax * 0.75 + (level.posCount ? level.posSum / level.posCount : 0) * 0.25},
      {strike: level.strike, exposure: -level.negMax, side: "put", score: level.negMax * 0.75 + (level.negCount ? level.negSum / level.negCount : 0) * 0.25},
    ])
    .filter(level => level.score > rawMaxAbs * 0.08);
  const summaryLevels = metricKey === "net_gex"
    ? (summary?.top_abs_gex_levels || []).map(l => ({
        strike: Number(l.strike),
        exposure: Number(l.net_gex),
        side: Number(l.net_gex) >= 0 ? "call" : "put",
        score: Math.abs(Number(l.net_gex) || 0),
      }))
    : [];
  const levels = [...dynamicLevels, ...summaryLevels]
    .filter(l => Number.isFinite(l.strike) && Number.isFinite(l.exposure) && Number.isFinite(l.score));
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
    const callLabels = metricKey === "net_gex" ? ["CALL RESISTANCE", "CALL WALL 2", "CALL WALL 3"] : [cfg.wallLabel, cfg.wallLabel + " 2", cfg.wallLabel + " 3"];
    uniqueLevels(levels.filter(l => l.strike > spot && l.exposure > 0), "call").forEach((l, i) => {
      wallLines.push({strike: l.strike, color: COLORS.cyan, lineColor: rgbaFromHex(COLORS.cyan, 0.45), dash: i === 0 ? "solid" : "dash", label: callLabels[i] + " " + l.strike.toFixed(0), side: "right"});
    });
    const putLabels = metricKey === "net_gex" ? ["PUT SUPPORT", "PUT WALL 2", "PUT WALL 3"] : [cfg.wallLabel, cfg.wallLabel + " 2", cfg.wallLabel + " 3"];
    uniqueLevels(levels.filter(l => l.strike < spot && l.exposure < 0), "put").forEach((l, i) => {
      wallLines.push({strike: l.strike, color: COLORS.orange, lineColor: rgbaFromHex(COLORS.orange, 0.45), dash: i === 0 ? "solid" : "dash", label: putLabels[i] + " " + l.strike.toFixed(0), side: "left"});
    });
  }
  const flip = summary?.[cfg.flipKey];
  if (flip) {
    wallLines.push({strike: Number(flip), color: "#C084FC", lineColor: "rgba(192,132,252,0.4)", dash: "dot", label: cfg.flipLabel + " " + Number(flip).toFixed(2), side: "right"});
  }

  return {strikes, bucketCount, bucketStartUtc, bucketEndUtc, intervalMs, grid, maxAbs, wallLines, points, candles, spot, metricKey, label: cfg.label};
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
      const t = heatIntensityT(Math.abs(cell.exposure), g.maxAbs, floorFrac);
      ctx.fillStyle = heatColorFromT(cell.exposure, t);
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
      const color = c.close >= c.open ? COLORS.bull : COLORS.bear;
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
  heatState.autoFollow = false;
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
  heatState.pointerInside = true;
  heatState.autoFollow = false;
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
  const metricLabel = String(g.metricKey || "net_gex").replace("net_", "").toUpperCase();
  tooltip.innerHTML = `<b>$${fmtStrikeLabel(strike)}</b><br>${metricLabel}&nbsp;&nbsp;${moneyM(cell.exposure)}<br>` +
    `<span style="color:#94A3B8">${timeET(cell.time)} · O ${fmtHM(bucketOpen)} C ${fmtHM(bucketClose)}</span>`;
  tooltip.style.left = (px + 14) + "px";
  tooltip.style.top = (py + 10) + "px";
  tooltip.style.display = "block";
}

function onHeatMouseMove(e) {
  heatState.pointerInside = true;
  heatState.autoFollow = false;
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
  if (!heatState.pointerInside) {
    heatState.autoFollow = true;
    renderHeatCanvas();
  }
}

function onHeatMouseLeave() {
  heatState.pointerInside = false;
  heatDrag = null;
  heatState.autoFollow = true;
  hideHeatTooltip();
  renderHeatCanvas();
}

export function initHeatTrackerControls(onRefresh) {
  const canvas = heatCanvasEl();
  const tickerSel = document.getElementById("heatTicker");
  const metricSel = document.getElementById("heatMetric");
  const intervalSel = document.getElementById("heatInterval");
  const modeBtn = document.getElementById("heatModeToggle");
  const spotBtn = document.getElementById("heatSpotToggle");
  const intensityInput = document.getElementById("heatIntensity");
  const resetBtn = document.getElementById("heatResetZoom");
  if (tickerSel) {
    tickerSel.value = heatState.ticker;
    tickerSel.addEventListener("change", async () => {
      heatState.ticker = String(tickerSel.value || "QQQ").toUpperCase();
      try { localStorage.setItem("qqqHeatTicker", heatState.ticker); } catch (_err) {}
      heatState.sessionKey = "";
      heatState.viewX = [0, 1];
      heatState.viewY = [0, 1];
      heatState.grid = null;
      heatState.autoFollow = true;
      hideHeatTooltip();
      try {
        await onRefresh?.(true);
      } catch (err) {
        document.getElementById("status").textContent = "Heat Tracker error: " + err.message;
      }
    });
  }
  if (metricSel) {
    metricSel.value = heatState.metric;
    metricSel.addEventListener("change", () => {
      heatState.metric = EXPOSURE_CONFIG[metricSel.value] ? metricSel.value : "net_gex";
      try { localStorage.setItem("qqqHeatMetric", heatState.metric); } catch (_err) {}
      heatState.grid = buildHeatGrid(heatState);
      heatState.viewX = [0, heatState.grid.bucketCount];
      heatState.viewY = [0, heatState.grid.strikes.length];
      heatState.autoFollow = true;
      hideHeatTooltip();
      renderHeatCanvas();
    });
  }
  if (intervalSel) {
    intervalSel.value = String(heatState.interval);
    intervalSel.addEventListener("change", () => {
      heatState.interval = Number(intervalSel.value) || 1;
      try { localStorage.setItem("qqqHeatInterval", String(heatState.interval)); } catch (_err) {}
      heatState.grid = buildHeatGrid(heatState);
      heatState.viewX = [0, heatState.grid.bucketCount];
      heatState.viewY = [0, heatState.grid.strikes.length];
      heatState.autoFollow = true;
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
      heatState.autoFollow = true;
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

export function drawGexRibbon(ribbon, points, summary, session, candles = []) {
  heatState.ribbon = ribbon || [];
  heatState.points = points || [];
  heatState.candles = candles || [];
  heatState.summary = summary || {};
  heatState.session = session || null;
  const sessionOpenUtc = session && session.market_open_utc ? new Date(session.market_open_utc) : null;
  const sessionKey = "session-" + (session?.trading_date || (sessionOpenUtc ? plotTimeNY(sessionOpenUtc).slice(0, 10) : "")) + (session?.history_snapshot_id ? "-" + session.history_snapshot_id : "");
  const previousStrikeCount = heatState.grid?.strikes?.length || 0;
  const wasFullStrikeView = previousStrikeCount > 0
    && heatState.viewY[0] <= 0.001
    && Math.abs(heatState.viewY[1] - previousStrikeCount) <= 0.001;
  const grid = buildHeatGrid(heatState);
  heatState.grid = grid;
  if (sessionKey !== heatState.sessionKey) {
    heatState.sessionKey = sessionKey;
    heatState.viewX = [0, grid.bucketCount];
    heatState.viewY = [0, grid.strikes.length];
    heatState.autoFollow = true;
  } else {
    heatState.viewX = followOrClampView(heatState.viewX, 0, grid.bucketCount, heatState.autoFollow);
    heatState.viewY = heatState.autoFollow && wasFullStrikeView
      ? [0, grid.strikes.length]
      : clampView(heatState.viewY, 0, grid.strikes.length);
  }
  renderHeatCanvas();
}
