import { COLORS } from "./config.js";
import { baseLayout, rightLegend } from "./utils.js";

let skewExpirySelected = "nearest";
let skewSeriesVisible = {call: true, put: true, iv: true};
let skewExpiryOptionsKey = "";

const TENOR_COLORS = [COLORS.orange, COLORS.cyan, COLORS.green, "#7C3AED", "#F472B6", COLORS.yellow];

export function drawSkew(rows, summary, tenors) {
  syncSkewContext(summary);
  if ((tenors || []).length >= 1) {
    drawSkewTenors(tenors, Number(summary?.spot));
    return;
  }
  drawSkewSingle(rows, summary);
}

function syncSkewContext(summary) {
  const dateNode = document.getElementById("skewDate");
  const tickerNode = document.getElementById("skewTicker");
  const rawDate = String(summary?.requested_snapshot_date || summary?.effective_snapshot_date || "");
  if (dateNode) {
    const parsed = rawDate ? new Date(`${rawDate.slice(0, 10)}T00:00:00`) : null;
    dateNode.textContent = parsed && !Number.isNaN(parsed.getTime())
      ? parsed.toLocaleDateString("en-GB")
      : "--";
  }
  if (tickerNode) tickerNode.textContent = String(summary?.ticker || "QQQ").toUpperCase();
}

function syncSkewExpiryOptions(tenors) {
  const sel = document.getElementById("skewExpiry");
  if (!sel) return;
  const key = tenors.map(t => t.expiry).join("|");
  if (key !== skewExpiryOptionsKey) {
    skewExpiryOptionsKey = key;
    const options = ['<option value="nearest">Nearest</option>'];
    tenors.forEach(tenor => {
      const name = Number.isFinite(tenor.dte) ? `${tenor.dte}DTE` : "Expiry";
      const dateLabel = tenor.expiry
        ? new Date(tenor.expiry).toLocaleDateString("en-US", {month: "short", day: "2-digit"})
        : tenor.expiry;
      options.push(`<option value="${tenor.expiry}">${name} · ${dateLabel}</option>`);
    });
    sel.innerHTML = options.join("");
    const stillValid = skewExpirySelected === "nearest" || tenors.some(t => t.expiry === skewExpirySelected);
    if (!stillValid) skewExpirySelected = "nearest";
    sel.value = skewExpirySelected;
  }
}

function nearestSkewPoint(trace, strike) {
  const xs = trace.x || [];
  const ys = trace.y || [];
  let best = null;
  xs.forEach((rawX, index) => {
    const x = Number(rawX);
    const y = Number(ys[index]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    const distance = Math.abs(x - strike);
    if (!best || distance < best.distance) best = {x, y, distance};
  });
  return best;
}

function skewStrikeStep(traces) {
  const xs = [...new Set(traces.flatMap(trace => (trace.x || []).map(Number).filter(Number.isFinite)))].sort((a, b) => a - b);
  const gaps = xs.slice(1).map((x, index) => x - xs[index]).filter(gap => gap > 0);
  if (!gaps.length) return 1;
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)] || 1;
}

function skewTooltipHtml(traces, strike) {
  const groups = new Map();
  const tolerance = Math.max(0.01, skewStrikeStep(traces) * 0.45);
  traces.forEach(trace => {
    if (!trace.meta || !trace.meta.skew) return;
    const point = nearestSkewPoint(trace, strike);
    if (!point || point.distance > tolerance) return;
    const {tenor, side, color} = trace.meta.skew;
    if (!groups.has(tenor)) groups.set(tenor, {color, values: []});
    groups.get(tenor).values.push({side, value: point.y});
  });
  const order = {C: 0, P: 1, IV: 2};
  const lines = [];
  groups.forEach((group, tenor) => {
    group.values
      .sort((a, b) => order[a.side] - order[b.side])
      .forEach(item => lines.push(`<div><span style="color:${group.color}">${tenor} ${item.side}</span> <span>${item.value.toFixed(1)}% IV</span></div>`));
  });
  return lines.length
    ? `<div class="strike">strike ${Number(strike).toFixed(Number.isInteger(Number(strike)) ? 0 : 1)}</div>${lines.join("")}`
    : "";
}

function installSkewHover(data, baseShapes) {
  const gd = document.getElementById("skew");
  const tooltip = document.getElementById("skewTooltip");
  if (!gd || !tooltip || !gd.on) return;
  if (typeof gd.removeAllListeners === "function") {
    gd.removeAllListeners("plotly_hover");
    gd.removeAllListeners("plotly_unhover");
  }
  const restore = () => {
    tooltip.style.display = "none";
    Plotly.relayout(gd, {shapes: baseShapes});
  };
  gd.on("plotly_hover", event => {
    const point = (event.points || []).find(item => Number.isFinite(Number(item.x)) && Number.isFinite(Number(item.y)));
    if (!point) return;
    const strike = Number(point.x);
    const iv = Number(point.y);
    const html = skewTooltipHtml(data, strike);
    if (!html) return;
    tooltip.innerHTML = html;
    const box = gd.parentElement.getBoundingClientRect();
    const width = 196;
    const height = Math.max(62, 27 + html.split("<div").length * 18);
    const rawLeft = Number(event.event?.clientX || box.left) - box.left + 14;
    const rawTop = Number(event.event?.clientY || box.top) - box.top + 14;
    tooltip.style.left = `${Math.max(8, Math.min(rawLeft, box.width - width - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(rawTop, box.height - height - 8))}px`;
    tooltip.style.display = "block";
    const step = skewStrikeStep(data);
    const hoverShapes = [
      ...baseShapes,
      {type: "rect", x0: strike - step * 0.34, x1: strike + step * 0.34, y0: 0, y1: 1, xref: "x", yref: "paper", fillcolor: "rgba(148,163,184,0.10)", line: {width: 0}, layer: "below"},
      {type: "line", x0: strike, x1: strike, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: "#64748B", width: 1, dash: "dot"}},
      {type: "line", x0: 0, x1: 1, y0: iv, y1: iv, xref: "paper", yref: "y", line: {color: "#64748B", width: 1, dash: "dot"}}
    ];
    Plotly.relayout(gd, {shapes: hoverShapes});
  });
  gd.on("plotly_unhover", restore);
}

function renderSkewChart(data, layout) {
  const baseShapes = (layout.shapes || []).map(shape => ({...shape}));
  return Plotly.react("skew", data, layout, {
    displayModeBar: false, scrollZoom: true, responsive: true
  }).then(() => installSkewHover(data, baseShapes));
}

function drawSkewTenors(tenors, spot) {
  syncSkewExpiryOptions(tenors);
  const tenorsToShow = skewExpirySelected === "nearest"
    ? tenors
    : tenors.filter(t => t.expiry === skewExpirySelected);
  const labels = [];
  const data = [];
  tenorsToShow.forEach((tenor, i) => {
    const color = tenor.color || TENOR_COLORS[i % TENOR_COLORS.length];
    const name = Number.isFinite(tenor.dte) ? `${tenor.dte}DTE` : tenor.expiry;
    if (Number.isFinite(Number(tenor.atm_iv))) {
      labels.push(`<span style="color:${color}">${name}</span> <span style="color:${COLORS.muted}">${(Number(tenor.atm_iv) * 100).toFixed(1)}%</span>`);
    }
    const sides = [
      {key: "call", label: "C", dash: "solid"},
      {key: "put", label: "P", dash: "dash"},
      {key: "iv", label: "IV", dash: "dot"}
    ].filter(side => skewSeriesVisible[side.key]);
    sides.forEach(side => {
      const curve = tenor[side.key];
      if (!curve || !curve.strike || !curve.strike.length) return;
      data.push({
        x: curve.strike,
        y: curve.iv.map(v => Number(v) * 100),
        type: "scatter",
        mode: "lines",
        name: `${name} ${side.label}`,
        showlegend: false,
        meta: {skew: {tenor: name, side: side.label, color}},
        line: {color, width: 2, dash: side.dash},
        hovertemplate: "<extra></extra>"
      });
    });
  });
  const layout = baseLayout(340);
  layout.margin = {l: 70, r: 28, t: 55, b: 48};
  layout.showlegend = false;
  layout.hovermode = "closest";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  if (Number.isFinite(spot)) {
    layout.shapes = [{type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
  }
  if (labels.length) {
    layout.annotations = [{x: 0, y: 1.16, xref: "paper", yref: "paper", text: labels.join(" · "), showarrow: false, font: {color: COLORS.muted, size: 11}, xanchor: "left"}];
  }
  renderSkewChart(data, layout);
}

function drawSkewSingle(rows, summary) {
  const spot = Number(summary?.spot);
  const clean = rows
    .filter(r => Number.isFinite(Number(r.strike)))
    .sort((a, b) => Number(a.strike) - Number(b.strike));
  const atmRow = clean.reduce((best, row) => {
    if (!Number.isFinite(spot)) return best;
    if (!best) return row;
    return Math.abs(Number(row.strike) - spot) < Math.abs(Number(best.strike) - spot) ? row : best;
  }, null);
  const atmStrike = atmRow ? Number(atmRow.strike) : spot;
  // Strikes with no open interest and no traded volume have no real quote behind
  // them; their IV is a stale/extrapolated BSM back-solve, not a market price.
  const hasLiquidity = (row, side) => {
    const oi = Number(row[side + "_oi"]) || 0;
    const vol = Number(row[side + "_volume"]) || 0;
    return oi > 0 || vol > 0;
  };
  const atmValues = [];
  if (atmRow && hasLiquidity(atmRow, "call") && Number.isFinite(Number(atmRow.call_iv_pct))) atmValues.push(Number(atmRow.call_iv_pct));
  if (atmRow && hasLiquidity(atmRow, "put") && Number.isFinite(Number(atmRow.put_iv_pct))) atmValues.push(Number(atmRow.put_iv_pct));
  const atmIv = atmValues.length ? atmValues.reduce((a, b) => a + b, 0) / atmValues.length : NaN;
  const atmPoint = Number.isFinite(atmIv) ? [{strike: atmStrike, iv: atmIv}] : [];
  const callRows = [
    ...atmPoint,
    ...clean
      .filter(r => Number(r.strike) > atmStrike && hasLiquidity(r, "call") && Number.isFinite(Number(r.call_iv_pct)))
      .map(r => ({strike: Number(r.strike), iv: Number(r.call_iv_pct)}))
  ];
  const putRows = [
    ...clean
      .filter(r => Number(r.strike) < atmStrike && hasLiquidity(r, "put") && Number.isFinite(Number(r.put_iv_pct)))
      .map(r => ({strike: Number(r.strike), iv: Number(r.put_iv_pct)})),
    ...atmPoint
  ];
  const smoothIvRows = curve => {
    const cleanCurve = curve
      .filter(r => Number.isFinite(Number(r.strike)) && Number.isFinite(Number(r.iv)))
      .sort((a, b) => Number(a.strike) - Number(b.strike));
    return cleanCurve.map((row, i) => {
      const neighbors = cleanCurve.slice(Math.max(0, i - 1), Math.min(cleanCurve.length, i + 2)).map(r => Number(r.iv));
      const sorted = [...neighbors].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      const jumpLimit = Math.max(3.0, Math.abs(median) * 0.35);
      const cleaned = Math.abs(Number(row.iv) - median) > jumpLimit ? median : Number(row.iv);
      const prev = i > 0 ? Number(cleanCurve[i - 1].iv) : cleaned;
      const next = i < cleanCurve.length - 1 ? Number(cleanCurve[i + 1].iv) : cleaned;
      return {strike: Number(row.strike), iv: (prev * 0.2 + cleaned * 0.6 + next * 0.2)};
    });
  };
  const smoothCallRows = smoothIvRows(callRows);
  const smoothPutRows = smoothIvRows(putRows);
  const data = [];
  if (skewSeriesVisible.call) {
    data.push({
      x: smoothCallRows.map(r => r.strike),
      y: smoothCallRows.map(r => r.iv),
      type: "scatter",
      mode: "lines",
      name: "Calls",
      line: {color: COLORS.cyan, width: 2},
      meta: {skew: {tenor: "0DTE", side: "C", color: COLORS.cyan}},
      hovertemplate: "<extra></extra>"
    });
  }
  if (skewSeriesVisible.put) {
    data.push({
      x: smoothPutRows.map(r => r.strike),
      y: smoothPutRows.map(r => r.iv),
      type: "scatter",
      mode: "lines",
      name: "Puts",
      line: {color: COLORS.orange, width: 2, dash: "dash"},
      meta: {skew: {tenor: "0DTE", side: "P", color: COLORS.orange}},
      hovertemplate: "<extra></extra>"
    });
  }
  const layout = baseLayout(340);
  layout.margin = {l: 70, r: 28, t: 55, b: 48};
  layout.legend = rightLegend();
  layout.hovermode = "closest";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  if (Number.isFinite(spot)) {
    layout.shapes = [{type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
  }
  renderSkewChart(data, layout);
}

export function initSkewControls(onRedraw) {
  const expirySel = document.getElementById("skewExpiry");
  const seriesBtn = document.getElementById("skewSeriesBtn");
  const seriesMenu = document.getElementById("skewSeriesMenu");
  if (expirySel) {
    expirySel.addEventListener("change", () => {
      skewExpirySelected = expirySel.value;
      onRedraw?.();
    });
  }
  const syncSeriesLabel = () => {
    const active = ["call", "put", "iv"]
      .filter(key => skewSeriesVisible[key])
      .map(key => key === "call" ? "Calls" : key === "put" ? "Puts" : "IV");
    if (seriesBtn) seriesBtn.textContent = active.length ? active.join(" · ") : "None";
  };
  syncSeriesLabel();
  if (seriesBtn && seriesMenu) {
    seriesBtn.addEventListener("click", (evt) => {
      evt.stopPropagation();
      seriesMenu.hidden = !seriesMenu.hidden;
    });
    seriesMenu.querySelectorAll("input[type=checkbox]").forEach(box => {
      box.addEventListener("change", () => {
        skewSeriesVisible[box.dataset.series] = box.checked;
        syncSeriesLabel();
        onRedraw?.();
      });
    });
    document.addEventListener("click", (evt) => {
      if (!seriesMenu.hidden && !seriesMenu.contains(evt.target) && evt.target !== seriesBtn) {
        seriesMenu.hidden = true;
      }
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") seriesMenu.hidden = true;
    });
  }
}
