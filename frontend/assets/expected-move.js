import { COLORS } from "./config.js";
import { fmtHM, fmtLevel, paddedRange } from "./utils.js";

function emCanvasEl() { return document.getElementById("emCanvas"); }
function emWrapEl() { return document.getElementById("emWrap"); }

function emMetrics() {
  const wrap = emWrapEl();
  const rect = wrap?.getBoundingClientRect();
  const cssW = Math.max(320, Math.floor(rect?.width || 900));
  const cssH = Math.max(220, Math.floor(rect?.height || 300));
  const margin = {l: 58, r: 82, t: 24, b: 34};
  return {cssW, cssH, margin, plotW: cssW - margin.l - margin.r, plotH: cssH - margin.t - margin.b};
}

function finitePointRows(points) {
  return (points || [])
    .map(p => ({time: new Date(p.time), spot: Number(p.spot)}))
    .filter(p => Number.isFinite(p.time.getTime()) && Number.isFinite(p.spot))
    .sort((a, b) => a.time - b.time);
}

function sessionDomain(session) {
  const start = new Date(session?.market_open_utc || session?.collection_start_utc || "");
  const end = new Date(session?.market_close_utc || "");
  const fallbackEnd = Number.isFinite(start.getTime()) ? new Date(start.getTime() + 6.5 * 60 * 60 * 1000) : null;
  return {
    start: Number.isFinite(start.getTime()) ? start : null,
    end: Number.isFinite(end.getTime()) ? end : fallbackEnd,
  };
}

function updateExpectedMoveStat(anchor, ticker) {
  const stat = document.getElementById("emStat");
  const tickerEl = document.getElementById("emTicker");
  const textTicker = String(anchor?.ticker || ticker || "QQQ").toUpperCase();
  if (tickerEl) tickerEl.textContent = textTicker;
  if (!stat) return;
  if (!anchor) {
    stat.textContent = "waiting for anchor...";
    return;
  }
  stat.textContent = `+/-$${fmtLevel(anchor.move_abs, 2)} (${fmtLevel(anchor.move_pct, 2)}%) · ATM IV ${fmtLevel(anchor.atm_iv, 1)}%`;
}

function drawGrid(ctx, margin, plotW, plotH) {
  ctx.strokeStyle = "rgba(51,65,85,0.42)";
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {
    const y = margin.t + (plotH * i) / 4;
    ctx.moveTo(margin.l, y);
    ctx.lineTo(margin.l + plotW, y);
  }
  for (let i = 0; i <= 6; i++) {
    const x = margin.l + (plotW * i) / 6;
    ctx.moveTo(x, margin.t);
    ctx.lineTo(x, margin.t + plotH);
  }
  ctx.stroke();
}

function drawHorizontalLine(ctx, y, x0, x1, color, dash, label, price) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.35;
  ctx.setLineDash(dash);
  ctx.beginPath();
  ctx.moveTo(x0, y);
  ctx.lineTo(x1, y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = color;
  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(`${label} ${fmtLevel(price, 2)}`, x1 + 8, y);
}

function expectedMoveLevel(anchor, multiple) {
  const spot = Number(anchor?.spot);
  const move = Number(anchor?.move_abs);
  if (!Number.isFinite(spot) || !Number.isFinite(move)) return null;
  return spot + move * multiple;
}

export function drawExpectedMoveChart(points, anchor, session) {
  const canvas = emCanvasEl();
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const {cssW, cssH, margin, plotW, plotH} = emMetrics();
  const targetW = Math.round(cssW * dpr);
  const targetH = Math.round(cssH * dpr);
  if (canvas.width !== targetW || canvas.height !== targetH) {
    canvas.width = targetW;
    canvas.height = targetH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = COLORS.panel;
  ctx.fillRect(0, 0, cssW, cssH);

  const rows = finitePointRows(points);
  const {start, end} = sessionDomain(session);
  if (!start || !end || end <= start) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for session...", 16, 24);
    return;
  }
  if (!rows.length) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText("Waiting for spot data...", 16, 24);
  }

  const yValues = rows.map(p => p.spot);
  if (anchor) {
    [-2, -1, 0, 1, 2].forEach(multiple => yValues.push(expectedMoveLevel(anchor, multiple)));
  }
  const range = paddedRange(yValues, 1.0, 0.14) || [0, 1];
  const [yMin, yMax] = range;
  const x0ms = start.getTime();
  const x1ms = end.getTime();
  const xToPx = d => margin.l + ((d.getTime() - x0ms) / (x1ms - x0ms)) * plotW;
  const yToPx = v => margin.t + (1 - (Number(v) - yMin) / (yMax - yMin)) * plotH;

  drawGrid(ctx, margin, plotW, plotH);

  ctx.fillStyle = COLORS.muted;
  ctx.font = "10px Menlo, Consolas, monospace";
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const value = yMax - ((yMax - yMin) * i) / 4;
    const y = margin.t + (plotH * i) / 4;
    ctx.fillText(fmtLevel(value, 2), margin.l - 8, y);
  }

  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i <= 6; i++) {
    const t = new Date(x0ms + ((x1ms - x0ms) * i) / 6);
    const x = margin.l + (plotW * i) / 6;
    ctx.fillText(fmtHM(t), x, margin.t + plotH + 9);
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(margin.l, margin.t, plotW, plotH);
  ctx.clip();
  if (rows.length > 1) {
    const maxGapMs = 3 * 60 * 1000;
    ctx.strokeStyle = "#F8FAFC";
    ctx.lineWidth = 1.65;
    ctx.setLineDash([]);
    ctx.beginPath();
    rows.forEach((p, i) => {
      const x = xToPx(p.time);
      const y = yToPx(p.spot);
      const prev = rows[i - 1];
      if (i === 0 || (prev && p.time - prev.time > maxGapMs)) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
  } else if (rows.length === 1) {
    ctx.fillStyle = "#F8FAFC";
    ctx.beginPath();
    ctx.arc(xToPx(rows[0].time), yToPx(rows[0].spot), 2.5, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();

  if (anchor) {
    const left = margin.l;
    const right = margin.l + plotW;
    const upper1 = expectedMoveLevel(anchor, 1);
    const lower1 = expectedMoveLevel(anchor, -1);
    const upper2 = expectedMoveLevel(anchor, 2);
    const lower2 = expectedMoveLevel(anchor, -2);
    if (upper2 !== null) drawHorizontalLine(ctx, yToPx(upper2), left, right, "rgba(34,211,238,0.56)", [3, 5], "+2EM", upper2);
    if (lower2 !== null) drawHorizontalLine(ctx, yToPx(lower2), left, right, "rgba(34,211,238,0.56)", [3, 5], "-2EM", lower2);
    if (upper1 !== null) drawHorizontalLine(ctx, yToPx(upper1), left, right, "#22D3EE", [8, 5], "+1EM", upper1);
    if (lower1 !== null) drawHorizontalLine(ctx, yToPx(lower1), left, right, "#22D3EE", [8, 5], "-1EM", lower1);
    drawHorizontalLine(ctx, yToPx(anchor.spot), left, right, "rgba(148,163,184,0.88)", [2, 3], "EQ", anchor.spot);
  }

  ctx.strokeStyle = "rgba(148,163,184,0.48)";
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  ctx.strokeRect(margin.l, margin.t, plotW, plotH);
}

export function drawExpectedMovePanel({latestState, panelDayState = {}, panelPayload = {}}) {
  const liveTicker = latestState?.latest_summary?.ticker || "QQQ";
  let payload = latestState;
  if (panelDayState.em !== "live" && panelPayload.em) payload = panelPayload.em;
  const ticker = payload?.latest_summary?.ticker || liveTicker || "QQQ";
  const points = payload?.points || [];
  const anchor = payload?.expected_move_anchor || null;
  const session = payload?.session || latestState?.session || null;
  updateExpectedMoveStat(anchor, ticker);
  drawExpectedMoveChart(points, anchor, session);
}
