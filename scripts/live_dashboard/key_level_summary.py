from __future__ import annotations

import math

CANDIDATE_LEVELS = (
    ("call_resistance", "Call Resistance"),
    ("put_support", "Put Support"),
    ("gamma_wall_abs", "Gamma Wall"),
    ("delta_flip", "Delta Flip"),
    ("gamma_flip", "Gamma Flip"),
)


def _num(value) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _nearest_strike_row(by_strike: list[dict], level: float) -> dict | None:
    best = None
    best_dist = None
    for row in by_strike:
        strike = _num(row.get("strike"))
        if strike is None:
            continue
        dist = abs(strike - level)
        if best_dist is None or dist < best_dist:
            best = row
            best_dist = dist
    return best


def _oi_at(row: dict | None) -> float:
    if row is None:
        return 0.0
    call_oi = _num(row.get("call_oi")) or 0.0
    put_oi = _num(row.get("put_oi")) or 0.0
    return call_oi + put_oi


def _avg_oi(by_strike: list[dict]) -> float:
    total = 0.0
    count = 0
    for row in by_strike:
        total += _oi_at(row)
        count += 1
    return total / count if count else 0.0


def compute_key_level_summary(
    summary: dict | None,
    by_strike: list[dict] | None,
    live_spot: float | None,
    basis: str,
    threshold_pct: float = 0.005,
) -> dict:
    """Synthesize IV/Gamma/Vanna/DEX regime plus a Key Level and Trigger, per
    the manual read described in GEX_DEX_TRADE_PLAN_GUIDE.txt.

    `basis` is "eod" or "intraday" - it is passed in (decided by the caller
    based on `distance_pct` vs `threshold_pct`), not computed here, since the
    caller also decides which data (frozen EOD vs live intraday) to pass as
    `summary`/`by_strike`.
    """
    if not summary:
        return {"status": "pending"}

    by_strike = by_strike or []
    spot_ref = _num(summary.get("spot"))
    net_gex = _num(summary.get("net_gex")) or 0.0
    net_dex = _num(summary.get("net_dex")) or 0.0
    net_vex = _num(summary.get("net_vex")) or 0.0
    avg_iv = _num(summary.get("avg_iv"))
    gamma_flip = _num(summary.get("gamma_flip"))
    delta_flip = _num(summary.get("delta_flip"))

    gamma_regime = "positive" if net_gex >= 0 else "negative"
    vanna_regime = "positive" if net_vex >= 0 else "negative"
    dex_regime = "positive" if net_dex >= 0 else "negative"

    distance_pct = None
    if spot_ref not in (None, 0) and live_spot is not None:
        distance_pct = abs(_num(live_spot) - spot_ref) / spot_ref

    avg_oi = _avg_oi(by_strike)
    candidates = []
    for field, label in CANDIDATE_LEVELS:
        level = _num(summary.get(field))
        if level is None:
            continue
        row = _nearest_strike_row(by_strike, level)
        oi = _oi_at(row)
        confidence = "high" if avg_oi > 0 and oi >= avg_oi else "low"
        iv_at_level = _num(row.get("iv")) if row else None
        candidates.append({
            "field": field,
            "label": label,
            "price": level,
            "confidence": confidence,
            "oi": oi,
            "iv": iv_at_level,
        })

    reference_spot = live_spot if live_spot is not None else spot_ref
    key_level = None
    if reference_spot is not None and candidates:
        high_conf = [c for c in candidates if c["confidence"] == "high" and c["field"] != "gamma_flip"]
        pool = high_conf or [c for c in candidates if c["field"] != "gamma_flip"]
        key_level = min(pool, key=lambda c: abs(c["price"] - reference_spot)) if pool else None

    trigger_level = next((c for c in candidates if c["field"] == "gamma_flip"), None)

    notes: list[str] = []
    alignment_count = 0

    notes.append(
        "GEX " + ("dương" if gamma_regime == "positive" else "âm")
        + " -> " + ("thị trường có xu hướng range/mean-reversion" if gamma_regime == "positive" else "thị trường dễ trend/breakout")
    )

    dex_note = "DEX dương -> áp lực hedge mua khi giá giảm" if dex_regime == "positive" else "DEX âm -> áp lực hedge bán khi giá giảm"
    notes.append(dex_note)

    if key_level is not None:
        if key_level["confidence"] == "high":
            alignment_count += 1
            notes.append(f"OI xác nhận {key_level['label']} ({key_level['price']:.2f}) là vùng đáng tin cậy (OI cao)")
        else:
            notes.append(f"OI tại {key_level['label']} ({key_level['price']:.2f}) thấp -> mức này có thể fragile")

        if avg_iv is not None and key_level.get("iv") is not None:
            if key_level["iv"] <= avg_iv:
                alignment_count += 1
                notes.append("IV tại Key Level thấp hơn trung bình -> ủng hộ khả năng giữ vùng (mean-reversion)")
            else:
                notes.append("IV tại Key Level cao hơn trung bình -> cảnh báo rủi ro breakout")

    if gamma_regime == "positive" and dex_regime == "negative":
        alignment_count += 1
    elif gamma_regime == "negative" and dex_regime == "positive":
        alignment_count += 1

    if distance_pct is not None and distance_pct <= threshold_pct:
        alignment_count += 1
        notes.append(f"Spot còn gần vùng tham chiếu ({distance_pct * 100:.2f}%) -> đọc EOD vẫn còn hiệu lực")
    elif distance_pct is not None:
        notes.append(f"Spot đã lệch {distance_pct * 100:.2f}% khỏi vùng tham chiếu -> ưu tiên dùng level intraday")

    if alignment_count >= 3:
        confidence_label = "Đồng thuận cao"
    elif alignment_count == 2:
        confidence_label = "Giảm size"
    else:
        confidence_label = "Đứng ngoài, chờ tín hiệu rõ hơn"

    return {
        "status": "ready",
        "basis": basis,
        "iv_eod_pct": (avg_iv * 100) if avg_iv is not None else None,
        "gamma_regime": gamma_regime,
        "vanna_regime": vanna_regime,
        "dex_regime": dex_regime,
        "key_level": key_level,
        "trigger_level": trigger_level,
        "alignment_count": alignment_count,
        "confidence_label": confidence_label,
        "notes": notes,
        "distance_pct": distance_pct,
        "spot_ref": spot_ref,
        "gamma_flip": gamma_flip,
        "delta_flip": delta_flip,
    }
