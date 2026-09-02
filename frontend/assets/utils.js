import { COLORS } from "./config.js";

export function validHex(value) {
  return typeof value === "string" && /^#[0-9A-F]{6}$/i.test(value);
}

export function normalizeHex(value) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  const withHash = text.startsWith("#") ? text : "#" + text;
  return validHex(withHash) ? withHash.toUpperCase() : null;
}

export function hexToRgb(hex) {
  const clean = validHex(hex) ? hex.slice(1) : "000000";
  return {
    r: parseInt(clean.slice(0, 2), 16),
    g: parseInt(clean.slice(2, 4), 16),
    b: parseInt(clean.slice(4, 6), 16)
  };
}

export function rgbaFromHex(hex, alpha) {
  const rgb = hexToRgb(hex);
  return `rgba(${rgb.r},${rgb.g},${rgb.b},${alpha})`;
}

export function compact(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "NA";
  const sign = Number(v) < 0 ? "-" : "";
  const n = Math.abs(Number(v));
  if (n >= 1e9) return sign + (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return sign + (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return sign + (n / 1e3).toFixed(2) + "K";
  return sign + n.toFixed(0);
}

export function moneyM(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "NA";
  const sign = Number(v) < 0 ? "-" : "";
  return sign + "$" + (Math.abs(Number(v)) / 1e6).toFixed(1) + "M";
}

export function signedMoneyCompact(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "$NA";
  const n = Number(v);
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return sign + "$" + compact(Math.abs(n));
}

export function decimalsForTick(tickSize) {
  const text = String(tickSize || "");
  return text.includes(".") ? Math.min(6, text.split(".")[1].length) : 0;
}

export function fmtLevel(value, decimals = 0, tickSize = null) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "NA";
  let n = Number(value);
  const tick = Number(tickSize);
  if (Number.isFinite(tick) && tick > 0) {
    n = Math.round(n / tick) * tick;
    const tickDecimals = decimalsForTick(tick);
    if (decimals === 0 && tickDecimals > 0 && Math.abs(n - Math.round(n)) > 1e-9) {
      return n.toFixed(tickDecimals).replace(/0+$/, "").replace(/\.$/, "");
    }
  }
  if (decimals === 0) return String(Math.round(n));
  return n.toFixed(decimals);
}

export function timeET(v) {
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v || "NA");
  return d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }) + " ET";
}

export function plotTimeNY(v) {
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

export function baseLayout(height) {
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

export function rightLegend() {
  return {orientation: "h", x: 1, xanchor: "right", y: 1.13, bgcolor: "rgba(9,13,21,0.70)"};
}

export function paddedRange(values, minSpan, padRatio = 0.12) {
  const clean = values.map(Number).filter(Number.isFinite);
  if (!clean.length) return undefined;
  let lo = Math.min(...clean);
  let hi = Math.max(...clean);
  const center = (lo + hi) / 2;
  const span = Math.max(hi - lo, minSpan);
  const pad = span * padRatio;
  return [center - span / 2 - pad, center + span / 2 + pad];
}

export function lastFinite(values) {
  for (let i = values.length - 1; i >= 0; i--) {
    if (Number.isFinite(values[i])) return values[i];
  }
  return null;
}

export function nyMinutes(value) {
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

export function medianFinite(values) {
  const clean = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!clean.length) return null;
  const mid = Math.floor(clean.length / 2);
  return clean.length % 2 ? clean[mid] : (clean[mid - 1] + clean[mid]) / 2;
}

export function nyDateISO(date = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit"
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
