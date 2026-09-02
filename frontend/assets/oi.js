import { COLORS } from "./config.js";
import { baseLayout, rightLegend } from "./utils.js";
import { reactWithBarFade } from "./plotly-utils.js";

export function drawOi(rows, summary) {
  const x = rows.map(r => r.strike);
  const data = [
    {x, y: rows.map(r => r.call_oi), type: "bar", name: "Calls", marker: {color: COLORS.cyan}, meta: {fadeSide: "call"}},
    {x, y: rows.map(r => r.put_oi), type: "bar", name: "Puts", marker: {color: COLORS.orange}, meta: {fadeSide: "put"}},
  ];
  const layout = baseLayout(360);
  layout.margin.t = 55;
  layout.legend = rightLegend();
  layout.barmode = "group";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis = {title: "Open Interest", showgrid: false, zeroline: false, color: COLORS.muted};
  if (summary?.spot) {
    layout.shapes = [{type: "line", x0: summary.spot, x1: summary.spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
    layout.annotations = [{x: summary.spot, y: 0.98, xref: "x", yref: "paper", text: "SPOT " + Number(summary.spot).toFixed(2), showarrow: false, xanchor: "left", yanchor: "top", xshift: 6, font: {color: COLORS.spot, size: 11}, bgcolor: "rgba(5,7,11,0.70)"}];
  }
  reactWithBarFade("oi", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true}, "oi");
}

export function drawOiIv(rows, summary) {
  const x = rows.map(r => r.strike);
  const data = [
    {x, y: rows.map(r => r.call_oi), type: "bar", name: "Calls", marker: {color: COLORS.cyan}, meta: {fadeSide: "call"}},
    {x, y: rows.map(r => r.put_oi), type: "bar", name: "Puts", marker: {color: COLORS.orange}, meta: {fadeSide: "put"}},
    {x, y: rows.map(r => r.iv_pct), type: "scatter", mode: "lines", name: "IV", yaxis: "y2", line: {color: "#F8FAFC", width: 2}},
  ];
  const layout = baseLayout(360);
  layout.margin.t = 55;
  layout.legend = rightLegend();
  layout.barmode = "group";
  layout.xaxis = {title: "Strike", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis = {title: "Open Interest", showgrid: false, zeroline: false, color: COLORS.muted};
  layout.yaxis2 = {title: "IV %", overlaying: "y", side: "right", color: COLORS.muted, showgrid: false, zeroline: false};
  if (summary?.spot) {
    layout.shapes = [{type: "line", x0: summary.spot, x1: summary.spot, y0: 0, y1: 1, xref: "x", yref: "paper", line: {color: COLORS.spot, dash: "dot"}}];
    layout.annotations = [{x: summary.spot, y: 0.98, xref: "x", yref: "paper", text: "SPOT " + Number(summary.spot).toFixed(2), showarrow: false, xanchor: "left", yanchor: "top", xshift: 6, font: {color: COLORS.spot, size: 11}, bgcolor: "rgba(5,7,11,0.70)"}];
  }
  reactWithBarFade("oiiv", data, layout, {displayModeBar: false, scrollZoom: true, responsive: true}, "oi");
}
