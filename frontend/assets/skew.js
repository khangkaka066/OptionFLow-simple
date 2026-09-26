import { COLORS } from "./config.js";
import { baseLayout, rgbaFromHex, rightLegend } from "./utils.js";

let skewExpirySelected = "nearest";
let skewSeriesVisible = {call: true, put: true, iv: true};
let skewExpiryOptionsKey = "";
let skewViewSelected = "smile";

export function drawSkew(rows, summary, tenors) {
  syncSkewContext(summary);
  const allTenors = tenors || [];
  if (allTenors.length >= 1) {
    syncSkewExpiryOptions(allTenors);
    const tenor = selectedTenor(allTenors);
    renderSkewStatCards(tenor);
    if (skewViewSelected === "term") {
      drawSkewTerm(allTenors);
    } else if (skewViewSelected === "table") {
      renderSkewTable(tenor);
    } else {
      drawSkewTenor(tenor, Number(summary?.spot));
    }
    return;
  }
  renderSkewStatCards(null);
  if (skewViewSelected === "smile") drawSkewSingle(rows, summary);
}

function selectedTenor(tenors) {
  if (skewExpirySelected !== "nearest") {
    const match = tenors.find(t => t.expiry === skewExpirySelected);
    if (match) return match;
  }
  return [...tenors].sort((a, b) => (a.dte ?? 0) - (b.dte ?? 0))[0];
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
    const ordered = [...tenors].sort((a, b) => (a.dte ?? 0) - (b.dte ?? 0));
    const options = ['<option value="nearest">Nearest</option>'];
    ordered.forEach(tenor => {
      const name = Number.isFinite(tenor.dte) ? `${tenor.dte}DTE` : "Expiry";
      const dateLabel = tenor.expiry
        ? new Date(tenor.expiry).toLocaleDateString("en-US", {month: "short", day: "2-digit"})
        : tenor.expiry;
      options.push(`<option value="${tenor.expiry}">${dateLabel} · ${name}</option>`);
    });
    sel.innerHTML = options.join("");
    const stillValid = skewExpirySelected === "nearest" || tenors.some(t => t.expiry === skewExpirySelected);
    if (!stillValid) skewExpirySelected = "nearest";
    sel.value = skewExpirySelected;
  }
}

function fmtSigned(value, decimals = 1, suffix = "") {
  if (!Number.isFinite(Number(value))) return "--";
  const n = Number(value);
  const sign = n > 0 ? "+" : n < 0 ? "" : "";
  return `${sign}${n.toFixed(decimals)}${suffix}`;
}

function setStatValueClass(el, value) {
  if (!el) return;
  el.classList.remove("positive", "negative");
  if (!Number.isFinite(Number(value))) return;
  if (Number(value) > 0) el.classList.add("positive");
  else if (Number(value) < 0) el.classList.add("negative");
}

// InsiderFinance's exact formulas aren't published; these follow the
// standard delta-bucket risk-reversal/butterfly convention computed
// server-side in scripts/skew_stats.py, not a numeric clone of their widget.
export function renderSkewStatCards(tenor) {
  const atmIvEl = document.getElementById("skewStatAtmIv");
  const atmStrikeEl = document.getElementById("skewStatAtmStrike");
  const skew25El = document.getElementById("skewStat25dSkew");
  const skew25SubEl = document.getElementById("skewStat25dSkewSub");
  const fly25El = document.getElementById("skewStat25dFly");
  const skew10El = document.getElementById("skewStat10dSkew");
  const slopeEl = document.getElementById("skewStatSlope");
  const termEl = document.getElementById("skewStatTermSlope");
  const termSubEl = document.getElementById("skewStatTermSub");
  if (!atmIvEl) return;

  if (!tenor) {
    [atmIvEl, skew25El, fly25El, skew10El, slopeEl, termEl].forEach(el => { if (el) el.textContent = "--"; });
    if (atmStrikeEl) atmStrikeEl.textContent = "Strike --";
    if (skew25SubEl) skew25SubEl.textContent = "--";
    if (termSubEl) termSubEl.textContent = "--";
    return;
  }

  atmIvEl.textContent = Number.isFinite(Number(tenor.atm_iv)) ? `${(Number(tenor.atm_iv) * 100).toFixed(1)}%` : "--";
  atmStrikeEl.textContent = Number.isFinite(Number(tenor.atm_strike)) ? `Strike ${Number(tenor.atm_strike).toFixed(0)}` : "Strike --";

  skew25El.textContent = fmtSigned(tenor.skew_25d, 1, " pts");
  setStatValueClass(skew25El, tenor.skew_25d);
  if (Number.isFinite(Number(tenor.skew_25d)) && tenor.put_25d && tenor.call_25d) {
    const tilt = Number(tenor.skew_25d) > 0 ? "Puts rich, downside bid" : "Calls rich, upside bid";
    skew25SubEl.textContent = `${tilt} · ${tenor.put_25d.strike.toFixed(0)}P ${(tenor.put_25d.iv * 100).toFixed(1)}% vs ${tenor.call_25d.strike.toFixed(0)}C ${(tenor.call_25d.iv * 100).toFixed(1)}%`;
  } else {
    skew25SubEl.textContent = "Not enough liquid strikes";
  }

  fly25El.textContent = fmtSigned(tenor.butterfly_25d, 1, " pts");
  setStatValueClass(fly25El, tenor.butterfly_25d);

  skew10El.textContent = fmtSigned(tenor.skew_10d, 1, " pts");
  setStatValueClass(skew10El, tenor.skew_10d);

  slopeEl.textContent = fmtSigned(tenor.skew_slope, 2);
  setStatValueClass(slopeEl, tenor.skew_slope);

  termEl.textContent = fmtSigned(tenor.term_slope, 1, " pts");
  setStatValueClass(termEl, tenor.term_slope);
  termSubEl.textContent = tenor.term_slope_label || "Term flat";
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

// One expiry at a time (InsiderFinance shows a single tenor via its own
// expiry dropdown, not several overlaid) with a filled put/call smile,
// an ATM reference line, and 25Δ wing markers when the backend found them.
function drawSkewTenor(tenor, spot) {
  if (!tenor) {
    renderSkewChart([], baseLayout(340));
    return;
  }
  const data = [];
  if (skewSeriesVisible.put && tenor.put?.strike?.length) {
    data.push({
      x: tenor.put.strike,
      y: tenor.put.iv.map(v => Number(v) * 100),
      type: "scatter", mode: "lines", name: "Puts", fill: "tozeroy",
      fillcolor: rgbaFromHex(COLORS.orange, 0.18),
      line: {color: COLORS.orange, width: 2},
      meta: {skew: {tenor: "Puts", side: "P", color: COLORS.orange}},
      hovertemplate: "<extra></extra>"
    });
  }
  if (skewSeriesVisible.call && tenor.call?.strike?.length) {
    data.push({
      x: tenor.call.strike,
      y: tenor.call.iv.map(v => Number(v) * 100),
      type: "scatter", mode: "lines", name: "Calls", fill: "tozeroy",
      fillcolor: rgbaFromHex(COLORS.cyan, 0.18),
      line: {color: COLORS.cyan, width: 2},
      meta: {skew: {tenor: "Calls", side: "C", color: COLORS.cyan}},
      hovertemplate: "<extra></extra>"
    });
  }
  if (skewSeriesVisible.iv && tenor.iv?.strike?.length) {
    data.push({
      x: tenor.iv.strike,
      y: tenor.iv.iv.map(v => Number(v) * 100),
      type: "scatter", mode: "lines", name: "IV", line: {color: COLORS.muted, width: 1, dash: "dot"},
      meta: {skew: {tenor: "IV", side: "IV", color: COLORS.muted}},
      hovertemplate: "<extra></extra>"
    });
  }

  const shapes = [];
  const annotations = [];
  if (Number.isFinite(spot)) {
    shapes.push({type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}});
  }
  if (Number.isFinite(Number(tenor.atm_iv))) {
    const atmPct = Number(tenor.atm_iv) * 100;
    shapes.push({type: "line", x0: 0, x1: 1, y0: atmPct, y1: atmPct, xref: "paper", yref: "y", line: {color: COLORS.yellow, dash: "dash", width: 1}});
  }
  [
    {point: tenor.put_25d, label: "25Δ"},
    {point: tenor.call_25d, label: "25Δ"},
  ].forEach(({point, label}) => {
    if (!point) return;
    data.push({
      x: [point.strike], y: [point.iv * 100], type: "scatter", mode: "markers",
      marker: {size: 9, color: "rgba(0,0,0,0)", line: {color: COLORS.muted, width: 1.5}},
      showlegend: false, hoverinfo: "skip",
    });
    annotations.push({x: point.strike, y: point.iv * 100, text: label, showarrow: false, yshift: 14, font: {color: COLORS.muted, size: 10}});
  });

  const layout = baseLayout(340);
  layout.margin = {l: 70, r: 28, t: 20, b: 48};
  layout.showlegend = false;
  layout.hovermode = "closest";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  layout.shapes = shapes;
  layout.annotations = annotations;
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
  layout.margin = {l: 70, r: 28, t: 20, b: 48};
  layout.legend = rightLegend();
  layout.hovermode = "closest";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  if (Number.isFinite(spot)) {
    layout.shapes = [{type: "line", x0: spot, x1: spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
  }
  renderSkewChart(data, layout);
}

function drawSkewTerm(tenors) {
  const ordered = [...tenors]
    .filter(t => Number.isFinite(Number(t.atm_iv)) && Number.isFinite(Number(t.dte)))
    .sort((a, b) => a.dte - b.dte);
  const data = [{
    x: ordered.map(t => t.dte),
    y: ordered.map(t => Number(t.atm_iv) * 100),
    type: "scatter", mode: "lines+markers", name: "ATM IV",
    line: {color: COLORS.yellow, width: 2},
    marker: {size: 7, color: COLORS.yellow},
    text: ordered.map(t => t.expiry),
    hovertemplate: "%{text}<br>%{x} DTE · %{y:.1f}% ATM IV<extra></extra>"
  }];
  const layout = baseLayout(340);
  layout.margin = {l: 70, r: 28, t: 20, b: 48};
  layout.showlegend = false;
  layout.hovermode = "x unified";
  layout.xaxis = {title: "Days to expiry", showgrid: false, zeroline: false, color: COLORS.muted, tickfont: {size: 12}};
  layout.yaxis = {title: "ATM IV %", showgrid: true, gridcolor: "rgba(31,41,55,0.72)", zeroline: false, color: COLORS.muted, ticksuffix: "%", tickfont: {size: 12}};
  Plotly.react("skewTerm", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true});
}

function renderSkewTable(tenor) {
  const container = document.getElementById("skewTable");
  if (!container) return;
  if (!tenor) {
    container.innerHTML = "<p style=\"color:#7C8798;\">No data.</p>";
    return;
  }
  const byStrike = new Map();
  (tenor.put?.strike || []).forEach((strike, i) => {
    byStrike.set(strike, {...(byStrike.get(strike) || {}), strike, putIv: tenor.put.iv[i]});
  });
  (tenor.call?.strike || []).forEach((strike, i) => {
    byStrike.set(strike, {...(byStrike.get(strike) || {}), strike, callIv: tenor.call.iv[i]});
  });
  const rows = [...byStrike.values()].sort((a, b) => a.strike - b.strike);
  const atmStrike = Number(tenor.atm_strike);
  const body = rows.map(row => {
    const isAtm = Number.isFinite(atmStrike) && Math.abs(row.strike - atmStrike) < 1e-6;
    return `<tr class="${isAtm ? "atm-row" : ""}">
      <td>${row.strike.toFixed(0)}</td>
      <td class="call-col">${Number.isFinite(row.callIv) ? (row.callIv * 100).toFixed(1) + "%" : "--"}</td>
      <td class="put-col">${Number.isFinite(row.putIv) ? (row.putIv * 100).toFixed(1) + "%" : "--"}</td>
    </tr>`;
  }).join("");
  container.innerHTML = `<table>
    <thead><tr><th>Strike</th><th>Call IV</th><th>Put IV</th></tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

function setSkewView(view, onRedraw) {
  skewViewSelected = view;
  document.querySelectorAll(".skew-view-tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });
  const smileView = document.getElementById("skewSmileView");
  const termView = document.getElementById("skewTermView");
  const tableView = document.getElementById("skewTableView");
  const seriesMenu = document.getElementById("skewSeries");
  if (smileView) smileView.hidden = view !== "smile";
  if (termView) termView.hidden = view !== "term";
  if (tableView) tableView.hidden = view !== "table";
  if (seriesMenu) seriesMenu.style.display = view === "smile" ? "" : "none";
  onRedraw?.();
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
  document.querySelectorAll(".skew-view-tab").forEach(btn => {
    btn.addEventListener("click", () => setSkewView(btn.dataset.view, onRedraw));
  });
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
