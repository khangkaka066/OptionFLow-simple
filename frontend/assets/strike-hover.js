import { COLORS } from "./config.js";
import { moneyM } from "./utils.js";

const numberText = value => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
  ? Number(value).toLocaleString("en-US") : "NA";

function nearestRow(rows, strike) {
  let nearest = null;
  let distance = Infinity;
  for (const row of rows) {
    const delta = Math.abs(Number(row.strike) - strike);
    if (delta < distance) {
      nearest = row;
      distance = delta;
    }
  }
  return nearest;
}

function tooltipHtml(row, options) {
  const strike = String(Number(row.strike));
  if (options.kind === "exposure") {
    const net = Number(row[options.key]) || 0;
    const sideColor = net < 0 ? COLORS.orange : COLORS.cyan;
    return `<strong style="color:${sideColor}">$${strike} net ${moneyM(net)}</strong>` +
      `<span style="color:${COLORS.cyan}">Net Call ${moneyM(row[options.callKey])}</span>` +
      `<span style="color:${COLORS.orange}">Net Put ${moneyM(row[options.putKey])}</span>`;
  }
  const iv = numberText(row.iv_pct);
  return `<strong>Strike $${strike}</strong>` +
    `<span style="color:${COLORS.cyan}">Calls ${numberText(row.call_oi)}</span>` +
    `<span style="color:${COLORS.orange}">Puts ${numberText(row.put_oi)}</span>` +
    (options.kind === "oiiv" ? `<span>IV ${iv}${iv === "NA" ? "" : "%"}</span>` : "");
}

function updateTooltip(gd, event) {
  const state = gd._strikeHover;
  if (!state) return;
  state.lastPointer = {clientX: event.clientX, clientY: event.clientY, buttons: event.buttons};
  const {rows, options, tooltip} = state;
  const size = gd._fullLayout?._size;
  const axis = options.kind === "exposure" ? gd._fullLayout?.yaxis : gd._fullLayout?.xaxis;
  if (!size || !axis || !rows.length || event.buttons) {
    tooltip.hidden = true;
    return;
  }

  const bounds = gd.getBoundingClientRect();
  const x = event.clientX - bounds.left;
  const y = event.clientY - bounds.top;
  if (x < size.l || x > size.l + size.w || y < size.t || y > size.t + size.h) {
    tooltip.hidden = true;
    return;
  }

  const pixel = options.kind === "exposure" ? y - size.t : x - size.l;
  const row = nearestRow(rows, axis.p2l(pixel));
  if (!row) {
    tooltip.hidden = true;
    return;
  }
  if (options.kind !== "exposure" && Math.abs(pixel - axis.l2p(Number(row.strike))) > 24) {
    tooltip.hidden = true;
    return;
  }

  // Keep a small hit area around thin bars. The label is driven by the
  // pointer position, so Plotly's transient unhover events cannot hide it.
  if (options.kind === "exposure") {
    const strikePixel = axis.l2p(Number(row.strike));
    const xaxis = gd._fullLayout.xaxis;
    const zero = size.l + xaxis.l2p(0);
    const end = size.l + xaxis.l2p(options.valueToPlot(Number(row[options.key]) || 0));
    if (Math.abs(pixel - strikePixel) > Math.max(8, Math.abs(axis.l2p(Number(row.strike) + 0.48) - strikePixel)) ||
        x < Math.min(zero, end) - 14 || x > Math.max(zero, end) + 14) {
      tooltip.hidden = true;
      return;
    }
  }

  tooltip.innerHTML = tooltipHtml(row, options);
  tooltip.hidden = false;
  const tipWidth = tooltip.offsetWidth;
  const tipHeight = tooltip.offsetHeight;
  let left = x + 14;
  let top = y + 14;
  if (left + tipWidth > bounds.width - 8) left = x - tipWidth - 14;
  if (top + tipHeight > bounds.height - 8) top = y - tipHeight - 14;
  tooltip.style.left = Math.max(8, left) + "px";
  tooltip.style.top = Math.max(8, top) + "px";
}

export function attachStrikeHover(id, rows, options) {
  const gd = document.getElementById(id);
  if (!gd) return;
  const lastPointer = gd._strikeHover?.lastPointer;
  let tooltip = gd.querySelector(".strike-tooltip");
  if (!tooltip) {
    tooltip = document.createElement("div");
    tooltip.className = "strike-tooltip";
    tooltip.hidden = true;
    gd.appendChild(tooltip);
  }
  gd._strikeHover = {rows, options, tooltip, lastPointer};
  if (lastPointer) updateTooltip(gd, lastPointer);
  if (gd.dataset.strikeHoverHooked) return;
  gd.dataset.strikeHoverHooked = "1";
  gd.addEventListener("pointermove", event => updateTooltip(gd, event));
  gd.addEventListener("pointerleave", () => {
    gd._strikeHover.lastPointer = null;
    gd._strikeHover.tooltip.hidden = true;
  });
  gd.addEventListener("pointerdown", () => {
    gd._strikeHover.lastPointer = null;
    gd._strikeHover.tooltip.hidden = true;
  });
}
