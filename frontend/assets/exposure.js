import { COLORS, EXPOSURE_CONFIG } from "./config.js";
import { moneyM, signedMoneyCompact } from "./utils.js";
import { reactWithBarFade } from "./plotly-utils.js";

function exposureLine(y, color, dash = "dash") {
  return {
    type: "line", x0: 0, x1: 1, y0: y, y1: y,
    xref: "paper", yref: "y",
    line: {color, dash, width: 1.2}
  };
}

function exposureLabel(text, y, color, side = "right") {
  return {
    x: side === "left" ? 0.015 : 0.985,
    y,
    xref: "paper",
    yref: "y",
    text,
    showarrow: false,
    xanchor: side === "left" ? "left" : "right",
    yanchor: "middle",
    font: {color, size: 11, family: "Menlo, Consolas, monospace"},
    bgcolor: "rgba(5,7,11,0.70)",
    borderpad: 2
  };
}

function updateExposureDealerBalance(id, rows, cfg) {
  const el = document.getElementById(id + "DealerBalance");
  if (!el || !cfg) return;
  const callTotal = rows.reduce((sum, r) => sum + (Number(r[cfg.callKey]) || 0), 0);
  const putTotal = rows.reduce((sum, r) => sum + (Number(r[cfg.putKey]) || 0), 0);
  const callAbs = Math.abs(callTotal);
  const putAbs = Math.abs(putTotal);
  const totalAbs = callAbs + putAbs;
  const callPct = totalAbs > 0 ? Math.max(2, (callAbs / totalAbs) * 100) : 50;
  const putPct = totalAbs > 0 ? Math.max(2, (putAbs / totalAbs) * 100) : 50;
  el.innerHTML = `
    <div class="dealer-balance-row">
      <div class="dealer-balance-label">DEALER BALANCE</div>
      <div class="dealer-balance-values">
        <span class="dealer-balance-call">${signedMoneyCompact(callTotal)}</span>
        <span>&nbsp;/&nbsp;</span>
        <span class="dealer-balance-put">${signedMoneyCompact(putTotal)}</span>
      </div>
    </div>
    <div class="dealer-balance-track" aria-label="Dealer balance">
      <span style="width:${callPct}%"></span>
      <span style="width:${putPct}%"></span>
    </div>`;
}

export function drawExposure(id, rows, key, summary) {
  const baseRows = rows
    .filter(r => Number.isFinite(Number(r.strike)) && Number.isFinite(Number(r[key])))
    .filter(r => Number(r[key]) !== 0 || Number(r.call_oi) > 0 || Number(r.put_oi) > 0);
  // Strikes whose exposure is negligible next to the biggest strike just
  // clutter the row axis with invisible bars — keep only the ones with a
  // real share of the total so the panel stays cropped to what matters.
  const fullMaxAbs = Math.max(1, ...baseRows.map(r => Math.abs(Number(r[key]) || 0)));
  const cleanRows = baseRows
    .filter(r => Math.abs(Number(r[key]) || 0) >= fullMaxAbs * 0.03)
    .sort((a, b) => Number(a.strike) - Number(b.strike));
  const values = cleanRows.map(r => Number(r[key]) || 0);
  const strikes = cleanRows.map(r => Number(r.strike));
  const maxAbs = Math.max(1, ...values.map(v => Math.abs(v)));
  const positiveColor = COLORS.cyan;
  const negativeColor = COLORS.orange;
  const cfg = EXPOSURE_CONFIG[key];
  updateExposureDealerBalance(id, baseRows, cfg);
  const label = cfg.label;
  const callKey = cfg.callKey;
  const putKey = cfg.putKey;
  const buildExposureTrace = (sideRows, color, name, fadeSide) => ({
    x: sideRows.map(r => Number(r[key]) || 0),
    y: sideRows.map(r => Number(r.strike)),
    type: "bar",
    orientation: "h",
    width: 0.52,
    marker: {
      color,
      opacity: 1,
      line: {width: 0}
    },
    customdata: sideRows.map(r => [
      moneyM(Number(r[key]) || 0),
      moneyM(Number(r[callKey]) || 0),
      moneyM(Number(r[putKey]) || 0)
    ]),
    hoverlabel: {bgcolor: "#111827", bordercolor: "#374151", font: {color}},
    hovertemplate: (
      `<b><span style='color:${color}'>$%{y} net %{customdata[0]}</span></b><br>` +
      `<span style='color:${COLORS.cyan}'>Net Call %{customdata[1]}</span><br>` +
      `<span style='color:${COLORS.orange}'>Net Put %{customdata[2]}</span><extra></extra>`
    ),
    meta: {fadeSide},
    name
  });
  const data = [
    buildExposureTrace(cleanRows.filter(r => Number(r[key]) >= 0), positiveColor, label + " +", "pos"),
    buildExposureTrace(cleanRows.filter(r => Number(r[key]) < 0), negativeColor, label + " -", "neg"),
  ];
  const yMin = Math.min(...strikes, Number(summary?.spot || Infinity)) - 0.8;
  const yMax = Math.max(...strikes, Number(summary?.spot || -Infinity)) + 0.8;
  const shapes = [
    {type: "line", x0: 0, x1: 0, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.cyan, dash: "dot", width: 1.2}},
  ];
  const annotations = [];

  if (summary?.spot) {
    shapes.push(exposureLine(Number(summary.spot), COLORS.spot, "dot"));
    annotations.push(exposureLabel("SPOT " + Number(summary.spot).toFixed(2), Number(summary.spot), COLORS.spot, "right"));
  }

  if (summary?.call_resistance) {
    shapes.push(exposureLine(Number(summary.call_resistance), COLORS.cyan, "dash"));
    annotations.push(exposureLabel("CALL RESISTANCE " + Number(summary.call_resistance).toFixed(0), Number(summary.call_resistance), COLORS.cyan, "right"));
  }
  if (summary?.put_support) {
    shapes.push(exposureLine(Number(summary.put_support), COLORS.orange, "dash"));
    annotations.push(exposureLabel("PUT SUPPORT " + Number(summary.put_support).toFixed(0), Number(summary.put_support), COLORS.orange, "left"));
  }

  const wallAbs = summary?.[cfg.wallAbsKey];
  if (wallAbs) {
    shapes.push(exposureLine(Number(wallAbs), "#A78BFA", "dashdot"));
    annotations.push(exposureLabel(cfg.wallLabel + " " + Number(wallAbs).toFixed(0), Number(wallAbs), "#A78BFA", "right"));
  }
  const flip = summary?.[cfg.flipKey];
  if (flip) {
    shapes.push(exposureLine(Number(flip), "#C084FC", "dot"));
    annotations.push(exposureLabel(cfg.flipLabel + " " + Number(flip).toFixed(2), Number(flip), "#C084FC", "right"));
  }

  const layout = {
    paper_bgcolor: COLORS.bg,
    plot_bgcolor: "#000000",
    font: {color: COLORS.text, family: "Menlo, Consolas, monospace"},
    margin: {l: 58, r: 42, t: 12, b: 10},
    height: 500,
    uirevision: "keep-user-zoom",
    dragmode: "pan",
    hovermode: "closest",
    showlegend: false,
    bargap: 0,
    xaxis: {visible: false, zeroline: false, range: [-maxAbs * 1.18, maxAbs * 1.18], fixedrange: false},
    yaxis: {
      showgrid: false,
      zeroline: false,
      color: COLORS.muted,
      dtick: 1,
      range: [yMin, yMax],
      fixedrange: false,
      title: null
    },
    shapes,
    annotations
  };
  reactWithBarFade(id, data, layout, {displayModeBar: false, scrollZoom: true, responsive: true}, "exposure");
}
