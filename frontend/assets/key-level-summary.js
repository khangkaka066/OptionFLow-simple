import { fmtLevel } from "./utils.js";

const REGIME_LABEL = { positive: "Dương", negative: "Âm" };
const GAMMA_REGIME_LABEL = { positive: "Dương (thiên về range)", negative: "Âm (dễ khuếch đại biến động)" };
const CONFIDENCE_LABEL = { high: "nổi bật", low: "chưa nổi bật", unknown: "chưa đủ dữ liệu" };

function snapshotLabel(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString("en-US", {timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}) + " ET"
    : "NA";
}

function formatLevel(level) {
  return `${fmtLevel(level.price, 2)} (${level.label}, OI=${CONFIDENCE_LABEL[level.confidence] || level.confidence})`;
}

const DIR_LABEL = { RISE: "RISE", FALL: "FALL", FLAT: "FLAT", UNKNOWN: "—" };

function dirClass(direction) {
  if (direction === "RISE") return "dv-rise";
  if (direction === "FALL") return "dv-fall";
  if (direction === "FLAT") return "dv-flat";
  return "dv-unknown";
}

function verdictClass(verdict) {
  if (verdict === "BUY LESS | SELL MORE") return "dv-bearish";
  if (verdict === "BUY MORE | SELL LESS") return "dv-bullish";
  return "dv-neutral";
}

function formatDealerVerdictLine(dv) {
  if (!dv) return "";
  const a = dv.branch_a || {};
  const b = dv.branch_b || {};
  const fmtPct = v => (v == null ? "n/a" : `${fmtLevel(v, 2)}%`);
  const fmtPx = v => (v == null ? "n/a" : fmtLevel(v, 2));
  const spotSide = dv.spot_above_strike == null ? "n/a" : (dv.spot_above_strike ? "TRÊN" : "DƯỚI");
  return [
    `Branch A (EOD close) ${dv.prev2_date} ${fmtPct(a.prev2)} → ${dv.prev1_date} ${fmtPct(a.prev1)} ⇒ ${a.direction}`,
    `Branch B (TB phiên) ${dv.prev2_date} ${fmtPct(b.prev2)} → ${dv.prev1_date} ${fmtPct(b.prev1)} ⇒ ${b.direction}`,
    `2 nhánh ${dv.branches_agree ? "đồng thuận" : "lệch nhau"} — độ tin cậy ${dv.confidence}`,
    `Strike ref (close ${dv.strike_ref_date}): ${fmtPx(dv.strike_ref)} · Spot 9:00 ET: ${fmtPx(dv.spot_900et)} (${spotSide} strike)`,
    `Verdict: ${dv.verdict || "NA"}`,
  ].join(" | ");
}

function renderDealerVerdictCard(dv) {
  const card = document.getElementById("dvCard");
  if (!card) return;
  if (!dv) { card.hidden = true; return; }
  card.hidden = false;

  const fmtPct = v => (v == null ? "n/a" : `${fmtLevel(v, 2)}%`);
  const fmtPx = v => (v == null ? "n/a" : fmtLevel(v, 2));

  const fillBranch = (prefix, branch, prev2Date, prev1Date) => {
    const dir = branch?.direction || "UNKNOWN";
    const dirEl = document.getElementById(`${prefix}Dir`);
    if (dirEl) {
      dirEl.textContent = DIR_LABEL[dir] || dir;
      dirEl.className = "dv-bp " + dirClass(dir);
    }
    const valueEl = document.getElementById(`${prefix}Value`);
    if (valueEl) valueEl.textContent = `${prev2Date} ${fmtPct(branch?.prev2)} → ${prev1Date} ${fmtPct(branch?.prev1)}`;
    const barEl = document.getElementById(`${prefix}Bar`);
    if (barEl) {
      const magnitude = Number.isFinite(Number(branch?.delta)) ? Math.min(100, Math.abs(Number(branch.delta)) * 6) : 0;
      barEl.style.width = `${Math.max(dir === "UNKNOWN" ? 0 : 6, magnitude)}%`;
      barEl.className = dirClass(dir);
    }
    const branchBox = document.getElementById(prefix);
    if (branchBox) branchBox.className = "dv-branch " + (dir === "RISE" ? "dv-rise" : dir === "FALL" ? "dv-fall" : "");
  };
  fillBranch("dvBranchA", dv.branch_a, dv.prev2_date, dv.prev1_date);
  fillBranch("dvBranchB", dv.branch_b, dv.prev2_date, dv.prev1_date);

  const agreeEl = document.getElementById("dvAgree");
  const labelEl = document.getElementById("dvAgreeLabel");
  const detailEl = document.getElementById("dvAgreeDetail");
  if (agreeEl) agreeEl.className = "dv-agree " + verdictClass(dv.verdict);
  if (labelEl) {
    const agreement = dv.branches_agree ? "2 NHÁNH ĐỒNG THUẬN" : "2 NHÁNH LỆCH NHAU (ưu tiên Branch A)";
    labelEl.textContent = `${agreement} · ${dv.verdict || "NA"}`;
  }
  if (detailEl) {
    const spotSide = dv.spot_above_strike == null ? "n/a" : (dv.spot_above_strike ? "TRÊN" : "DƯỚI");
    detailEl.textContent = `Strike ref (close ${dv.strike_ref_date}) ${fmtPx(dv.strike_ref)} · Spot 9:00 ET ${fmtPx(dv.spot_900et)} — ${spotSide} strike · độ tin cậy ${dv.confidence}`;
  }
}

function formatTrigger(trigger) {
  if (!trigger) return "NA — không có net DEX đổi dấu gần spot";
  const direction = trigger.from_sign === "positive" ? "+ → −" : "− → +";
  const oi = Number.isFinite(Number(trigger.oi)) ? Number(trigger.oi).toLocaleString("en-US") : "NA";
  const iv = trigger.iv != null ? `${fmtLevel(trigger.iv * 100, 1)}%` : "NA";
  const atmIv = trigger.atm_iv != null ? `${fmtLevel(trigger.atm_iv * 100, 1)}%` : "NA";
  const ivDelta = trigger.iv_relative_pct != null ? ` (${trigger.iv_relative_pct >= 0 ? "+" : ""}${fmtLevel(trigger.iv_relative_pct, 1)}%)` : "";
  const status = trigger.evidence_status === "supported" ? "OI nổi bật, IV hợp lệ"
    : trigger.evidence_status === "weak_dex" ? "một phía DEX quá nhỏ"
    : trigger.evidence_status === "missing_iv" ? "thiếu IV hợp lệ"
    : trigger.evidence_status === "unknown_oi" ? "chưa đủ OI để so sánh" : "OI chưa nổi bật";
  return `${fmtLevel(trigger.price, 2)} — net DEX ${direction}; OI ${oi} tại ${fmtLevel(trigger.oi_strike, 2)}; IV ${iv} / ATM ${atmIv}${ivDelta}; ${status}`;
}

function buildKeyLevelLine(ticker, summary) {
  const label = `$${String(ticker || "QQQ").toUpperCase()}`;
  const basisLabel = summary.basis === "eod" ? "EOD (locked)" : "Intraday";
  const ivLabel = summary.basis === "eod" ? "IV EOD" : "IV Live";
  const keyLevels = summary.key_levels || [];
  const trigger = summary.trigger_level;
  const parts = [
    `${label}: Net GEX ${GAMMA_REGIME_LABEL[summary.gamma_regime] || "NA"}`,
    ivLabel, summary.iv_eod_pct != null ? `${fmtLevel(summary.iv_eod_pct, 1)}%` : "NA",
    "Vanna", REGIME_LABEL[summary.vanna_regime] || "NA",
    "DEX", REGIME_LABEL[summary.dex_regime] || "NA",
    "Key Levels", keyLevels.length ? keyLevels.map(formatLevel).join(" | ") : "NA",
    "Dealer Verdict", summary.dealer_verdict ? formatDealerVerdictLine(summary.dealer_verdict) : "NA",
    "Trigger", formatTrigger(trigger),
    "Basis", basisLabel,
    "Snapshot", snapshotLabel(summary.snapshot_utc),
    "Spot đánh giá", summary.current_spot != null ? fmtLevel(summary.current_spot, 2) : "NA",
    "Đọc", summary.confidence_label || "NA",
    "Plan", summary.plan?.setup || "NA",
    "Điều kiện", summary.plan?.condition || "NA",
    "Hủy khi", summary.plan?.invalidation || "NA",
  ];
  return parts.join(", ");
}

function fcCell(accentClass, label, value, note) {
  return `<div class="fc-cell ${accentClass}"><div class="fc-h">${label}</div><div class="fc-v">${value}</div><p>${note}</p></div>`;
}

function renderFlowContext(fc) {
  const grid = document.getElementById("flowContextGrid");
  if (!grid) return;
  if (!fc) { grid.innerHTML = ""; return; }
  const fmtPx = v => (v == null ? "NA" : fmtLevel(v, 2));
  const fmtPct = v => (v == null ? "NA" : `${fmtLevel(v, 2)}%`);

  const verdictAccent = verdictClass(fc.iv_verdict) === "dv-bearish" ? "fc-r" : verdictClass(fc.iv_verdict) === "dv-bullish" ? "fc-g" : "fc-c";
  const cells = [
    fcCell(verdictAccent, "IV EOD — VERDICT", fc.iv_verdict || "NA", "Hướng OTM IV (2 phiên trước) × vị trí spot so với strike."),
    fcCell("fc-c", "STRIKE REF", fmtPx(fc.strike_ref), `Giá đóng cửa phiên ${fc.strike_ref_date || "n/a"}.`),
    fcCell("fc-w", "SPOT VS STRIKE",
      fc.spot_above_strike == null ? "NA" : `${fc.spot_above_strike ? "ABOVE" : "BELOW"} ${fmtPx(fc.spot_ref)}`,
      fc.spot_strike_diff != null ? `${fc.spot_strike_diff >= 0 ? "+" : ""}${fmtLevel(fc.spot_strike_diff, 2)} so với strike lúc 9:00 ET.` : "Chờ dữ liệu 9:00 ET."),
    fc.expected_move
      ? fcCell("fc-c", "EXPECTED MOVE", `± ${fmtLevel(fc.expected_move.move_abs, 2)}`,
          `Band ${fmtPx(fc.expected_move.band_low)} / ${fmtPx(fc.expected_move.band_high)}` +
          (fc.expected_move.em_multiple_to_strike != null ? ` — lệch strike ${fmtLevel(fc.expected_move.em_multiple_to_strike, 2)}× EM.` : "."))
      : fcCell("fc-w", "EXPECTED MOVE", "NA", "Chưa có anchor Expected Move."),
    fc.overnight_path
      ? fcCell("fc-w", "OVERNIGHT PATH", `${fmtPx(fc.overnight_path.low)} – ${fmtPx(fc.overnight_path.high)}`,
          `Mở ${fmtPx(fc.overnight_path.open)}, hiện ${fmtPx(fc.overnight_path.last)}.`)
      : fcCell("fc-w", "OVERNIGHT PATH", "NO DATA", "Cần cấu hình Alpaca API key cho candles qua đêm."),
    fc.confluence_top
      ? fcCell("fc-a", "CONFLUENCE TOP", `${fmtPx(fc.confluence_top.price)} · ${fc.confluence_top.score}/${fc.confluence_top.max_score}`,
          `${fc.confluence_top.label}${fc.confluence_top.divisible_by_5 ? " (strike tròn 5)" : ""}.`)
      : fcCell("fc-w", "CONFLUENCE TOP", "NA", "Chưa đủ Key Levels gần spot."),
  ];
  grid.innerHTML = cells.join("");
}

function renderGreeksStructure(gs) {
  const grid = document.getElementById("greeksStructureGrid");
  if (!grid) return;
  if (!gs || gs.status !== "ready") { grid.innerHTML = ""; return; }
  const fmtPx = v => (v == null ? "NA" : fmtLevel(v, 2));
  const fmtM = v => (v == null ? "NA" : `${(v / 1e6).toFixed(1)}M`);
  const balanceNote = b => (b == null ? "chưa đủ dữ liệu" : b > 0 ? "nghiêng Call" : b < 0 ? "nghiêng Put" : "cân bằng");

  const gd = gs.gamma_delta || {};
  const vanna = gs.vanna || {};
  const charm = gs.charm || {};
  const oi = gs.open_interest || {};
  const oiIv = gs.oi_iv || {};
  const skew = gs.skew || {};

  const cells = [
    fcCell(gd.spot_above_all ? "fc-g" : "fc-w", "GAMMA / DELTA",
      gd.spot_above_all ? "SPOT ABOVE EVERYTHING" : `WALL ${fmtPx(gd.gamma_wall)}`,
      `Flip GEX ${fmtPx(gd.gamma_flip)} · Flip DEX ${fmtPx(gd.delta_flip)}. Call Res ${fmtPx(gd.call_resistance)} / Put Sup ${fmtPx(gd.put_support)}.`),
    fcCell("fc-v", "VANNA — VEX", `FLIP ${fmtPx(vanna.flip)}`,
      `Wall ${fmtPx(vanna.wall)}. Call ${fmtM(vanna.call_total)} / Put ${fmtM(vanna.put_total)} — ${balanceNote(vanna.balance)}.`),
    fcCell("fc-v", "CHARM — CHEX", `FLIP ${fmtPx(charm.flip)}`,
      `Wall ${fmtPx(charm.wall)}. Call ${fmtM(charm.call_total)} / Put ${fmtM(charm.put_total)} — ${balanceNote(charm.balance)}.`),
    fcCell("fc-g", "OPEN INTEREST",
      (oi.calls || []).length ? `${fmtPx(oi.calls[0].strike)} · ${(oi.calls[0].oi / 1000).toFixed(1)}K` : "NA",
      `Call: ${(oi.calls || []).map(c => `${fmtPx(c.strike)} (${(c.oi / 1000).toFixed(1)}K)`).join(", ") || "NA"}. ` +
      `Put: ${(oi.puts || []).map(p => `${fmtPx(p.strike)} (${(p.oi / 1000).toFixed(1)}K)`).join(", ") || "NA"}.`),
    fcCell("fc-c", "OI × IV",
      oiIv.trough_strike != null ? `TROUGH ${fmtPct(oiIv.trough_iv_pct)} @ ${fmtPx(oiIv.trough_strike)}` : "NA",
      oiIv.spot_iv_pct != null ? `IV tại spot ${fmtPct(oiIv.spot_iv_pct)}.` : ""),
    skew.available
      ? fcCell("fc-w", "SKEW", `${fmtPct(skew.atm_iv_pct)} (${skew.expiry || "n/a"})`,
          skew.skew_25d != null ? `25Δ skew ${fmtLevel(skew.skew_25d, 2)}.` : "")
      : fcCell("fc-w", "SKEW", "NO INPUT", "Chưa có dữ liệu Volatility Skew."),
  ];
  grid.innerHTML = cells.join("");
}

function fmtPct(v) { return v == null ? "NA" : `${fmtLevel(v, 2)}%`; }

function renderLevelMap(levelMap) {
  const listEl = document.getElementById("levelMapList");
  if (!listEl) return;
  listEl.innerHTML = "";
  if (!levelMap || !levelMap.length) return;
  levelMap.forEach(row => {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="lm-price fc-${row.accent}">${fmtLevel(row.price, 2)}</span>` +
      `<span><span class="lm-tag">${row.tag}</span><span class="lm-note">${row.note}</span></span>`;
    listEl.appendChild(li);
  });
}

function renderScenario(scenario) {
  const card = document.getElementById("scenarioCard");
  if (!card) return;
  if (!scenario) { card.hidden = true; return; }
  card.hidden = false;

  const setText = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  setText("scWindow", scenario.window);
  setText("scTitle", scenario.title);
  const confEl = document.getElementById("scConfidence");
  if (confEl) {
    confEl.textContent = scenario.confidence;
    confEl.className = "sc-confidence " + (scenario.confidence === "HIGH" ? "sc-high" : scenario.confidence === "LOW" ? "sc-low" : "sc-none");
  }
  setText("scThesis", scenario.thesis);
  const entry = scenario.entry || {};
  setText("scEntry", `${fmtLevel(entry.zone_low, 2)}–${fmtLevel(entry.zone_high, 2)}`);
  setText("scTap", `tap @ ${fmtLevel(entry.tap, 2)} · ${entry.label || ""}`);
  setText("scStop", fmtLevel(scenario.stop, 2));

  const targetsEl = document.getElementById("scTargets");
  if (targetsEl) {
    targetsEl.innerHTML = "";
    (scenario.targets || []).forEach((t, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="sc-tprice">T${i + 1} ${fmtLevel(t.price, 2)}</span> (${t.weight}%) — ${t.label}${t.rr != null ? ` · R:R ${t.rr}:1` : ""}`;
      targetsEl.appendChild(li);
    });
  }
  const checklistEl = document.getElementById("scChecklist");
  if (checklistEl) {
    checklistEl.innerHTML = "";
    (scenario.checklist || []).forEach(item => {
      const li = document.createElement("li");
      li.textContent = `☐ ${item}`;
      checklistEl.appendChild(li);
    });
  }
}

function renderKeyLevelPanel(summary, ticker) {
  const statusEl = document.getElementById("keyLevelStatus");
  const bodyEl = document.getElementById("keyLevelBody");
  const tagEl = document.getElementById("keyLevelTag");
  const exportEl = document.getElementById("keyLevelExport");
  if (!statusEl || !bodyEl || !tagEl || !exportEl) return;

  if (!summary || summary.status === "pending") {
    statusEl.textContent = "Đang chờ xác nhận sau 9:15 NY...";
    statusEl.hidden = false;
    bodyEl.hidden = true;
    tagEl.textContent = "PENDING";
    tagEl.classList.remove("locked");
    exportEl.textContent = "";
    return;
  }

  statusEl.hidden = true;
  bodyEl.hidden = false;
  tagEl.textContent = summary.basis === "eod" ? "EOD" : "INTRADAY";
  tagEl.classList.toggle("locked", summary.basis === "eod");

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  renderDealerVerdictCard(summary.dealer_verdict);
  setText("keyLevelSnapshot", snapshotLabel(summary.snapshot_utc));
  setText("keyLevelSpot", summary.current_spot != null ? fmtLevel(summary.current_spot, 2) : "NA");
  renderFlowContext(summary.flow_context);
  renderGreeksStructure(summary.greeks_structure);
  renderScenario(summary.scenario);
  renderLevelMap(summary.level_map);
  exportEl.textContent = buildKeyLevelLine(ticker, summary);
}

export function initKeyLevelCopyControls() {
  const btn = document.getElementById("keyLevelCopyBtn");
  const exportEl = document.getElementById("keyLevelExport");
  if (!btn || !exportEl) return;
  let resetTimer = null;
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(exportEl.textContent || "");
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

export function drawKeyLevelPanel({ latestState, panelDayState, panelPayload }) {
  if (panelDayState.keyLevel === "live") {
    if (!latestState) return;
    renderKeyLevelPanel(latestState.key_level_summary, latestState.latest_summary?.ticker || "QQQ");
  } else if (panelPayload.keyLevel) {
    renderKeyLevelPanel(panelPayload.keyLevel.key_level_summary, panelPayload.keyLevel.latest_summary?.ticker || "QQQ");
  }
}
