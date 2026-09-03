import { COLORS, COLOR_STORAGE_KEYS, EXPOSURE_CONFIG, LEGACY_DEFAULT_COLORS } from "./config.js";
import { normalizeHex, nyDateISO, validHex } from "./utils.js";
import { fetchDaySnapshot, fetchIntradaySnapshot } from "./api.js";
import { drawLevelsPanel as renderLevelsPanel, initLevelsCopyControls } from "./levels.js";
import { drawIvRank } from "./iv-rank.js";
import { drawOi, drawOiIv } from "./oi.js";
import { drawExposure } from "./exposure.js";
import { drawSkew, initSkewControls } from "./skew.js";
import { drawGexRibbon, heatState, initHeatTrackerControls } from "./heat-tracker.js";
import { drawFlow, drawFlowTracker, flowState, initFlowControls, initTrackerControls, trackerState } from "./flow-panels.js";

let latestState = null;
let lastChartKey = "";
let lastHeatLiveRefreshAt = 0;
const HEAT_LIVE_REFRESH_MS = 60_000;

function resetChartLocks() {
  lastChartKey = "";
  if (typeof flowState !== "undefined") flowState.autoFollow = true;
  if (typeof trackerState !== "undefined") trackerState.autoFollow = true;
  if (typeof heatState !== "undefined") heatState.autoFollow = true;
}

function loadAccentColors() {
  try {
    Object.entries(COLOR_STORAGE_KEYS).forEach(([key, storageKey]) => {
      const stored = localStorage.getItem(storageKey);
      if (!validHex(stored)) return;
      if (stored.toUpperCase() === LEGACY_DEFAULT_COLORS[key]) {
        localStorage.setItem(storageKey, COLORS[key]);
        return;
      }
      COLORS[key] = stored.toUpperCase();
    });
  } catch (_err) {}
}

function applyAccentColors() {
  document.documentElement.style.setProperty("--accent-call", COLORS.cyan);
  document.documentElement.style.setProperty("--accent-put", COLORS.orange);
  [
    ["callColor", "callHex", "cyan"],
    ["putColor", "putHex", "orange"],
    ["bullColor", "bullHex", "bull"],
    ["bearColor", "bearHex", "bear"]
  ].forEach(([colorId, hexId, key]) => {
    const colorInput = document.getElementById(colorId);
    const hexInput = document.getElementById(hexId);
    if (colorInput) colorInput.value = COLORS[key];
    if (hexInput) hexInput.value = COLORS[key];
  });
}

function setAccentColor(key, value) {
  const normalized = normalizeHex(value);
  if (!normalized) return false;
  COLORS[key] = normalized;
  try {
    localStorage.setItem(COLOR_STORAGE_KEYS[key] || ("qqqDashboardColor_" + key), normalized);
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


initFlowControls(() => latestState);

initTrackerControls(() => latestState);

const exposurePanelMetric = { gex: "net_gex", dex: "net_dex" };
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

initHeatTrackerControls(refreshHeatPanelPayload);
initExposureTabs();

initSkewControls(() => {
  if (latestState) drawAll(latestState);
});

const panelDayState = {
  levels: "live", ivRank: "live", skew: "live", oiIv: "live", oi: "live", exposureGex: "live", exposureDex: "live", heat: "live"
};
const panelPayload = {
  levels: null, ivRank: null, skew: null, oiIv: null, oi: null, exposureGex: null, exposureDex: null, heat: null
};
function drawLevelsPanel() {
  renderLevelsPanel({latestState, panelDayState, panelPayload});
}

function drawIvRankPanel() {
  if (panelDayState.ivRank === "live") {
    if (!latestState) return;
    drawIvRank(latestState.history || [], latestState.latest_summary || {});
  } else if (panelPayload.ivRank) {
    drawIvRank(panelPayload.ivRank.history || [], panelPayload.ivRank.latest_summary || {});
  }
}

function drawSkewPanel() {
  if (panelDayState.skew === "live") {
    if (!latestState) return;
    drawSkew(latestState.skew_by_strike || [], latestState.skew_summary || {}, latestState.skew_tenors || []);
  } else if (panelPayload.skew) {
    drawSkew(panelPayload.skew.skew_by_strike || panelPayload.skew.by_strike || [], panelPayload.skew.skew_summary || panelPayload.skew.latest_summary || {}, panelPayload.skew.skew_tenors || []);
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

function drawHeatPanel(state) {
  const primaryTicker = String(state?.latest_summary?.ticker || "QQQ").toUpperCase();
  const selectedTicker = String(heatState.ticker || primaryTicker).toUpperCase();
  if (panelDayState.heat === "live" && selectedTicker === primaryTicker) {
    if (!state) return;
    const liveSession = state.session ? {...state.session, history_snapshot_id: state.history_snapshot_id || null} : state.session;
    drawGexRibbon(state.gex_ribbon || [], state.points || [], state.latest_summary || {}, liveSession, state.candles || []);
  } else if (panelPayload.heat) {
    const payload = panelPayload.heat;
    const historySession = payload.session
      ? {...payload.session, history_snapshot_id: payload.history_snapshot_id || panelDayState.heat}
      : payload.session;
    drawGexRibbon(
      payload.gex_ribbon || [],
      payload.points || [],
      payload.latest_summary || {},
      historySession,
      payload.candles || []
    );
  }
}

async function refreshHeatPanelPayload(force = false) {
  if (!latestState) return;
  const selectedTicker = String(heatState.ticker || latestState.latest_summary?.ticker || "QQQ").toUpperCase();
  const primaryTicker = String(latestState.latest_summary?.ticker || "QQQ").toUpperCase();
  if (panelDayState.heat === "live" && selectedTicker === primaryTicker) {
    panelPayload.heat = null;
    drawHeatPanel(latestState);
    return;
  }
  const heatDateInput = document.getElementById("heatDate");
  const tradingDate = panelDayState.heat === "live"
    ? (latestState.session?.trading_date || heatDateInput?.value || nyDateISO())
    : panelDayState.heat;
  if (!tradingDate || tradingDate === "live") return;
  const isLiveSecondary = panelDayState.heat === "live" && selectedTicker !== primaryTicker;
  const now = Date.now();
  if (isLiveSecondary && panelPayload.heat && now - lastHeatLiveRefreshAt < HEAT_LIVE_REFRESH_MS) {
    drawHeatPanel(latestState);
    return;
  }
  const shouldForce = force && !isLiveSecondary;
  panelPayload.heat = await fetchIntradaySnapshot(selectedTicker, tradingDate, shouldForce);
  if (isLiveSecondary) lastHeatLiveRefreshAt = Date.now();
  drawHeatPanel(latestState);
}

function redrawPinnablePanels() {
  drawHeatPanel(latestState);
  drawLevelsPanel();
  drawIvRankPanel();
  drawSkewPanel();
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
      if (key === "heat") {
        try {
          await refreshHeatPanelPayload(true);
        } catch (err) {
          document.getElementById("status").textContent = "Heat Tracker error: " + err.message;
        }
      }
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
  bindPanelDatePicker("skew", "skewDatePicker", async value => {
    panelPayload.skew = await fetchDaySnapshot(latestState?.skew_summary?.ticker || latestState?.latest_summary?.ticker || "QQQ", "day:" + value);
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
  bindPanelDatePicker("heat", "heatDate", async value => {
    panelPayload.heat = await fetchIntradaySnapshot(heatState.ticker || latestState?.latest_summary?.ticker || "QQQ", value);
  });
}
initLevelsCopyControls();
initPanelDatePickers();

function drawAll(state) {
  drawFlow(state.points || [], state.session || null, state.candles || []);
  drawFlowTracker(state.points || [], state.session || null);
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
    heatState.ticker || "",
    state.latest_summary?.snapshot_utc || "",
    state.skew_summary?.snapshot_utc || "",
    state.skew_tenors?.length || 0,
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
  if (panelDayState.heat === "live") {
    const selectedTicker = String(heatState.ticker || state.latest_summary?.ticker || "QQQ").toUpperCase();
    const primaryTicker = String(state.latest_summary?.ticker || "QQQ").toUpperCase();
    if (selectedTicker !== primaryTicker) {
      refreshHeatPanelPayload(true).catch(err => {
        document.getElementById("status").textContent = "Heat Tracker error: " + err.message;
      });
    }
  }
}

applyAccentColors();
bindColorControls("callColor", "callHex", "cyan");
bindColorControls("putColor", "putHex", "orange");
bindColorControls("bullColor", "bullHex", "bull");
bindColorControls("bearColor", "bearHex", "bear");
["flowInterval", "flowMoneyness", "flowExpiry", "trackerInterval", "trackerMoneyness", "trackerExpiry", "trackerMode"].forEach(id => {
  document.getElementById(id)?.addEventListener("change", () => {
    resetChartLocks();
    trackerState.sessionKey = "";
    if (latestState) drawAll(latestState);
  });
});
document.getElementById("flowResetZoom")?.addEventListener("click", () => {
  resetChartLocks();
  flowState.autoFollow = true;
  flowState.viewX = [0, flowState.bucketCount || 1];
  if (latestState) drawAll(latestState);
});
document.getElementById("trackerResetZoom")?.addEventListener("click", () => {
  trackerState.sessionKey = "";
  trackerState.autoFollow = true;
  trackerState.viewX = [0, trackerState.bucketCount || 1];
  if (latestState) drawAll(latestState);
});
window.addEventListener("resize", () => {
  if (latestState) drawAll(latestState);
});
update();
setInterval(update, 10000);
