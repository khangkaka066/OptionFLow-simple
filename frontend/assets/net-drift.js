import { apiUrl, COLORS } from "./config.js";
import { fmtHM } from "./utils.js";

const REFRESH_MS = 60_000;
let payload = null;
let error = "";
let loading = false;
let lastKey = "";
let lastFetchAt = 0;
let requestId = 0;
let tickerProvider = () => "QQQ";
let visibleRows = [];
let plotBox = null;

function dollars(value) {
  const n = Number(value) || 0;
  const abs = Math.abs(n);
  const unit = abs >= 1e6 ? "M" : abs >= 1e3 ? "K" : "";
  const divisor = unit === "M" ? 1e6 : unit === "K" ? 1e3 : 1;
  return `${n < 0 ? "−" : n > 0 ? "+" : ""}$${(abs / divisor).toFixed(unit ? 1 : 0)}${unit}`;
}

function sideSign(side) {
  const value = String(side || "").toUpperCase().replace(/[^A-Z]/g, "");
  if (["ASK", "ABOVEASK", "A", "AA"].includes(value)) return 1;
  if (["BID", "BELOWBID", "B", "BB"].includes(value)) return -1;
  return 0;
}

export function buildNetDriftRows(trades, intervalMinutes, expiry) {
  const intervalMs = intervalMinutes * 60_000;
  const filtered = (trades || []).filter(trade =>
    expiry === "ALL" || trade.expiry === trade.date
  ).sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  const times = filtered.map(trade => Date.parse(trade.time)).filter(Number.isFinite);
  if (!times.length) return { rows: [], unclassified: 0, trades: filtered.length };
  const first = Math.floor(Math.min(...times) / intervalMs) * intervalMs;
  const last = Math.floor(Math.max(...times) / intervalMs) * intervalMs;
  const rows = [];
  const byBucket = new Map();
  for (let time = first; time <= last; time += intervalMs) {
    const row = { time, call: 0, put: 0, net: 0, volume: 0, count: 0, spot: null };
    rows.push(row);
    byBucket.set(time, row);
  }
  let unclassified = 0;
  filtered.forEach(trade => {
    const time = Date.parse(trade.time);
    if (!Number.isFinite(time)) return;
    const row = byBucket.get(Math.floor(time / intervalMs) * intervalMs);
    if (!row) return;
    const premium = Number(trade.premium);
    if (!Number.isFinite(premium) || premium < 0) return;
    const cp = String(trade.cp || "").toUpperCase();
    if (cp !== "CALL" && cp !== "PUT") return;
    row.count += 1;
    const spot = Number(trade.spot);
    if (Number.isFinite(spot) && spot > 0) row.spot = spot;
    const sign = sideSign(trade.trade_side);
    if (!sign) { unclassified += 1; return; }
    if (cp === "CALL") row.call += sign * premium;
    else row.put -= sign * premium;
    const size = Number(trade.size);
    if (Number.isFinite(size) && size > 0) row.volume += (cp === "CALL" ? sign : -sign) * size;
    row.net = row.call + row.put;
  });
  let callRunning = 0, putRunning = 0, netRunning = 0, volumeRunning = 0;
  let lastSpot = filtered.map(trade => Number(trade.spot)).find(value => Number.isFinite(value) && value > 0) ?? null;
  rows.forEach(row => {
    callRunning += row.call;
    putRunning += row.put;
    netRunning += row.net;
    volumeRunning += row.volume;
    row.callRunning = callRunning;
    row.putRunning = putRunning;
    row.netRunning = netRunning;
    row.volumeRunning = volumeRunning;
    if (row.spot != null) lastSpot = row.spot;
    row.spot = lastSpot;
  });
  return { rows, unclassified, trades: filtered.length };
}

function drawLine(ctx, rows, field, x, y, color, width) {
  ctx.beginPath();
  rows.forEach((row, index) => {
    const px = x(index), py = y(row[field]);
    if (index === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = "round";
  ctx.stroke();
}

export function drawNetDrift() {
  const canvas = document.getElementById("netDriftCanvas");
  const wrap = document.getElementById("netDriftWrap");
  const status = document.getElementById("netDriftStatus");
  if (!canvas || !wrap) return;
  const width = Math.max(1, wrap.clientWidth);
  const height = Math.max(1, wrap.clientHeight);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, width, height);

  const note = error || (loading && !payload ? "Loading InsiderFinance public trades…" : "");
  if (note || !payload) {
    ctx.textAlign = "left";
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText(note || "Waiting for InsiderFinance data…", 20, 60);
    if (status) status.textContent = note;
    visibleRows = [];
    plotBox = null;
    return;
  }

  const interval = Number(document.getElementById("netDriftInterval")?.value || 1);
  const expiry = document.getElementById("netDriftExpiry")?.value || "ALL";
  const built = buildNetDriftRows(payload.trades, interval, expiry);
  const rows = built.rows;
  visibleRows = rows;
  const coverage = payload.session_count == null
    ? `${payload.returned_count} public trades`
    : `${payload.returned_count}/${payload.session_count} public trades`;
  const partial = payload.partial ? " · incomplete" : "";
  const sideScope = (payload.trade_sides || []).join("/") || "unknown side";
  const unclassified = built.unclassified ? ` · ${built.unclassified} unclassified` : "";
  if (status) status.textContent = `${payload.date || "--"} · ${coverage}${partial} · ${sideScope} feed · ${expiry}${unclassified}`;
  if (!rows.length) {
    ctx.textAlign = "left";
    ctx.fillStyle = COLORS.muted;
    ctx.font = "12px Menlo, Consolas, monospace";
    ctx.fillText(expiry === "0DTE" ? "No 0DTE trades in this public feed." : "No public trades for this date.", 20, 60);
    plotBox = null;
    return;
  }

  const callColor = "#32E63A";
  const putColor = "#FF3B46";
  const spotColor = "#2F83FF";
  const netColor = "#FACC15";
  const left = 78, right = 68;
  const premiumBox = {x: left, y: width < 720 ? 82 : 62, w: Math.max(50, width - left - right), h: Math.max(100, height * 0.55)};
  const volumeBox = {x: left, y: premiumBox.y + premiumBox.h + 43, w: premiumBox.w, h: Math.max(45, height - premiumBox.y - premiumBox.h - 104)};
  plotBox = {x: left, y: premiumBox.y, w: premiumBox.w, h: volumeBox.y + volumeBox.h - premiumBox.y};
  const x = index => left + (rows.length === 1 ? 0 : index * premiumBox.w / (rows.length - 1));
  const premiumValues = rows.flatMap(row => [row.callRunning, row.putRunning, row.netRunning]);
  const rawMin = Math.min(0, ...premiumValues), rawMax = Math.max(0, ...premiumValues);
  const premiumPad = Math.max(1, (rawMax - rawMin) * 0.08);
  const premiumMin = rawMin - premiumPad, premiumMax = rawMax + premiumPad;
  const premiumY = value => premiumBox.y + (premiumMax - value) / (premiumMax - premiumMin) * premiumBox.h;
  const spotValues = rows.map(row => Number(row.spot)).filter(Number.isFinite);
  const spotMin = Math.min(...spotValues), spotMax = Math.max(...spotValues);
  const spotPad = Math.max(0.2, (spotMax - spotMin) * 0.08);
  const priceY = value => premiumBox.y + (spotMax + spotPad - value) / (spotMax - spotMin + 2 * spotPad) * premiumBox.h;
  const volumeValues = rows.map(row => row.volumeRunning);
  const volumeMin = Math.min(0, ...volumeValues), volumeMax = Math.max(0, ...volumeValues);
  const volumePad = Math.max(1, (volumeMax - volumeMin) * 0.08);
  const volumeY = value => volumeBox.y + (volumeMax + volumePad - value) / (volumeMax - volumeMin + 2 * volumePad) * volumeBox.h;

  ctx.font = "11px Menlo, Consolas, monospace";
  ctx.textAlign = "left";
  const last = rows[rows.length - 1];
  const legend = [
    ["Calls", dollars(last.callRunning), callColor],
    ["Puts", dollars(last.putRunning), putColor],
    ["Net Premium", dollars(last.netRunning), netColor],
    ["Underlying", last.spot == null ? "—" : `$${last.spot.toFixed(2)}`, spotColor]
  ];
  const compactLegend = width < 720;
  const legendWidths = compactLegend ? [140, 140] : [145, 145, 185, 170];
  let legendX = compactLegend ? Math.max(18, (width - 280) / 2)
    : Math.max(18, (width - legendWidths.reduce((sum, item) => sum + item, 0)) / 2);
  legend.forEach(([label, value, color], index) => {
    if (compactLegend && index === 2) legendX = Math.max(18, (width - 280) / 2);
    const y = compactLegend && index >= 2 ? 48 : 28;
    ctx.fillStyle = color;
    ctx.fillRect(legendX, y - 9, 8, 8);
    ctx.fillStyle = "#E2E8F0";
    ctx.fillText(`${label} (${value})`, legendX + 13, y);
    legendX += legendWidths[index % legendWidths.length];
  });

  ctx.strokeStyle = "rgba(71, 85, 105, 0.42)";
  ctx.fillStyle = "#94A3B8";
  for (let i = 0; i <= 4; i++) {
    const py = premiumBox.y + i * premiumBox.h / 4;
    ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(left + premiumBox.w, py); ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(dollars(premiumMax - i * (premiumMax - premiumMin) / 4), left - 9, py + 4);
    if (spotValues.length) {
      ctx.textAlign = "left";
      ctx.fillStyle = spotColor;
      ctx.fillText(`$${(spotMax + spotPad - i * (spotMax - spotMin + 2 * spotPad) / 4).toFixed(2)}`, left + premiumBox.w + 8, py + 4);
      ctx.fillStyle = "#94A3B8";
    }
  }
  ctx.strokeStyle = "rgba(148, 163, 184, 0.65)";
  ctx.beginPath(); ctx.moveTo(left, premiumY(0)); ctx.lineTo(left + premiumBox.w, premiumY(0)); ctx.stroke();
  ctx.save();
  ctx.beginPath(); ctx.rect(premiumBox.x, premiumBox.y, premiumBox.w, premiumBox.h); ctx.clip();
  drawLine(ctx, rows, "callRunning", x, premiumY, callColor, 2.2);
  drawLine(ctx, rows, "putRunning", x, premiumY, putColor, 2.2);
  drawLine(ctx, rows, "netRunning", x, premiumY, netColor, 2.8);
  if (spotValues.length) drawLine(ctx, rows, "spot", x, priceY, spotColor, 1.8);
  ctx.restore();

  ctx.textAlign = "left";
  ctx.fillStyle = "#CBD5E1";
  ctx.font = "700 11px Menlo, Consolas, monospace";
  ctx.fillText("Directional volume (contracts)", left, volumeBox.y - 13);
  ctx.font = "10px Menlo, Consolas, monospace";
  ctx.strokeStyle = "rgba(71, 85, 105, 0.42)";
  for (let i = 0; i <= 2; i++) {
    const py = volumeBox.y + i * volumeBox.h / 2;
    ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(left + volumeBox.w, py); ctx.stroke();
    ctx.fillStyle = "#94A3B8";
    ctx.textAlign = "right";
    ctx.fillText(Math.round(volumeMax + volumePad - i * (volumeMax - volumeMin + 2 * volumePad) / 2).toLocaleString("en-US"), left - 9, py + 4);
  }
  ctx.save();
  ctx.beginPath(); ctx.rect(volumeBox.x, volumeBox.y, volumeBox.w, volumeBox.h); ctx.clip();
  rows.forEach((row, index) => {
    ctx.strokeStyle = row.volumeRunning < 0 ? "rgba(255, 59, 70, 0.16)" : "rgba(50, 230, 58, 0.16)";
    ctx.beginPath(); ctx.moveTo(x(index), volumeY(0)); ctx.lineTo(x(index), volumeY(row.volumeRunning)); ctx.stroke();
  });
  for (let index = 1; index < rows.length; index++) {
    ctx.strokeStyle = rows[index].volumeRunning < 0 ? putColor : callColor;
    ctx.lineWidth = 1.7;
    ctx.beginPath(); ctx.moveTo(x(index - 1), volumeY(rows[index - 1].volumeRunning));
    ctx.lineTo(x(index), volumeY(rows[index].volumeRunning)); ctx.stroke();
  }
  ctx.restore();
  ctx.strokeStyle = "rgba(148, 163, 184, 0.65)";
  ctx.beginPath(); ctx.moveTo(left, volumeY(0)); ctx.lineTo(left + volumeBox.w, volumeY(0)); ctx.stroke();
  ctx.textAlign = "center";
  ctx.fillStyle = "#94A3B8";
  const step = Math.max(1, Math.ceil(rows.length / 7));
  rows.forEach((row, index) => {
    if (index % step !== 0 && index !== rows.length - 1) return;
    ctx.fillText(fmtHM(new Date(row.time)), x(index), volumeBox.y + volumeBox.h + 19);
  });
}

export async function refreshNetDrift(ticker, force = false) {
  const date = document.getElementById("netDriftDate")?.value || "";
  const normalizedTicker = String(document.getElementById("netDriftTickerSelect")?.value || ticker || "QQQ").toUpperCase();
  const key = `${normalizedTicker}:${date}`;
  if (!force && key === lastKey && Date.now() - lastFetchAt < REFRESH_MS) return;
  const currentRequest = ++requestId;
  if (key !== lastKey) payload = null;
  lastKey = key;
  lastFetchAt = Date.now();
  loading = true;
  error = "";
  drawNetDrift();
  const params = new URLSearchParams({ ticker: normalizedTicker });
  if (date) params.set("date", date);
  try {
    const response = await fetch(apiUrl(`/api/net-drift?${params}`));
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || `HTTP ${response.status}`);
    if (currentRequest !== requestId) return;
    payload = result;
  } catch (exc) {
    if (currentRequest !== requestId) return;
    payload = null;
    error = `InsiderFinance: ${exc.message}`;
  } finally {
    if (currentRequest === requestId) {
      loading = false;
      drawNetDrift();
    }
  }
}

export function initNetDrift(getTicker) {
  tickerProvider = getTicker || tickerProvider;
  document.getElementById("netDriftTickerSelect")?.addEventListener("change", () => {
    refreshNetDrift(tickerProvider(), true);
  });
  document.getElementById("netDriftDate")?.addEventListener("change", () => {
    refreshNetDrift(tickerProvider(), true);
  });
  document.getElementById("netDriftLatest")?.addEventListener("click", () => {
    document.getElementById("netDriftDate").value = "";
    refreshNetDrift(tickerProvider(), true);
  });
  ["netDriftInterval", "netDriftExpiry"].forEach(id => {
    document.getElementById(id)?.addEventListener("change", drawNetDrift);
  });
  document.getElementById("netDriftCanvas")?.addEventListener("mousemove", event => {
    const tip = document.getElementById("netDriftTooltip");
    if (!tip || !plotBox || !visibleRows.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const index = Math.round((x - plotBox.x) / plotBox.w * (visibleRows.length - 1));
    if (index < 0 || index >= visibleRows.length) { tip.style.display = "none"; return; }
    const row = visibleRows[index];
    tip.textContent = `${fmtHM(new Date(row.time))} ET  ·  ${row.count} trades\n` +
      `Calls ${dollars(row.callRunning)}  ·  Puts ${dollars(row.putRunning)}\n` +
      `Net ${dollars(row.netRunning)}  ·  Volume ${Math.round(row.volumeRunning).toLocaleString("en-US")} contracts\n` +
      `Underlying ${row.spot == null ? "—" : "$" + row.spot.toFixed(2)}`;
    tip.style.display = "block";
    tip.style.left = `${Math.max(8, Math.min(x + 14, rect.width - tip.offsetWidth - 8))}px`;
    tip.style.top = `${Math.max(70, event.clientY - rect.top - tip.offsetHeight - 10)}px`;
  });
  document.getElementById("netDriftCanvas")?.addEventListener("mouseleave", () => {
    const tip = document.getElementById("netDriftTooltip");
    if (tip) tip.style.display = "none";
  });
}
