import { fmtLevel } from "./utils.js";

function buildLevelsLine(summary) {
  const ticker = String(summary?.ticker || "QQQ").toUpperCase();
  const basis = Number.isFinite(Number(summary?.futures_basis)) ? Number(summary.futures_basis) : 0;
  const adj = v => (v === null || v === undefined || Number.isNaN(Number(v))) ? v : Number(v) + basis;
  const isFutures = Boolean(summary?.futures_ticker);
  const tickSize = isFutures && Number.isFinite(Number(summary?.futures_tick_size)) ? Number(summary.futures_tick_size) : null;
  const label = isFutures ? String(summary.futures_ticker) : `$${ticker}`;
  const top = Array.isArray(summary?.top_abs_gex_levels) ? summary.top_abs_gex_levels : [];
  const gex = top.slice(0, 10).map(item => adj(item?.strike));
  while (gex.length < 10) gex.push(null);
  const parts = [
    `${label}: Call Resistance`, fmtLevel(adj(summary?.call_resistance), 0, tickSize),
    "Put Support", fmtLevel(adj(summary?.put_support), 0, tickSize),
    "HVL", fmtLevel(adj(summary?.spot), 2),
    "1D Min", fmtLevel(adj(summary?.one_day_min), 2),
    "1D Max", fmtLevel(adj(summary?.one_day_max), 2),
    "Call Resistance 0DTE", fmtLevel(adj(summary?.call_resistance), 0, tickSize),
    "Put Support 0DTE", fmtLevel(adj(summary?.put_support), 0, tickSize),
    "HVL 0DTE", fmtLevel(adj(summary?.gamma_flip), 0, tickSize),
    "Gamma Wall 0DTE", fmtLevel(adj(summary?.gamma_wall_abs), 0, tickSize),
  ];
  gex.forEach((strike, idx) => {
    parts.push(`GEX ${idx + 1}`, fmtLevel(strike, 0, tickSize));
  });
  return parts.join(", ");
}

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

export function initLevelsCopyControls() {
  initLevelsCopy("levelsExport", "levelsCopyBtn");
  initLevelsCopy("levelsExportB", "levelsCopyBtnB");
}

export function drawLevelsPanel({latestState, panelDayState, panelPayload}) {
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
