import { COLORS } from "./config.js";
import { medianFinite, nyMinutes, plotTimeNY, rgbaFromHex } from "./utils.js";

let latestStateProvider = () => null;

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

function zoomAxis(view, cursorPx, originPx, spanPx, factor, minBound, maxBound, invert = false) {
  const frac = invert ? 1 - (cursorPx - originPx) / spanPx : (cursorPx - originPx) / spanPx;
  const cursorVal = view[0] + frac * (view[1] - view[0]);
  let newSpan = Math.max(2, Math.min(maxBound - minBound, (view[1] - view[0]) * factor));
  let v0 = cursorVal - frac * newSpan;
  let v1 = v0 + newSpan;
  if (v0 < minBound) { v0 = minBound; v1 = v0 + newSpan; }
  if (v1 > maxBound) { v1 = maxBound; v0 = v1 - newSpan; }
  return [v0, v1];
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

function cleanFlowSeries(values, opts = {}) {
  const windowSize = opts.windowSize || 5;
  const minWindow = opts.minWindow || 4;
  const minJump = opts.minJump || 4.0;
  const jumpRatio = opts.jumpRatio || 0.28;
  const raw = values.map(v => Number.isFinite(v) ? Number(v) : null);
  const cleaned = raw.slice();
  for (let i = 0; i < raw.length; i++) {
    if (!Number.isFinite(raw[i])) continue;
    const start = Math.max(0, i - windowSize + 1);
    const window = raw.slice(start, i + 1).filter(Number.isFinite);
    if (window.length < minWindow) continue;
    const med = medianFinite(window);
    const threshold = Math.max(minJump, Math.abs(med) * jumpRatio);
    if (Number.isFinite(med) && Math.abs(raw[i] - med) > threshold) {
      cleaned[i] = med;
    }
  }
  return cleaned;
}

function emaFlowSeries(values, period = 5) {
  const alpha = 2 / (period + 1);
  let prev = null;
  return values.map(v => {
    if (!Number.isFinite(v)) return null;
    prev = Number.isFinite(prev) ? prev + alpha * (Number(v) - prev) : Number(v);
    return prev;
  });
}

function prepareFlowSignals(rows) {
  rows.forEach(row => {
    row.ivRaw = Number.isFinite(row.iv) ? Number(row.iv) : null;
    row.arvRaw = Number.isFinite(row.arv) ? Number(row.arv) : null;
  });
  const ivClean = cleanFlowSeries(rows.map(r => r.ivRaw), {
    windowSize: 5,
    minWindow: 4,
    minJump: 4.0,
    jumpRatio: 0.28
  });
  const arvClean = cleanFlowSeries(rows.map(r => r.arvRaw), {
    windowSize: 5,
    minWindow: 4,
    minJump: 5.0,
    jumpRatio: 0.40
  });
  const ivSmooth = emaFlowSeries(ivClean, 5);
  const arvSmooth = emaFlowSeries(arvClean, 5);
  rows.forEach((row, i) => {
    row.iv = Number.isFinite(ivSmooth[i]) ? ivSmooth[i] : row.ivRaw;
    row.arv = Number.isFinite(arvSmooth[i]) ? arvSmooth[i] : row.arvRaw;
  });
}

function flowTrendState(rows, series, lookback = 5) {
  const clean = rows.filter(r => Number.isFinite(r[series])).map(r => Number(r[series]));
  if (clean.length < 3) return "warming";
  const latest = clean[clean.length - 1];
  const prev = clean[Math.max(0, clean.length - 1 - lookback)];
  const delta = latest - prev;
  const threshold = Math.max(0.35, Math.abs(prev) * 0.025);
  if (delta > threshold) return "rising";
  if (delta < -threshold) return "falling";
  return "flat";
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
  let lines = {iv: true, ivRaw: false, arv: true, arvRaw: false};
  try {
    const storedSpotMode = localStorage.getItem("qqqFlowSpotMode");
    if (storedSpotMode === "line" || storedSpotMode === "candles") spotMode = storedSpotMode;
    const storedLines = JSON.parse(localStorage.getItem("qqqFlowLines") || "null");
    if (storedLines && typeof storedLines === "object") {
      lines = {...lines, ...storedLines};
    }
  } catch (_err) {}
  return {spotMode, lines};
}

const flowPrefs = loadFlowPrefs();

export const flowState = {
  points: [], candles: [], session: null, sessionKey: "",
  viewX: [0, 1], bucketCount: 1, spotMode: flowPrefs.spotMode,
  lines: flowPrefs.lines, autoFollow: true, pointerInside: false
};

let flowDrag = null;

function buildFlowRows(points, session, candles = []) {
  const flowStartUtc = session && session.market_open_utc
    ? new Date(session.market_open_utc)
    : (session && session.collection_start_utc ? new Date(session.collection_start_utc) : null);
  points = points || [];
  if (flowStartUtc && !Number.isNaN(flowStartUtc.getTime())) {
    points = points.filter(p => p.time && new Date(p.time).getTime() >= flowStartUtc.getTime());
  }
  const moneyness = document.getElementById("flowMoneyness")?.value || "ATM";
  const expiryFilter = document.getElementById("flowExpiry")?.value || "0DTE";
  points = expiryFilter === "0DTE" ? points.filter(p => p.expiry !== "ALL") : points;

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
  prepareFlowSignals(rows);
  let cum = 0;
  rows.forEach(row => {
    if (!row.hasData) return;
    cum += row.call - row.put;
    row.net = cum / 1000;
  });
  return {rows, intervalMin, startUtc, points};
}

function flowTooltipHtml(r) {
  return `IV ${Number.isFinite(r.iv) ? r.iv.toFixed(1) + "%" : "NA"} (${flowIvStatus(r.bucket) || "NA"}) &nbsp; ARV ${Number.isFinite(r.arv) ? r.arv.toFixed(1) + "%" : "NA"} &nbsp; Call - Put ${Number.isFinite(r.net) ? r.net.toFixed(1) + "K" : "NA"}<br>` +
    `RAW: IV ${Number.isFinite(r.ivRaw) ? r.ivRaw.toFixed(1) + "%" : "NA"} · ARV ${Number.isFinite(r.arvRaw) ? r.arvRaw.toFixed(1) + "%" : "NA"}<br>` +
    `TRADED IN BUCKET: Call contracts ${(r.call / 1000).toFixed(1)}K · Put contracts ${(r.put / 1000).toFixed(1)}K · price ${Number.isFinite(r.spot) ? r.spot.toFixed(2) : "NA"}`;
}

function trackerTooltipHtml(r, isPremium) {
  if (isPremium) {
    const fmtGrossD = v => "$" + formatTrackerK(v);
    const fmtSignedD = v => formatSignedTrackerK(v, "$");
    const netBias = trackerBiasLabel(r.netPremCumK);
    return `<b>${fmtHM(r.bucket)} ET</b><br>` +
      `<b>BUCKET GROSS PREMIUM</b><br>` +
      `<span style="color:${COLORS.cyan}">Call ${fmtGrossD(r.callPremK)}</span> ` +
      `<span style="color:${COLORS.orange}">Put ${fmtGrossD(r.putPremK)}</span><br>` +
      `<b>SESSION GROSS PREMIUM</b><br>` +
      `<span style="color:${COLORS.cyan}">Calls ${fmtGrossD(r.callGrossCumK)}</span> ` +
      `<span style="color:${COLORS.orange}">Puts ${fmtGrossD(r.putGrossCumK)}</span><br>` +
      `<b>DIRECTIONAL FLOW (est.)</b><br>` +
      `<span style="color:${COLORS.cyan}">Call pressure ${fmtSignedD(r.callNetCumK)}</span><br>` +
      `<span style="color:${COLORS.orange}">Put pressure ${fmtSignedD(r.putDirectionalCumK)}</span><br>` +
      `<b style="color:${COLORS.net}">Net pressure ${fmtSignedD(r.netPremCumK)} ${netBias}</b><br>` +
      `<span style="color:rgba(148,163,184,0.75); font-size:10px;">Quy ước: call buy +, put buy -, put sell +; ước tính theo mid tick</span>`;
  }
  const netBias = trackerBiasLabel(r.netVolCumK);
  return `<b>${fmtHM(r.bucket)} ET</b><br>` +
    `<span style="color:${COLORS.cyan}">Call volume ${formatTrackerK(r.callDeltaK)}</span><br>` +
    `<span style="color:${COLORS.orange}">Put volume ${formatSignedTrackerK(-r.putDeltaK)}</span><br>` +
    `<span style="color:${COLORS.text}">Bucket net ${formatSignedTrackerK(r.callDeltaK - r.putDeltaK)}</span><br>` +
    `<b style="color:${COLORS.net}">Net pressure ${formatSignedTrackerK(r.netVolCumK)} ${netBias}</b>`;
}

function nearestRowIndex(rows, timeMs) {
  let idx = -1, best = Infinity;
  rows.forEach((r, i) => {
    const d = Math.abs(r.bucket.getTime() - timeMs);
    if (d < best) { best = d; idx = i; }
  });
  return idx;
}

function clearFlowHover() {
  const tip = document.getElementById("flowTooltip");
  if (tip) tip.style.display = "none";
  if (flowState.rows && flowState.rows.length) {
    drawFlow(flowState.points, flowState.session, flowState.candles);
  }
}

function mirrorFlowHover(timeMs) {
  const rows = flowState.rows;
  if (!rows || !rows.length) return;
  drawFlow(flowState.points, flowState.session, flowState.candles);
  const idx = nearestRowIndex(rows, timeMs);
  if (idx < 0 || idx < flowState.viewX[0] || idx >= flowState.viewX[1]) {
    const tip = document.getElementById("flowTooltip");
    if (tip) tip.style.display = "none";
    return;
  }
  const r = rows[idx];
  const {canvas, top, bot, cssW} = flowMetrics();
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const domainSpan = flowState.viewX[1] - flowState.viewX[0];
  const px = top.x + (idx + 0.5 - flowState.viewX[0]) / domainSpan * top.w;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.strokeStyle = COLORS.net;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(px, top.y); ctx.lineTo(px, bot.y + bot.h); ctx.stroke();
  ctx.setLineDash([]);
  const tip = document.getElementById("flowTooltip");
  if (tip) {
    tip.style.display = "block";
    tip.style.left = Math.min(cssW - 250, px + 14) + "px";
    tip.style.top = "8px";
    tip.innerHTML = flowTooltipHtml(r);
  }
}

function clearTrackerHover() {
  const tip = document.getElementById("trackerTooltip");
  if (tip) tip.style.display = "none";
  if (trackerState.rows && trackerState.rows.length) {
    drawFlowTracker(trackerState.points, trackerState.session);
  }
}

function mirrorTrackerHover(timeMs) {
  const rows = trackerState.rows;
  if (!rows || !rows.length) return;
  drawFlowTracker(trackerState.points, trackerState.session);
  const idx = nearestRowIndex(rows, timeMs);
  if (idx < 0 || idx < trackerState.viewX[0] || idx >= trackerState.viewX[1]) {
    const tip = document.getElementById("trackerTooltip");
    if (tip) tip.style.display = "none";
    return;
  }
  const r = rows[idx];
  const {canvas, plot, cssW} = trackerMetrics();
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const domainSpan = trackerState.viewX[1] - trackerState.viewX[0];
  const px = plot.x + (idx + 0.5 - trackerState.viewX[0]) / domainSpan * plot.w;
  const isPremium = (document.getElementById("trackerMode")?.value || "CALL_PUT") === "PREMIUM";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.strokeStyle = COLORS.net;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(px, plot.y); ctx.lineTo(px, plot.y + plot.h); ctx.stroke();
  ctx.setLineDash([]);
  const tip = document.getElementById("trackerTooltip");
  if (tip) {
    tip.style.display = "block";
    const tipW = isPremium ? 240 : 230;
    tip.style.left = Math.min(cssW - tipW, px + 14) + "px";
    tip.style.top = "8px";
    tip.style.whiteSpace = isPremium ? "normal" : "nowrap";
    tip.style.maxWidth = isPremium ? tipW + "px" : "";
    tip.innerHTML = trackerTooltipHtml(r, isPremium);
  }
}

export function drawFlow(points, session, candles = []) {
  const {rows, intervalMin, startUtc, points: rowPoints} = buildFlowRows(points, session, candles);
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
  if (!rowPoints.length && !(candles || []).length) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for data...", 16, 24);
    return;
  }
  points = rowPoints;
  const validIv = rows.flatMap(r => [r.ivRaw, r.arvRaw, r.iv, r.arv]).filter(Number.isFinite);
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
  flowState.rows = rows;
  const flowSessionKey = "session-" + (session?.trading_date || (startUtc ? plotTimeNY(startUtc).slice(0, 10) : "")) + (session?.history_snapshot_id ? "-" + session.history_snapshot_id : "") + "-" + intervalMin;
  if (flowSessionKey !== flowState.sessionKey) {
    flowState.sessionKey = flowSessionKey;
    flowState.viewX = [0, rows.length];
    flowState.autoFollow = true;
  } else {
    flowState.viewX = followOrClampView(flowState.viewX, 0, rows.length, flowState.autoFollow);
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

  function line(series, yFn, color, width = 1.5, alpha = 1) {
    ctx.save();
    ctx.globalAlpha = alpha;
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
    ctx.restore();
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
      const color = r.spot >= r.spotOpen ? COLORS.bull : COLORS.bear;
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
  const linePrefs = flowState.lines || {};
  if (linePrefs.ivRaw) line("ivRaw", yPct, COLORS.cyan, 1.0, 0.25);
  if (linePrefs.arvRaw) line("arvRaw", yPct, COLORS.orange, 1.0, 0.25);
  if (linePrefs.iv !== false) line("iv", yPct, COLORS.cyan, 1.9, 1);
  if (linePrefs.arv !== false) line("arv", yPct, COLORS.orange, 1.7, 1);
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
  const ivTrend = flowTrendState(rows, "iv");
  const arvTrend = flowTrendState(rows, "arv");
  ctx.fillStyle = COLORS.muted;
  ctx.fillText(` · IV ${ivTrend} · ARV ${arvTrend}`, top.x + 238, top.y + 12);
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
    flowState.pointerInside = true;
    flowState.autoFollow = false;
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    if (flowDrag) {
      const dxDomain = (ev.clientX - flowDrag.startX) / top.w * (flowDrag.viewX[1] - flowDrag.viewX[0]);
      flowState.viewX = clampView([flowDrag.viewX[0] - dxDomain, flowDrag.viewX[1] - dxDomain], 0, flowState.bucketCount);
      drawFlow(points, session, candles);
      const tip = document.getElementById("flowTooltip");
      if (tip) tip.style.display = "none";
      clearTrackerHover();
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
      tip.innerHTML = flowTooltipHtml(r);
    }
    mirrorTrackerHover(r.bucket.getTime());
  };
  canvas.onmouseleave = () => {
    flowState.pointerInside = false;
    flowDrag = null;
    flowState.autoFollow = true;
    const tip = document.getElementById("flowTooltip");
    if (tip) tip.style.display = "none";
    drawFlow(points, session, candles);
    clearTrackerHover();
  };
}

function onFlowWheel(e) {
  if (flowState.bucketCount <= 1) return;
  e.preventDefault();
  flowState.autoFollow = false;
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
  flowState.pointerInside = true;
  flowState.autoFollow = false;
  flowDrag = {startX: e.clientX, viewX: [...flowState.viewX]};
}

function onFlowMouseUp() {
  flowDrag = null;
  if (!flowState.pointerInside) {
    flowState.autoFollow = true;
    drawFlow(flowState.points, flowState.session, flowState.candles);
  }
}

export function initFlowControls(getLatestState) {
  latestStateProvider = getLatestState || latestStateProvider;
  const canvas = document.getElementById("flowCanvas");
  const spotBtn = document.getElementById("flowSpotToggle");
  const linesBtn = document.getElementById("flowLinesToggle");
  const linesMenu = document.getElementById("flowLinesMenu");
  const lineInputs = Array.from(document.querySelectorAll("[data-flow-line]"));
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
      const latestState = latestStateProvider();
      if (latestState) drawFlow(latestState.points || [], latestState.session || null, latestState.candles || []);
    });
  }
  if (linesBtn && linesMenu) {
    const syncLineInputs = () => {
      lineInputs.forEach(input => {
        const key = input.dataset.flowLine;
        input.checked = flowState.lines?.[key] !== false;
      });
    };
    syncLineInputs();
    linesBtn.addEventListener("click", ev => {
      ev.stopPropagation();
      linesMenu.classList.toggle("open");
    });
    linesMenu.addEventListener("click", ev => ev.stopPropagation());
    document.addEventListener("click", () => linesMenu.classList.remove("open"));
    lineInputs.forEach(input => {
      input.addEventListener("change", () => {
        const key = input.dataset.flowLine;
        flowState.lines = {...flowState.lines, [key]: input.checked};
        try { localStorage.setItem("qqqFlowLines", JSON.stringify(flowState.lines)); } catch (_err) {}
        const latestState = latestStateProvider();
        if (latestState) drawFlow(latestState.points || [], latestState.session || null, latestState.candles || []);
      });
    });
  }
  if (canvas) {
    canvas.addEventListener("wheel", onFlowWheel, {passive: false});
    canvas.addEventListener("mousedown", onFlowMouseDown);
    window.addEventListener("mouseup", onFlowMouseUp);
  }
}

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

export const trackerState = {
  points: [], session: null, sessionKey: "",
  viewX: [0, 1], bucketCount: 1,
  lines: loadTrackerLinePrefs(), autoFollow: true, pointerInside: false
};

let trackerDrag = null;

function loadTrackerLinePrefs() {
  const defaults = {net: true, netRaw: false, call: true, callRaw: false, put: true, putRaw: false};
  try {
    const stored = JSON.parse(localStorage.getItem("qqqTrackerLines") || "null");
    if (stored && typeof stored === "object") return {...defaults, ...stored};
  } catch (_err) {}
  return defaults;
}

function formatTrackerK(v) {
  if (!Number.isFinite(v)) return "NA";
  const sign = v < 0 ? "-" : "";
  return sign + Math.round(Math.abs(v)) + "K";
}

function formatSignedTrackerK(v, prefix = "") {
  if (!Number.isFinite(v)) return prefix + "NA";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return prefix + sign + Math.round(Math.abs(v)) + "K";
}

function trackerBiasLabel(v) {
  if (!Number.isFinite(v)) return "NEUTRAL";
  const abs = Math.abs(v);
  if (abs < 250) return "NEUTRAL";
  return v > 0 ? "BULLISH" : "BEARISH";
}

function smoothTrackerSeries(values) {
  const cleaned = cleanFlowSeries(values, {
    windowSize: 7,
    minWindow: 4,
    minJump: 18,
    jumpRatio: 0.55
  });
  return emaFlowSeries(cleaned, 5);
}

function prepareTrackerSignals(rows) {
  const fields = ["callCum", "putCum", "netVolCumK", "callNetCumK", "putNetCumK", "putDirectionalCumK", "netPremCumK"];
  fields.forEach(field => {
    rows.forEach(row => { row[field + "Raw"] = Number.isFinite(row[field]) ? Number(row[field]) : null; });
    const smoothed = smoothTrackerSeries(rows.map(row => row[field + "Raw"]));
    rows.forEach((row, i) => {
      row[field + "Smooth"] = Number.isFinite(smoothed[i]) ? smoothed[i] : row[field + "Raw"];
    });
  });
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
    // Empty buckets should carry the last cumulative state forward. Otherwise
    // hover labels show $NA even though the correct meaning is "no new flow".
    row.callCum = callCum / 1000;
    row.putCum = -putCum / 1000;
    row.callGrossCumK = callGrossCum / 1000;
    row.putGrossCumK = putGrossCum / 1000;
    row.callNetCumK = callNetCum / 1000;
    row.putNetCumK = putNetCum / 1000;
    row.putDirectionalCumK = -putNetCum / 1000;
    row.netVolCumK = (callCum - putCum) / 1000;
    row.netPremCumK = row.callNetCumK + row.putDirectionalCumK;
    row.callDeltaK = row.callDelta / 1000;
    row.putDeltaK = row.putDelta / 1000;
    row.callPremK = row.callPrem / 1000;
    row.putPremK = row.putPrem / 1000;
  });
  prepareTrackerSignals(rows);
  return {rows, intervalMin, startUtc};
}

export function drawFlowTracker(points, session) {
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
  trackerState.rows = rows;
  if (!(points || []).length) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for data...", 16, 24);
    return;
  }
  const metric = document.getElementById("trackerMode")?.value || "CALL_PUT";
  const isPremium = metric === "PREMIUM";
  const lineCallRawField = isPremium ? "callNetCumKRaw" : "callCumRaw";
  const linePutRawField = isPremium ? "putDirectionalCumKRaw" : "putCumRaw";
  const netRawField = isPremium ? "netPremCumKRaw" : "netVolCumKRaw";
  const lineCallField = isPremium ? "callNetCumKSmooth" : "callCumSmooth";
  const linePutField = isPremium ? "putDirectionalCumKSmooth" : "putCumSmooth";
  const netField = isPremium ? "netPremCumKSmooth" : "netVolCumKSmooth";
  const fmtAxis = v => isPremium ? "$" + formatTrackerK(v) : formatTrackerK(v);
  const dateEl = document.getElementById("trackerDate");
  if (dateEl) {
    const d = session?.trading_date || (startUtc ? startUtc.toISOString().slice(0, 10) : "--");
    const [yyyy, mm, dd] = String(d).split("-");
    dateEl.textContent = yyyy && mm && dd ? `${dd}/${mm}/${yyyy}` : "--";
  }
  const tickerEl = document.getElementById("trackerTicker");
  if (tickerEl) tickerEl.textContent = latestStateProvider()?.latest_summary?.ticker || "QQQ";
  const sessionKey = "tracker-" + (session?.trading_date || "") + "-" + intervalMin + "-" +
    (document.getElementById("trackerMoneyness")?.value || "ATM") + "-" +
    (document.getElementById("trackerExpiry")?.value || "0DTE") +
    (session?.history_snapshot_id ? "-" + session.history_snapshot_id : "");
  if (sessionKey !== trackerState.sessionKey) {
    trackerState.sessionKey = sessionKey;
    trackerState.viewX = [0, rows.length];
    trackerState.autoFollow = true;
  } else {
    trackerState.viewX = followOrClampView(trackerState.viewX, 0, rows.length, trackerState.autoFollow);
  }
  const domainSpan = Math.max(1, trackerState.viewX[1] - trackerState.viewX[0]);
  const x = i => plot.x + (i + 0.5 - trackerState.viewX[0]) / domainSpan * plot.w;
  const valid = rows.flatMap(r => [
    r[lineCallField],
    r[linePutField],
    r[netField]
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
  function trackerLine(series, color, width, alpha) {
    ctx.strokeStyle = rgbaFromHex(color, alpha);
    ctx.lineWidth = width;
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
  function trackerNetFill(series, color, alpha) {
    ctx.beginPath();
    let started = false;
    let firstPx = null, lastPx = null;
    rows.forEach((r, i) => {
      const v = r[series];
      if (!Number.isFinite(v)) return;
      const px = x(i), py = y(v);
      if (!started) { ctx.moveTo(px, py); started = true; firstPx = px; }
      else ctx.lineTo(px, py);
      lastPx = px;
    });
    if (!started) return;
    ctx.lineTo(lastPx, zeroY);
    ctx.lineTo(firstPx, zeroY);
    ctx.closePath();
    ctx.fillStyle = rgbaFromHex(color, alpha);
    ctx.fill();
  }
  const linePrefs = trackerState.lines || {};
  if (linePrefs.putRaw) trackerLine(linePutRawField, COLORS.orange, 1.0, 0.25);
  if (linePrefs.callRaw) trackerLine(lineCallRawField, COLORS.cyan, 1.0, 0.25);
  if (linePrefs.netRaw) trackerLine(netRawField, COLORS.net, 1.0, 0.25);
  if (linePrefs.net !== false) trackerNetFill(netField, COLORS.net, 0.10);
  if (linePrefs.put !== false) trackerLine(linePutField, COLORS.orange, 1.6, 0.75);
  if (linePrefs.call !== false) trackerLine(lineCallField, COLORS.cyan, 1.6, 0.75);
  if (linePrefs.net !== false) trackerLine(netField, COLORS.net, 2.4, 1);
  ctx.restore();

  ctx.fillStyle = COLORS.text;
  ctx.font = "700 16px Inter, system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("Flow Tracker" + (isPremium ? " · Premium $ (beta)" : ""), 14, 24);
  const legend = [["Net pressure", COLORS.net], ["Call pressure", COLORS.cyan], ["Put pressure", COLORS.orange]];
  let legendX = 14;
  ctx.font = "11px Menlo, Consolas, monospace";
  legend.forEach(([label, color]) => {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(legendX, 40, 3.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(203,213,225,0.85)";
    ctx.textAlign = "left";
    ctx.fillText(label, legendX + 8, 43);
    legendX += ctx.measureText(label).width + 26;
  });
  ctx.fillStyle = "rgba(148,163,184,0.62)";
  ctx.font = "11px Menlo, Consolas, monospace";
  const tickEvery = Math.max(1, Math.floor(rows.length / 8));
  rows.forEach((r, i) => {
    if (i % tickEvery !== 0 && i !== rows.length - 1) return;
    ctx.textAlign = "center";
    ctx.fillText(fmtHM(r.bucket), x(i), cssH - 8);
  });

  canvas.onmousemove = ev => {
    trackerState.pointerInside = true;
    trackerState.autoFollow = false;
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    if (trackerDrag) {
      const dxDomain = (ev.clientX - trackerDrag.startX) / plot.w * (trackerDrag.viewX[1] - trackerDrag.viewX[0]);
      trackerState.viewX = clampView([trackerDrag.viewX[0] - dxDomain, trackerDrag.viewX[1] - dxDomain], 0, trackerState.bucketCount);
      drawFlowTracker(points, session);
      document.getElementById("trackerTooltip")?.style.setProperty("display", "none");
      clearFlowHover();
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
      tip.innerHTML = trackerTooltipHtml(r, isPremium);
    }
    mirrorFlowHover(r.bucket.getTime());
  };
  canvas.onmouseleave = () => {
    trackerState.pointerInside = false;
    trackerDrag = null;
    trackerState.autoFollow = true;
    const tip = document.getElementById("trackerTooltip");
    if (tip) tip.style.display = "none";
    drawFlowTracker(points, session);
    clearFlowHover();
  };
}

function onTrackerWheel(e) {
  if (trackerState.bucketCount <= 1) return;
  e.preventDefault();
  trackerState.autoFollow = false;
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
  trackerState.pointerInside = true;
  trackerState.autoFollow = false;
  trackerDrag = {startX: e.clientX, viewX: [...trackerState.viewX]};
}

function onTrackerMouseUp() {
  trackerDrag = null;
  if (!trackerState.pointerInside) {
    trackerState.autoFollow = true;
    drawFlowTracker(trackerState.points, trackerState.session);
  }
}

export function initTrackerControls(getLatestState) {
  latestStateProvider = getLatestState || latestStateProvider;
  const canvas = document.getElementById("trackerCanvas");
  const linesBtn = document.getElementById("trackerLinesToggle");
  const linesMenu = document.getElementById("trackerLinesMenu");
  if (linesBtn && linesMenu) {
    const lineInputs = [...linesMenu.querySelectorAll("input[data-tracker-line]")];
    const syncLineInputs = () => {
      lineInputs.forEach(input => {
        const key = input.dataset.trackerLine;
        input.checked = trackerState.lines[key] !== false;
      });
    };
    syncLineInputs();
    linesBtn.addEventListener("click", ev => {
      ev.stopPropagation();
      linesMenu.classList.toggle("open");
    });
    linesMenu.addEventListener("click", ev => ev.stopPropagation());
    document.addEventListener("click", () => linesMenu.classList.remove("open"));
    lineInputs.forEach(input => {
      input.addEventListener("change", () => {
        const key = input.dataset.trackerLine;
        trackerState.lines = {...trackerState.lines, [key]: input.checked};
        try { localStorage.setItem("qqqTrackerLines", JSON.stringify(trackerState.lines)); } catch (_err) {}
        const latestState = latestStateProvider();
        if (latestState) drawFlowTracker(latestState.points || [], latestState.session || null);
      });
    });
  }
  if (canvas) {
    canvas.addEventListener("wheel", onTrackerWheel, {passive: false});
    canvas.addEventListener("mousedown", onTrackerMouseDown);
    window.addEventListener("mouseup", onTrackerMouseUp);
  }
}
