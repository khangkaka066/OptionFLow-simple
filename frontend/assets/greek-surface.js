import { fetchGreekSurface } from "./api.js";
import { apiUrl } from "./config.js";
import { nyDateISO } from "./utils.js";
import { Surface3DRenderer, formatCompact } from "./surface3d-renderer.js";

function el(id) { return document.getElementById(id); }

function normalizeDate(value) {
  if (!value) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const match = String(value).match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!match) return "";
  return `${match[3]}-${match[2].padStart(2, "0")}-${match[1].padStart(2, "0")}`;
}

/**
 * Creates one independent WebGL surface panel (state, SSE stream, fallback
 * polling, DOM wiring). Instantiated once for the "Exposure Surface" panel
 * (dollar GEX/DEX/VEX/CHEX, net/call/put) and once for the "Greek Surface"
 * panel (raw Delta/Gamma/Theta/Vega, single "raw" mode) — same mechanics,
 * different id prefix / greek options / mode options, matching quantdecay's
 * two-panel `/dashboard/oi` layout.
 *
 * @param {object} cfg
 * @param {string} cfg.idPrefix - DOM id prefix, e.g. "exposure" or "rawgreek".
 * @param {string} cfg.defaultGreek - initial `greek` param value.
 * @param {string} cfg.defaultMode - `mode` param value used on every request
 *   (fixed to "raw" for the greek panel, which has no mode selector).
 * @param {boolean} [cfg.hasModeSelect] - whether a `${idPrefix}Mode` <select>
 *   exists in the DOM to read from (exposure panel only).
 * @param {boolean} [cfg.hasRangeSelect] - whether a `${idPrefix}Range` <select>
 *   exists in the DOM to read from (exposure panel only).
 */
export function createSurfacePanel(cfg) {
  const { idPrefix, defaultGreek, defaultMode, hasModeSelect = false, hasRangeSelect = false } = cfg;
  const ids = {
    ticker: `${idPrefix}Ticker`,
    greek: `${idPrefix}Greek`,
    mode: `${idPrefix}Mode`,
    range: `${idPrefix}Range`,
    date: `${idPrefix}Date`,
    canvas: `${idPrefix}3dCanvas`,
    resetView: `${idPrefix}ResetView`,
    status: `${idPrefix}Status`,
    legendMin: `${idPrefix}LegendMin`,
    legendMax: `${idPrefix}LegendMax`,
  };

  const state = {
    ticker: "QQQ",
    greek: defaultGreek,
    mode: defaultMode,
    range: "",
    date: "live",
    renderer: null,
    loading: false,
    lastKey: "",
    stream: null,
    streamKey: "",
  };

  function currentDate(latestState) {
    if (state.date !== "live") return normalizeDate(state.date);
    return normalizeDate(latestState?.session?.trading_date) || nyDateISO();
  }

  function selectedParams(latestState, force = false) {
    return {
      ticker: state.ticker || latestState?.latest_summary?.ticker || "QQQ",
      date: currentDate(latestState),
      greek: state.greek,
      mode: state.mode,
      range: state.range,
      force,
    };
  }

  function setStatus(text) {
    const node = el(ids.status);
    if (node) node.textContent = text;
  }

  function setLegend(payload) {
    const maxAbs = Number(payload?.rawMaxAbs || 0);
    const minNode = el(ids.legendMin);
    const maxNode = el(ids.legendMax);
    if (minNode) minNode.textContent = formatCompact(-maxAbs);
    if (maxNode) maxNode.textContent = formatCompact(maxAbs);
  }

  function ensureRenderer() {
    if (state.renderer) return state.renderer;
    const container = el(ids.canvas);
    if (!container) return null;
    state.renderer = new Surface3DRenderer(container);
    return state.renderer;
  }

  function applySurfacePayload(payload) {
    const renderer = ensureRenderer();
    if (!renderer) return;
    renderer.setData(payload);
    setLegend(payload);
    const shape = `${payload.expiries?.length || 0}x${payload.strikes?.length || 0}`;
    const ts = payload.snapshot_utc ? new Date(payload.snapshot_utc).toLocaleTimeString() : "--";
    setStatus(`${payload.ticker} ${payload.greek.toUpperCase()} ${payload.mode} · ${shape} · ${ts}`);
  }

  async function fetchFallback(params, force = false) {
    if (state.loading) return;
    state.loading = true;
    setStatus("Loading surface...");
    try {
      applySurfacePayload(await fetchGreekSurface({ ...params, force }));
    } catch (err) {
      setStatus("Surface error: " + (err?.name ? err.name + ": " : "") + (err?.message || String(err)));
    } finally {
      state.loading = false;
    }
  }

  function connectStream(params) {
    const streamKey = JSON.stringify(params);
    if (streamKey === state.streamKey && state.stream) return;
    if (state.stream) state.stream.close();
    state.streamKey = streamKey;
    state.lastKey = "";
    const search = new URLSearchParams();
    search.set("panels", "greek_surface");
    search.set("ticker", params.ticker || "QQQ");
    if (params.date) search.set("date", params.date);
    search.set("greek", params.greek || defaultGreek);
    search.set("mode", params.mode || defaultMode);
    if (params.range) search.set("range", params.range);
    setStatus("Connecting stream...");
    if (!("EventSource" in window)) {
      fetchFallback(params, true);
      return;
    }
    const streamUrl = apiUrl("/api/stream?" + search.toString());
    let stream;
    try {
      stream = new EventSource(streamUrl);
    } catch (err) {
      return;
    }
    state.stream = stream;
    stream.addEventListener("greek_surface", event => {
      try {
        const payload = JSON.parse(event.data);
        applySurfacePayload(payload);
      } catch (err) {
        setStatus("Surface stream error: " + err.message);
      }
    });
    stream.addEventListener("error", () => {
      if (state.stream === stream) {
        stream.close();
        state.stream = null;
        // Clear streamKey so the next refresh tick reopens a fresh stream
        // instead of getting stuck on one-shot fallback data forever.
        state.streamKey = "";
        fetchFallback(params, true);
      }
    });
  }

  async function refresh(latestState, force = false) {
    const renderer = ensureRenderer();
    if (!renderer) return;
    const params = selectedParams(latestState, force);
    const key = JSON.stringify(params);
    if (!force && key === state.streamKey) return;
    state.streamKey = key;
    await fetchFallback(params, true);
    connectStream(params);
  }

  function init(getLatestState) {
    const ticker = el(ids.ticker);
    const greek = el(ids.greek);
    const mode = hasModeSelect ? el(ids.mode) : null;
    const range = hasRangeSelect ? el(ids.range) : null;
    const date = el(ids.date);
    if (date) {
      const today = normalizeDate(nyDateISO());
      if (today) date.value = today;
    }
    const syncAndRefresh = () => {
      state.ticker = ticker?.value || "QQQ";
      state.greek = greek?.value || defaultGreek;
      state.mode = mode?.value || defaultMode;
      state.range = range?.value || "";
      const normalizedDate = normalizeDate(date?.value);
      state.date = (!normalizedDate || normalizedDate === nyDateISO()) ? "live" : normalizedDate;
      state.lastKey = "";
      state.streamKey = "";
      refresh(getLatestState(), true);
    };
    [ticker, greek, mode, range, date].forEach(node => node?.addEventListener("change", syncAndRefresh));
    el(ids.resetView)?.addEventListener("click", () => state.renderer?.resetView());
    ensureRenderer();
  }

  return { state, init, refresh };
}
