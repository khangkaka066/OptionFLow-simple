import { fmtLevel } from "./utils.js";

const REGIME_LABEL = { positive: "Dương", negative: "Âm" };
const GAMMA_REGIME_LABEL = { positive: "Positive (Range)", negative: "Negative (Trend/Breakout)" };
const CONFIDENCE_LABEL = { high: "cao", low: "thấp" };

function buildKeyLevelLine(ticker, summary) {
  const label = `$${String(ticker || "QQQ").toUpperCase()}`;
  const basisLabel = summary.basis === "eod" ? "EOD (locked)" : "Intraday";
  const key = summary.key_level;
  const trigger = summary.trigger_level;
  const parts = [
    `${label}: Regime GEX ${GAMMA_REGIME_LABEL[summary.gamma_regime] || "NA"}`,
    "IV EOD", summary.iv_eod_pct != null ? `${fmtLevel(summary.iv_eod_pct, 1)}%` : "NA",
    "Vanna", REGIME_LABEL[summary.vanna_regime] || "NA",
    "DEX", REGIME_LABEL[summary.dex_regime] || "NA",
    "Key Level", key ? `${fmtLevel(key.price, 2)} (${key.label}, conf=${CONFIDENCE_LABEL[key.confidence] || key.confidence})` : "NA",
    "Trigger", trigger ? `${fmtLevel(trigger.price, 2)} (${trigger.label})` : "NA",
    "Basis", basisLabel,
    "Đọc", summary.confidence_label || "NA",
  ];
  return parts.join(", ");
}

function renderKeyLevelPanel(summary, ticker) {
  const statusEl = document.getElementById("keyLevelStatus");
  const bodyEl = document.getElementById("keyLevelBody");
  const tagEl = document.getElementById("keyLevelTag");
  const exportEl = document.getElementById("keyLevelExport");
  if (!statusEl || !bodyEl || !tagEl || !exportEl) return;

  if (!summary || summary.status === "pending") {
    statusEl.textContent = "Đang chờ xác nhận sau 9:00 NY...";
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
  setText("keyLevelGamma", GAMMA_REGIME_LABEL[summary.gamma_regime] || "NA");
  setText("keyLevelIv", summary.iv_eod_pct != null ? `${fmtLevel(summary.iv_eod_pct, 1)}%` : "NA");
  setText("keyLevelVanna", REGIME_LABEL[summary.vanna_regime] || "NA");
  setText("keyLevelDex", REGIME_LABEL[summary.dex_regime] || "NA");
  setText(
    "keyLevelKey",
    summary.key_level
      ? `${fmtLevel(summary.key_level.price, 2)} — ${summary.key_level.label} (độ tin cậy OI: ${CONFIDENCE_LABEL[summary.key_level.confidence] || summary.key_level.confidence})`
      : "NA"
  );
  setText(
    "keyLevelTrigger",
    summary.trigger_level ? `${fmtLevel(summary.trigger_level.price, 2)} — ${summary.trigger_level.label}` : "NA"
  );
  setText("keyLevelConfidence", summary.confidence_label || "NA");
  const notesEl = document.getElementById("keyLevelNotes");
  if (notesEl) {
    notesEl.innerHTML = "";
    (summary.notes || []).forEach(note => {
      const li = document.createElement("li");
      li.textContent = note;
      notesEl.appendChild(li);
    });
  }
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

export function drawKeyLevelPanel({ latestState }) {
  if (!latestState) return;
  const ticker = latestState.latest_summary?.ticker || "QQQ";
  renderKeyLevelPanel(latestState.key_level_summary, ticker);
}
