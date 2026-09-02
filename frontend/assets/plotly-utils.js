import { COLORS } from "./config.js";

function ensureSvgGradient(svg, id, stops, x1, y1, x2, y2) {
  if (!svg) return;
  let defs = svg.querySelector("defs");
  if (!defs) {
    defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    svg.prepend(defs);
  }
  let grad = defs.querySelector("#" + id);
  if (!grad) {
    grad = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
    grad.setAttribute("id", id);
    defs.appendChild(grad);
  }
  grad.setAttribute("gradientUnits", "objectBoundingBox");
  grad.setAttribute("x1", x1);
  grad.setAttribute("y1", y1);
  grad.setAttribute("x2", x2);
  grad.setAttribute("y2", y2);
  grad.replaceChildren();
  stops.forEach(([offset, color, opacity]) => {
    const stop = document.createElementNS("http://www.w3.org/2000/svg", "stop");
    stop.setAttribute("offset", offset);
    stop.setAttribute("stop-color", color);
    stop.setAttribute("stop-opacity", String(opacity));
    grad.appendChild(stop);
  });
}

function applyBarFade(id, mode) {
  const gd = document.getElementById(id);
  const svg = gd?.querySelector("svg.main-svg");
  if (!gd || !svg) return;
  const prefix = id.replace(/[^a-zA-Z0-9_-]/g, "");
  ensureSvgGradient(svg, `${prefix}-h-pos`, [["0%", COLORS.cyan, 1], ["62%", COLORS.cyan, 0.72], ["100%", "#000000", 0.05]], 0, 0, 1, 0);
  ensureSvgGradient(svg, `${prefix}-h-neg`, [["0%", "#000000", 0.05], ["38%", COLORS.orange, 0.72], ["100%", COLORS.orange, 1]], 0, 0, 1, 0);
  ensureSvgGradient(svg, `${prefix}-v-call`, [["0%", "#000000", 0.05], ["38%", COLORS.cyan, 0.72], ["100%", COLORS.cyan, 1]], 0, 0, 0, 1);
  ensureSvgGradient(svg, `${prefix}-v-put`, [["0%", "#000000", 0.05], ["38%", COLORS.orange, 0.72], ["100%", COLORS.orange, 1]], 0, 0, 0, 1);
  const renderedTraces = (gd.data || []).filter(trace => trace.type === "bar" && Array.isArray(trace.x) && trace.x.length);
  gd.querySelectorAll(".barlayer .trace.bars").forEach((trace, traceIdx) => {
    let fill = "";
    const side = renderedTraces[traceIdx]?.meta?.fadeSide;
    if (mode === "exposure") fill = `url(#${prefix}-${side === "neg" ? "h-neg" : "h-pos"})`;
    else if (mode === "oi") fill = `url(#${prefix}-${side === "put" ? "v-put" : "v-call"})`;
    if (!fill) return;
    trace.querySelectorAll("path").forEach(path => {
      path.setAttribute("fill", fill);
      path.style.fill = fill;
      path.style.opacity = "1";
    });
  });
}

export function reactWithBarFade(id, data, layout, config, mode) {
  return Plotly.react(id, data, layout, config).then(() => {
    const gd = document.getElementById(id);
    const apply = () => applyBarFade(id, mode);
    apply();
    requestAnimationFrame(apply);
    if (gd && !gd.dataset.fadeHooked) {
      gd.dataset.fadeHooked = "1";
      gd.on?.("plotly_afterplot", apply);
    }
  });
}
