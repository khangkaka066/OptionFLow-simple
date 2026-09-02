import { COLORS } from "./config.js";
import { baseLayout } from "./utils.js";

export function drawIvRank(history, summary) {
  const rows = (history || [])
    .filter(r => Number.isFinite(Number(r.avg_iv_pct)) && Number.isFinite(Number(r.spot)))
    .sort((a, b) => new Date(a.snapshot_utc || a.snapshot_vn) - new Date(b.snapshot_utc || b.snapshot_vn));
  drawIvRankChart(rows);
}

function drawIvRankChart(rows) {
  if (!rows.length) {
    const layout = baseLayout(340);
    layout.margin.t = 55;
    layout.annotations = [{
      x: 0.5, y: 0.5, xref: "paper", yref: "paper", showarrow: false,
      text: "Not enough local daily snapshots yet for IV Rank",
      font: {color: COLORS.muted, size: 13}
    }];
    Plotly.react("ivrank", [], layout, {displayModeBar: false, scrollZoom: false, responsive: true});
    return;
  }
  const x = rows.map(r => r.snapshot_vn || r.snapshot_utc);
  const y = rows.map(r => Number.isFinite(Number(r.iv_rank_pct)) ? Number(r.iv_rank_pct) : null);
  const hasRank = y.some(v => v !== null);
  const spotRaw = rows.map(r => Number(r.spot));
  const spotMin = Math.min(...spotRaw);
  const spotMax = Math.max(...spotRaw);
  const spotSpan = spotMax - spotMin || 1;
  const spotNorm = spotRaw.map(v => ((v - spotMin) / spotSpan) * 100);
  const tickText = x.map(v => new Date(v).toLocaleDateString("en-US", {month: "short", day: "2-digit"}));
  const current = rows[rows.length - 1];
  const formatHeader = row => hasRank
    ? `rank ${Number(row.iv_rank_pct ?? 0).toFixed(1)}% · IV ${Number(row.avg_iv_pct || 0).toFixed(1)}% · $${Number(row.spot || 0).toFixed(2)}`
    : `rank warming up · IV ${Number(row.avg_iv_pct || 0).toFixed(1)}% · $${Number(row.spot || 0).toFixed(2)}`;
  const lastIdx = rows.length - 1;
  const data = [
    {
      x, y, type: "scatter", mode: "lines", name: "IV Rank",
      line: {color: COLORS.cyan, width: 2.5}, connectgaps: true,
      customdata: rows.map(r => [Number(r.avg_iv_pct), Number(r.spot), r.iv_rank_pct]),
      hovertemplate: "%{x|%b %d}<extra></extra>"
    },
    {
      x, y: spotNorm, type: "scatter", mode: "lines", name: "Spot",
      line: {color: COLORS.spot, width: 1.25, dash: "dot"}, hoverinfo: "skip"
    },
    {
      x: [x[lastIdx]], y: [y[lastIdx]], type: "scatter", mode: "markers+text", showlegend: false,
      marker: {color: COLORS.cyan, size: 7},
      text: [Number(spotRaw[lastIdx]).toFixed(2)],
      textposition: "middle right",
      textfont: {color: COLORS.cyan, size: 11},
      hoverinfo: "skip"
    }
  ];
  const layout = baseLayout(340);
  layout.margin.t = 55;
  layout.margin.r = 45;
  layout.showlegend = false;
  layout.dragmode = false;
  // category axis (not date) so we can pad range in category units and
  // leave room for the end-of-line price label without it clipping at the
  // plot edge.
  layout.xaxis = {
    type: "category", showgrid: false, zeroline: false, color: COLORS.muted,
    tickmode: "array", tickvals: x, ticktext: tickText, tickangle: 0, fixedrange: true,
    range: [-0.5, rows.length - 0.35]
  };
  layout.yaxis = {
    showgrid: false, zeroline: false, color: COLORS.muted, range: [0, 100], fixedrange: true,
    tickmode: "array", tickvals: [0, 20, 50, 80, 100], ticktext: ["0%", "20%", "50%", "80%", "100%"]
  };
  layout.shapes = [
    {type: "rect", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 0, y1: 20, fillcolor: "rgba(34,211,238,0.10)", line: {width: 0}, layer: "below"},
    {type: "rect", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 80, y1: 100, fillcolor: "rgba(245,158,11,0.12)", line: {width: 0}, layer: "below"},
    {type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: 50, y1: 50, line: {color: COLORS.muted, width: 1, dash: "dot"}, layer: "below"}
  ];
  layout.annotations = [{
    x: 0, y: 1.15, xref: "paper", yref: "paper", showarrow: false, xanchor: "left",
    text: formatHeader(current),
    font: {color: COLORS.cyan, size: 11}
  }];
  Plotly.react("ivrank", data, layout, {displayModeBar: false, scrollZoom: false, responsive: true}).then(() => {
    const gd = document.getElementById("ivrank");
    if (!gd || !gd.on) return;
    if (gd.removeAllListeners) {
      gd.removeAllListeners("plotly_hover");
      gd.removeAllListeners("plotly_unhover");
    }
    gd.on("plotly_hover", ev => {
      const point = (ev.points || []).find(p => p.curveNumber === 0) || (ev.points || [])[0];
      const cd = point && point.customdata;
      if (!cd) return;
      Plotly.relayout(gd, {"annotations[0].text": formatHeader({avg_iv_pct: cd[0], spot: cd[1], iv_rank_pct: cd[2]})});
    });
    gd.on("plotly_unhover", () => {
      Plotly.relayout(gd, {"annotations[0].text": formatHeader(current)});
    });
  });
}
