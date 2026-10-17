import { fetchGreekSurface } from "./api.js";

const REFRESH_MS = 60_000;
let getTicker = () => "QQQ";
let lastKey = "";
let lastFetchAt = 0;
let requestId = 0;

function formatValue(value) {
  const n = Number(value) || 0;
  const abs = Math.abs(n);
  if (abs < 500) return "$0";
  const unit = abs >= 1e9 ? [1e9, "B"] : abs >= 1e6 ? [1e6, "M"] : [1e3, "K"];
  return `${n < 0 ? "−" : ""}$${(abs / unit[0]).toFixed(abs / unit[0] >= 10 ? 0 : 1)}${unit[1]}`;
}

function render(surface, ticker) {
  const table = document.getElementById("heatLadderTable");
  const status = document.getElementById("heatLadderStatus");
  table.replaceChildren();
  const strikes = surface.strikes || [];
  const expiries = surface.expiries || [];
  if (!strikes.length || !expiries.length) {
    status.textContent = `${ticker} · No strike and expiration data for this date`;
    return;
  }
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  const strikeHeader = document.createElement("th");
  strikeHeader.textContent = "Strike";
  header.append(strikeHeader);
  expiries.forEach((expiry, expiryIndex) => {
    const th = document.createElement("th");
    th.textContent = String(expiry).slice(5);
    const dte = surface.dte?.[expiryIndex];
    if (Number.isFinite(dte)) {
      const small = document.createElement("small");
      small.textContent = `${dte}DTE`;
      th.append(small);
    }
    th.title = String(expiry);
    header.append(th);
  });
  head.append(header);
  const body = document.createElement("tbody");
  const spot = Number(surface.spot);
  let spotIndex = -1;
  if (Number.isFinite(spot)) {
    spotIndex = strikes.reduce((best, strike, index) =>
      Math.abs(Number(strike) - spot) < Math.abs(Number(strikes[best]) - spot) ? index : best, 0);
  }
  const maxAbs = Number(surface.rawMaxAbs) || 0;
  for (let strikeIndex = strikes.length - 1; strikeIndex >= 0; strikeIndex--) {
    const tr = document.createElement("tr");
    const strikeCell = document.createElement("th");
    const strike = Number(strikes[strikeIndex]);
    strikeCell.textContent = Number.isFinite(strike) ? strike.toFixed(2).replace(/\.00$/, "") : String(strikes[strikeIndex]);
    if (strikeIndex === spotIndex) {
      tr.classList.add("heat-ladder__spot-row");
      strikeCell.title = `Closest strike to spot $${spot.toFixed(2)}`;
    }
    tr.append(strikeCell);
    expiries.forEach((expiry, expiryIndex) => {
      const value = Number(surface.raw_values?.[expiryIndex]?.[strikeIndex]) || 0;
      const intensity = maxAbs ? Math.min(1, Math.sqrt(Math.abs(value) / maxAbs)) : 0;
      const cell = document.createElement("td");
      cell.textContent = formatValue(value);
      cell.style.backgroundColor = value > 0
        ? `rgba(34, 197, 94, ${0.08 + intensity * 0.67})`
        : value < 0
          ? `rgba(239, 68, 68, ${0.08 + intensity * 0.67})`
          : "rgba(71, 85, 105, 0.14)";
      cell.style.color = value > 0 ? "#DCFCE7" : value < 0 ? "#FEE2E2" : "#64748B";
      cell.title = `${ticker} ${expiry} $${strikeCell.textContent}: ${value.toLocaleString("en-US", {maximumFractionDigits: 2})} GEX`;
      tr.append(cell);
    });
    body.append(tr);
  }
  table.append(head, body);
  const dateLabel = surface.source_date && surface.source_date !== surface.date
    ? `${surface.date} · source ${surface.source_date}`
    : (surface.date || "Latest");
  status.textContent = `${ticker} · ${dateLabel} · Spot $${Number.isFinite(spot) ? spot.toFixed(2) : "—"} · ${strikes.length} strikes × ${expiries.length} expirations · Local options data`;
}

export async function refreshHeatLadder(force = false) {
  const ticker = String(getTicker() || "QQQ").trim().toUpperCase();
  const mode = document.getElementById("heatLadderMode").value;
  const range = document.getElementById("heatLadderRange").value;
  const date = document.getElementById("heatLadderDate").value;
  const key = [ticker, mode, range, date].join("|");
  if (!force && key === lastKey && Date.now() - lastFetchAt < REFRESH_MS) return;
  lastKey = key;
  lastFetchAt = Date.now();
  const currentRequest = ++requestId;
  const status = document.getElementById("heatLadderStatus");
  status.textContent = `Loading ${ticker} expiration heatmap…`;
  try {
    const surface = await fetchGreekSurface({ticker, date, greek: "gex", mode, range});
    if (currentRequest === requestId) render(surface, ticker);
  } catch (error) {
    if (currentRequest !== requestId) return;
    lastFetchAt = 0;
    document.getElementById("heatLadderTable").replaceChildren();
    status.textContent = `${ticker} · ${error.message || "Unable to load GEX data"}`;
  }
}

export function initHeatLadder(tickerGetter) {
  getTicker = tickerGetter;
  ["heatLadderMode", "heatLadderRange", "heatLadderDate", "heatTicker"].forEach(id => {
    document.getElementById(id)?.addEventListener("change", () => refreshHeatLadder(true));
  });
  document.getElementById("heatLadderLatest")?.addEventListener("click", () => {
    document.getElementById("heatLadderDate").value = "";
    refreshHeatLadder(true);
  });
}
