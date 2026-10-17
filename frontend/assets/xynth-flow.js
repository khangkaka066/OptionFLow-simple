import { apiUrl, COLORS } from "./config.js";

let pending = false;
let latestPayload = null;
let plotted = [];
let hoveredIndex = -1;
let eventsBound = false;

export function aggregateXynthRows(rows) {
  const groups = new Map();
  for (const row of rows || []) {
    const time = Date.parse(row.time);
    if (!Number.isFinite(time)) continue;
    let group = groups.get(time);
    if (!group) {
      group = {time, timeEt: row.time_et || "", count: 0, callSize: 0, putSize: 0,
        ivWeighted: 0, ivWeight: 0, spot: null};
      groups.set(time, group);
    }
    const size = Math.max(0, Number(row.size) || 0);
    group.count += 1;
    if (row.cp === "C") group.callSize += size;
    if (row.cp === "P") group.putSize += size;
    if (row.iv_pct != null && Number.isFinite(Number(row.iv_pct))) {
      group.ivWeighted += Number(row.iv_pct) * (size || 1);
      group.ivWeight += size || 1;
    }
    if (row.spot != null && Number.isFinite(Number(row.spot))) group.spot = Number(row.spot);
  }
  return [...groups.values()].sort((a, b) => a.time - b.time).map(group => ({
    ...group, iv: group.ivWeight ? group.ivWeighted / group.ivWeight : null,
  }));
}

function drawLine(ctx, points, key, projectY, color) {
  const valid = points.filter(point => Number.isFinite(point[key]));
  if (!valid.length) return;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 2;
  if (valid.length > 1) {
    ctx.beginPath();
    valid.forEach((point, index) => {
      const y = projectY(point[key]);
      if (index) ctx.lineTo(point.x, y);
      else ctx.moveTo(point.x, y);
    });
    ctx.stroke();
  }
  for (const point of valid) {
    ctx.beginPath();
    ctx.arc(point.x, projectY(point[key]), valid.length === 1 ? 5 : 2.8, 0, Math.PI * 2);
    ctx.fill();
  }
}

export function drawXynthFlow() {
  const canvas = document.getElementById("xynthFlowCanvas");
  const wrap = document.getElementById("xynthFlowChart");
  if (!canvas || !wrap) return;
  const width = Math.max(1, wrap.clientWidth);
  const height = Math.max(1, wrap.clientHeight);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, width, height);
  ctx.font = "11px Menlo, Consolas, monospace";
  const rows = aggregateXynthRows(latestPayload?.rows);
  plotted = [];
  if (!rows.length) {
    ctx.fillStyle = "#94A3B8";
    ctx.fillText("Waiting for Xynth trade data…", 18, 28);
    return;
  }

  const m = {left: 56, right: 60, top: 22, bottom: 31};
  const plotX = m.left, plotW = Math.max(80, width - m.left - m.right);
  const topY = m.top, topH = Math.round((height - m.top - m.bottom) * 0.68);
  const barY = topY + topH + 20, barH = Math.max(30, height - barY - m.bottom);
  const times = rows.map(row => row.time);
  const firstTime = Math.min(...times), lastTime = Math.max(...times);
  const timeSpan = Math.max(lastTime - firstTime, 1);
  const x = time => rows.length === 1 ? plotX + plotW / 2 : plotX + (time - firstTime) / timeSpan * plotW;
  const ivValues = rows.map(row => row.iv).filter(Number.isFinite);
  const spotValues = rows.map(row => row.spot).filter(Number.isFinite);
  const ivLow = ivValues.length ? Math.min(...ivValues) : 0;
  const ivHigh = ivValues.length ? Math.max(...ivValues) : 1;
  const ivPad = Math.max(1.5, (ivHigh - ivLow) * 0.15);
  const spotLow = spotValues.length ? Math.min(...spotValues) : 0;
  const spotHigh = spotValues.length ? Math.max(...spotValues) : 1;
  const spotPad = Math.max(0.25, (spotHigh - spotLow) * 0.15);
  const yIv = value => topY + topH - ((value - ivLow + ivPad) / (ivHigh - ivLow + 2 * ivPad)) * topH;
  const ySpot = value => topY + topH - ((value - spotLow + spotPad) / (spotHigh - spotLow + 2 * spotPad)) * topH;
  const barMax = Math.max(1, ...rows.map(row => Math.max(row.callSize, row.putSize)));

  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const y = topY + topH * i / 4;
    ctx.strokeStyle = "#1F2937";
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(plotX, y); ctx.lineTo(plotX + plotW, y); ctx.stroke();
    ctx.setLineDash([]);
    const ivTick = ivHigh + ivPad - i * (ivHigh - ivLow + 2 * ivPad) / 4;
    const spotTick = spotHigh + spotPad - i * (spotHigh - spotLow + 2 * spotPad) / 4;
    ctx.fillStyle = COLORS.cyan; ctx.textAlign = "right"; ctx.fillText(`${ivTick.toFixed(1)}%`, plotX - 8, y);
    ctx.fillStyle = "#38BDF8"; ctx.textAlign = "left"; ctx.fillText(`$${spotTick.toFixed(2)}`, plotX + plotW + 8, y);
  }
  ctx.strokeStyle = "#334155";
  ctx.beginPath(); ctx.moveTo(plotX, barY); ctx.lineTo(plotX + plotW, barY); ctx.stroke();
  ctx.fillStyle = "#94A3B8"; ctx.textAlign = "left"; ctx.fillText("TRADE SIZE", plotX + 3, barY + 12);
  ctx.textAlign = "right"; ctx.fillText(barMax.toLocaleString("en-US"), plotX + plotW, barY + 12);
  const bw = Math.max(2, Math.min(14, plotW / Math.max(rows.length, 1) * 0.32));
  plotted = rows.map(row => ({...row, x: x(row.time)}));
  for (const row of plotted) {
    const callH = row.callSize / barMax * (barH - 22);
    const putH = row.putSize / barMax * (barH - 22);
    ctx.fillStyle = COLORS.cyan; ctx.fillRect(row.x - bw - 1, barY + barH - callH, bw, callH);
    ctx.fillStyle = COLORS.orange; ctx.fillRect(row.x + 1, barY + barH - putH, bw, putH);
  }
  drawLine(ctx, plotted, "iv", yIv, COLORS.cyan);
  drawLine(ctx, plotted, "spot", ySpot, "#38BDF8");
  if (hoveredIndex >= 0 && plotted[hoveredIndex]) {
    ctx.strokeStyle = "#64748B"; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(plotted[hoveredIndex].x, topY); ctx.lineTo(plotted[hoveredIndex].x, barY + barH); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#94A3B8"; ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left"; ctx.fillText(rows[0].timeEt.split(" ").at(-1) + " ET", plotX, height - 8);
  ctx.textAlign = "right"; ctx.fillText(rows.at(-1).timeEt.split(" ").at(-1) + " ET", plotX + plotW, height - 8);
  if (rows.length === 1) {
    ctx.textAlign = "center"; ctx.fillStyle = "#94A3B8";
    ctx.fillText("One trade timestamp available · waiting for more time points", plotX + plotW / 2, topY + topH + 14);
  }
}

function bindEvents() {
  if (eventsBound) return;
  const canvas = document.getElementById("xynthFlowCanvas");
  const tooltip = document.getElementById("xynthFlowTooltip");
  if (!canvas || !tooltip) return;
  eventsBound = true;
  canvas.addEventListener("mousemove", event => {
    if (!plotted.length) return;
    const rect = canvas.getBoundingClientRect();
    const mouseX = event.clientX - rect.left;
    let nearest = 0;
    plotted.forEach((row, i) => { if (Math.abs(row.x - mouseX) < Math.abs(plotted[nearest].x - mouseX)) nearest = i; });
    hoveredIndex = nearest;
    const row = plotted[nearest];
    tooltip.textContent = `${row.timeEt} ET · ${row.count} trade(s)\nIV ${row.iv == null ? "—" : row.iv.toFixed(1) + "%"} · QQQ ${row.spot == null ? "—" : "$" + row.spot.toFixed(2)}\nCall size ${row.callSize.toLocaleString()} · Put size ${row.putSize.toLocaleString()}`;
    tooltip.hidden = false;
    tooltip.style.whiteSpace = "pre-line";
    tooltip.style.left = `${Math.min(Math.max(8, mouseX + 12), rect.width - tooltip.offsetWidth - 8)}px`;
    tooltip.style.top = `${Math.max(6, event.clientY - rect.top - tooltip.offsetHeight - 12)}px`;
    drawXynthFlow();
  });
  canvas.addEventListener("mouseleave", () => { hoveredIndex = -1; tooltip.hidden = true; drawXynthFlow(); });
}

export async function refreshXynthFlow() {
  if (pending) return;
  pending = true;
  try {
    const response = await fetch(apiUrl("/api/xynth-flow?ts=" + Date.now()));
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    latestPayload = payload;
    const session = document.getElementById("xynthFlowSession");
    const status = document.getElementById("xynthFlowStatus");
    if (session) session.textContent = payload.trading_date ? `Session ${payload.trading_date} ET` : "Waiting for trades";
    const checked = payload.last_checked_at ? new Date(payload.last_checked_at).toLocaleTimeString() : "—";
    const timestamps = aggregateXynthRows(payload.rows).length;
    if (status) status.textContent = `${payload.rows?.length || 0} visible trades · ${timestamps} time point${timestamps === 1 ? "" : "s"} · checked ${checked}${payload.error ? " · " + payload.error : ""}`;
    bindEvents();
    drawXynthFlow();
  } catch (error) {
    const status = document.getElementById("xynthFlowStatus");
    if (status) status.textContent = error.message;
    drawXynthFlow();
  } finally {
    pending = false;
  }
}
