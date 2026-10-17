from __future__ import annotations

# Mechanical scenario builder: turns Dealer Verdict + Level Map into ONE
# trade card (direction, entry zone, targets, stop, checklist) using fixed
# rules only — no independent trading judgment, no probability scoring.
# Everything here is a restatement of numbers the dashboard already computed.

BUFFER_MIN = 0.30
BUFFER_MAX = 1.20
BUFFER_FRACTION_OF_SPOT = 0.0011

CHECKLIST_TEMPLATE = [
    "Range mở cửa (09:45 ET) đã hoàn tất trước khi vào lệnh.",
    "Có nến 5 phút đóng cửa xác nhận trong vùng entry — không phải chỉ chạm giá (wick).",
    "OTM IV vẫn đang {direction_word} đúng hướng Dealer Verdict trong phiên (Branch A/B).",
    "Độ tin cậy Dealer Verdict: {confidence} — {confidence_note}.",
]

CONFIDENCE_NOTE = {
    "HIGH": "2 nhánh IV đồng thuận, có thể vào full size theo kế hoạch",
    "LOW": "2 nhánh IV lệch nhau, cân nhắc giảm size",
    "NONE": "chưa đủ dữ liệu IV, chỉ nên theo dõi chứ chưa vào lệnh",
}


def _buffer(spot: float) -> float:
    return round(min(BUFFER_MAX, max(BUFFER_MIN, abs(spot) * BUFFER_FRACTION_OF_SPOT)), 2)


def _weights(n: int) -> list[int]:
    if n <= 1:
        return [100]
    if n == 2:
        return [60, 40]
    return [40, 40, 20]


def compute_scenario(
    *,
    dealer_verdict: dict | None,
    level_map: list[dict] | None,
    spot: float | None,
) -> dict | None:
    """One mechanically-derived scenario card: direction from the Dealer
    Verdict sign, entry zone from the nearest structural level on that side
    of spot, targets from the next Level Map entries in the trade's
    direction, stop just past the entry zone. No price/time judgment beyond
    these fixed rules."""
    if not dealer_verdict or spot is None or not level_map:
        return None
    verdict = dealer_verdict.get("verdict")
    if verdict not in ("BUY LESS | SELL MORE", "BUY MORE | SELL LESS"):
        return None

    is_fade = verdict == "BUY LESS | SELL MORE"  # bearish: dealers sell more -> fade into resistance
    buffer = _buffer(spot)

    # Candidate levels strictly on the trade's side of spot, nearest first.
    if is_fade:
        candidates = sorted((lvl for lvl in level_map if lvl["price"] > spot), key=lambda l: l["price"])
    else:
        candidates = sorted((lvl for lvl in level_map if lvl["price"] < spot), key=lambda l: -l["price"])
    if not candidates:
        return None

    # Prefer a level flagged as structural (confluence / wall) over a bare
    # price point (e.g. plain SPOT/STRIKE REF rows) when choosing the entry.
    def is_structural(lvl: dict) -> bool:
        tag = lvl.get("tag", "")
        return any(k in tag for k in ("CONFLUENCE", "WALL", "RESISTANCE", "SUPPORT", "EM UPPER", "EM LOWER"))

    entry_level = next((lvl for lvl in candidates if is_structural(lvl)), candidates[0])
    entry_price = entry_level["price"]

    if is_fade:
        zone_low, zone_high = round(entry_price - buffer, 2), entry_price
        stop = round(entry_price + buffer, 2)
        tap = round(entry_price - buffer * 0.4, 2)
    else:
        zone_low, zone_high = entry_price, round(entry_price + buffer, 2)
        stop = round(entry_price - buffer, 2)
        tap = round(entry_price + buffer * 0.4, 2)

    # Targets: subsequent Level Map rows beyond the entry, continuing in the
    # trade's direction, spaced at least one buffer apart.
    target_pool = sorted(
        (lvl for lvl in level_map if (lvl["price"] < entry_price if is_fade else lvl["price"] > entry_price)),
        key=lambda l: (-l["price"] if is_fade else l["price"]),
    )
    targets: list[dict] = []
    for lvl in target_pool:
        if targets and abs(lvl["price"] - targets[-1]["price"]) < buffer:
            continue
        targets.append(lvl)
        if len(targets) == 3:
            break
    weights = _weights(len(targets))

    entry_mid = (zone_low + zone_high) / 2
    risk = abs(stop - entry_mid)
    rr = []
    for t in targets:
        reward = abs(t["price"] - entry_mid)
        rr.append(round(reward / risk, 1) if risk else None)

    confidence = dealer_verdict.get("confidence", "NONE")
    direction_word = "TĂNG" if dealer_verdict.get("final_direction") == "RISE" else "GIẢM"
    checklist = [
        line.format(direction_word=direction_word, confidence=confidence, confidence_note=CONFIDENCE_NOTE.get(confidence, ""))
        for line in CHECKLIST_TEMPLATE
    ]

    return {
        "direction": "fade" if is_fade else "bounce",
        "verdict": verdict,
        "confidence": confidence,
        "window": "09:45–16:00 ET",
        "title": f"{verdict} — {'FADE' if is_fade else 'BOUNCE'} TẠI {entry_level.get('tag', '')} {entry_price:.2f}",
        "thesis": (
            f"{verdict}: {'dealer bán nhiều hơn / mua ít hơn khi giá chạm kháng cự' if is_fade else 'dealer mua nhiều hơn / bán ít hơn khi giá chạm hỗ trợ'} "
            f"tại {entry_price:.2f} ({entry_level.get('tag', '')})."
        ),
        "entry": {"zone_low": zone_low, "zone_high": zone_high, "tap": tap, "label": entry_level.get("tag", "")},
        "stop": stop,
        "targets": [
            {"price": t["price"], "label": t.get("tag", ""), "weight": w, "rr": r}
            for t, w, r in zip(targets, weights, rr)
        ],
        "checklist": checklist,
    }
