from __future__ import annotations

import math
import statistics

import pandas as pd

from exposure import nearest_atm_iv

GEX_WALL_LEVELS = (
    ("call_resistance", "Call Resistance"),
    ("put_support", "Put Support"),
    ("gamma_wall_abs", "Gamma Wall"),
    ("vanna_wall_abs", "Vanna Wall"),
    ("charm_wall_abs", "Charm Wall"),
)
# Deep ITM strikes accumulate OI over many rolls/days and are almost always
# the chain's global OI maxima, but they're not decision-relevant "near
# spot" levels - the High OI source is scoped to strikes within this many
# points of spot before picking the top ones, not the whole (wide) chain.
HIGH_OI_NEAR_SPOT_WINDOW = 30.0
KEY_LEVEL_DISTANCE_THRESHOLD = 0.005
MAX_TRIGGER_IV = 5.0  # IV is stored as a fraction; reject obvious bad back-solves above 500%.
MIN_DEX_LEG_RATIO = 0.10  # Each side must reach 10% of nearby median |net DEX|.


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


def _strike_spacing(by_strike: list[dict]) -> float:
    strikes = sorted({strike for row in by_strike if (strike := _num(row.get("strike"))) is not None})
    gaps = [right - left for left, right in zip(strikes, strikes[1:]) if right > left]
    return statistics.median(gaps) if gaps else 1.0


def _oi_cutoff(by_strike: list[dict], spot: float | None) -> float | None:
    if spot is None:
        return None
    values = sorted(
        _oi_at(row) for row in by_strike
        if (strike := _num(row.get("strike"))) is not None
        and abs(strike - spot) <= HIGH_OI_NEAR_SPOT_WINDOW and _oi_at(row) > 0
    )
    if len(values) < 3:
        return None
    return max(values[math.ceil(len(values) * 0.75) - 1], statistics.median(values) * 1.5)


def _matched_strike_row(by_strike: list[dict], price: float, spacing: float) -> dict | None:
    row = _nearest_strike_row(by_strike, price)
    if row is None or abs(float(row["strike"]) - price) > max(0.5, spacing * 0.51):
        return None
    return row


def _level_candidate(
    price: float, field: str, label: str, by_strike: list[dict], oi_cutoff: float | None, spacing: float
) -> dict:
    row = _matched_strike_row(by_strike, price, spacing)
    oi = _oi_at(row) if row is not None else None
    confidence = "unknown" if row is None or oi_cutoff is None else "high" if oi >= oi_cutoff else "low"
    iv_at_level = _num(row.get("iv")) if row else None
    return {"field": field, "label": label, "price": price, "confidence": confidence, "oi": oi, "iv": iv_at_level}


def _local_delta_flips(
    by_strike: list[dict], spot: float | None, spacing: float, oi_cutoff: float | None, atm_iv: float | None
) -> list[dict]:
    """Interpolate sign changes of per-strike net DEX, without cumulating it."""
    if spot is None:
        return []
    rows = sorted(
        ((strike, dex, row) for row in by_strike
         if (strike := _num(row.get("strike"))) is not None
         and (dex := _num(row.get("net_dex"))) is not None and dex != 0),
        key=lambda item: item[0],
    )
    near_window = max(HIGH_OI_NEAR_SPOT_WINDOW, abs(spot) * 0.04)
    nearby_dex = [abs(dex) for strike, dex, _ in rows if abs(strike - spot) <= near_window]
    dex_floor = statistics.median(nearby_dex) * MIN_DEX_LEG_RATIO if len(nearby_dex) >= 5 else 0.0
    flips: list[dict] = []
    for (left_strike, left_dex, left_row), (right_strike, right_dex, right_row) in zip(rows, rows[1:]):
        gap = right_strike - left_strike
        if gap <= 0 or gap > max(2.5 * spacing, 1.0) or left_dex * right_dex >= 0:
            continue
        price = left_strike + abs(left_dex) / (abs(left_dex) + abs(right_dex)) * gap
        if abs(price - spot) > near_window:
            continue
        anchor = max((left_row, right_row), key=_oi_at)
        anchor_oi = _oi_at(anchor)
        anchor_iv = _num(anchor.get("iv"))
        dex_supported = min(abs(left_dex), abs(right_dex)) >= dex_floor
        oi_supported = oi_cutoff is not None and anchor_oi >= oi_cutoff
        iv_available = (
            anchor_iv is not None and 0 < anchor_iv <= MAX_TRIGGER_IV
            and atm_iv is not None and 0 < atm_iv <= MAX_TRIGGER_IV
        )
        iv_relative_pct = (anchor_iv / atm_iv - 1) * 100 if iv_available else None
        iv_context = (
            "elevated" if iv_relative_pct is not None and iv_relative_pct >= 10
            else "subdued" if iv_relative_pct is not None and iv_relative_pct <= -10
            else "near_atm" if iv_relative_pct is not None else "unknown"
        )
        evidence_status = "supported" if dex_supported and oi_supported and iv_available else (
            "weak_dex" if not dex_supported else "missing_iv" if not iv_available
            else "unknown_oi" if oi_cutoff is None else "weak_oi"
        )
        flips.append({
            "field": "delta_sign_flip", "label": "Delta Flip (DEX đổi dấu)",
            "price": price,
            "from_sign": "positive" if left_dex > 0 else "negative",
            "to_sign": "positive" if right_dex > 0 else "negative",
            "strike_pair": [left_strike, right_strike],
            "dex_pair": [left_dex, right_dex],
            "oi_strike": _num(anchor.get("strike")), "oi": anchor_oi,
            "confidence": "high" if oi_supported else "low" if oi_cutoff is not None else "unknown",
            "iv": anchor_iv, "atm_iv": atm_iv, "iv_relative_pct": iv_relative_pct, "iv_context": iv_context,
            "evidence_status": evidence_status,
        })
    return sorted(flips, key=lambda flip: abs(flip["price"] - spot))


def _dedupe_levels(candidates: list[dict], by_strike: list[dict], spacing: float) -> list[dict]:
    """Merge candidates whose nearest real strike coincides.

    Multiple sources (e.g. Gamma Wall and a High-OI pick) often point at the
    same strike; show one entry with a combined label instead of duplicates.
    """
    merged: dict[float, dict] = {}
    for candidate in candidates:
        row = _matched_strike_row(by_strike, candidate["price"], spacing)
        strike_key = candidate.get("oi_strike") if candidate["field"] == "delta_sign_flip" else None
        if strike_key is None:
            strike_key = _num(row.get("strike")) if row else round(candidate["price"], 4)
        if strike_key in merged:
            existing = merged[strike_key]
            labels = existing["label"].split(" + ")
            if candidate["label"] not in labels:
                existing["label"] = existing["label"] + " + " + candidate["label"]
            if candidate["confidence"] == "high":
                existing["confidence"] = "high"
        else:
            merged[strike_key] = dict(candidate)
    return list(merged.values())


def compute_key_level_summary(
    summary: dict | None,
    by_strike: list[dict] | None,
    live_spot: float | None,
    basis: str,
    threshold_pct: float = KEY_LEVEL_DISTANCE_THRESHOLD,
) -> dict:
    """Summarize option levels and produce a conditional watch plan.

    `basis` is "eod" or "intraday" - it is passed in (decided by the caller
    based on `distance_pct` vs `threshold_pct`), not computed here, since the
    caller also decides which data (frozen EOD vs live intraday) to pass as
    `summary`/`by_strike`.
    """
    if not summary:
        return {"status": "pending"}

    by_strike = by_strike or []
    spot_ref = _num(summary.get("spot"))
    net_gex = _num(summary.get("net_gex"))
    net_dex = _num(summary.get("net_dex"))
    net_vex = _num(summary.get("net_vex"))
    current_spot = _num(live_spot)
    reference_spot = current_spot if current_spot is not None else spot_ref
    # A chain-wide mean IV (summary["avg_iv"]) is dragged around by noisy,
    # illiquid deep-OTM back-solves; nearest_atm_iv's liquidity-filtered
    # median of the strikes nearest this basis's own spot is the same robust
    # ATM read the Skew/IV-Rank panels use, so "IV EOD" means the same thing
    # everywhere in the dashboard. Falls back to the old chain-wide mean only
    # when there aren't enough by_strike rows to compute it.
    atm_iv_pct = nearest_atm_iv(pd.DataFrame(by_strike), spot_ref) if by_strike and spot_ref is not None else None
    avg_iv = (atm_iv_pct / 100.0) if atm_iv_pct is not None else _num(summary.get("avg_iv"))
    gamma_flip = _num(summary.get("gamma_flip"))

    # The flip is a zero crossing of cumulative exposure across strikes.
    # Its crossing direction is not fixed, so spot > flip does not imply
    # positive gamma. Use the aggregate sign for the regime and keep the
    # flip as a separate watch level.
    gamma_regime = "positive" if net_gex is not None and net_gex > 0 else "negative" if net_gex is not None and net_gex < 0 else "unknown"
    vanna_regime = "positive" if net_vex is not None and net_vex > 0 else "negative" if net_vex is not None and net_vex < 0 else "unknown"
    dex_regime = "positive" if net_dex is not None and net_dex > 0 else "negative" if net_dex is not None and net_dex < 0 else "unknown"

    distance_pct = None
    if spot_ref is not None and spot_ref > 0 and current_spot is not None:
        distance_pct = abs(current_spot - spot_ref) / spot_ref

    spacing = _strike_spacing(by_strike)
    oi_cutoff = _oi_cutoff(by_strike, reference_spot)
    # Delta Flip here is a sign change in *per-strike* net DEX. The older
    # summary.delta_flip is a cumulative crossing and is intentionally not
    # used for this trigger or for the Key Levels list.
    local_flips = _local_delta_flips(by_strike, reference_spot, spacing, oi_cutoff, avg_iv)
    trigger_level = next((flip for flip in local_flips if flip["evidence_status"] == "supported"), None)
    if trigger_level is None and local_flips:
        trigger_level = local_flips[0]

    # Key Levels combine GEX walls, supported local DEX sign flips, and
    # standout OI strikes. Unsupported flips remain visible as the Trigger
    # candidate, but do not outrank structural levels in this list.
    candidates: list[dict] = []
    for field, label in GEX_WALL_LEVELS:
        level = _num(summary.get(field))
        if level is not None:
            candidates.append(_level_candidate(level, field, label, by_strike, oi_cutoff, spacing))
    candidates += [flip for flip in local_flips if flip["evidence_status"] == "supported"][:3]
    near_spot_rows = by_strike
    if reference_spot is not None:
        near_spot_rows = [
            row for row in by_strike
            if (strike := _num(row.get("strike"))) is not None and abs(strike - reference_spot) <= HIGH_OI_NEAR_SPOT_WINDOW
        ]
    top_oi_rows = sorted(near_spot_rows, key=_oi_at, reverse=True)[:3]
    for row in top_oi_rows:
        strike = _num(row.get("strike"))
        if strike is not None and oi_cutoff is not None and _oi_at(row) >= oi_cutoff:
            candidates.append(_level_candidate(strike, "high_oi", "High OI", by_strike, oi_cutoff, spacing))
    candidates = _dedupe_levels(candidates, by_strike, spacing)

    key_levels: list[dict] = []
    key_level = None
    if reference_spot is not None and candidates:
        near_window = max(HIGH_OI_NEAR_SPOT_WINDOW, abs(reference_spot) * 0.04)
        candidates = [c for c in candidates if abs(c["price"] - reference_spot) <= near_window]
        ranked = sorted(
            candidates,
            key=lambda c: (abs(c["price"] - reference_spot), 0 if c["field"] != "high_oi" else 1,
                           0 if c["confidence"] == "high" else 1),
        )
        key_levels = ranked[:5]
        key_level = key_levels[0]

    notes: list[str] = []
    if gamma_regime == "positive":
        notes.append("Net GEX dương: theo dõi kịch bản range, cần giá xác nhận tại biên")
    elif gamma_regime == "negative":
        notes.append("Net GEX âm: theo dõi kịch bản breakout, cần giá giữ được sau khi phá mốc")
    else:
        notes.append("Thiếu Net GEX: chưa xác định được regime")
    if dex_regime != "unknown":
        notes.append(f"Net DEX {'dương' if dex_regime == 'positive' else 'âm'} là ước tính vị thế, không xác nhận hướng hedge thực tế")
    if key_level is not None:
        if key_level["confidence"] == "high":
            notes.append(f"OI tại {key_level['price']:.2f} nổi bật so với các strike gần spot; OI không tự xác nhận support/resistance")
        elif key_level["confidence"] == "low":
            notes.append(f"OI tại {key_level['price']:.2f} chưa nổi bật so với vùng gần spot")
        else:
            notes.append(f"Chưa đủ dữ liệu OI ngay tại {key_level['price']:.2f}; không gán độ tin cậy")
        if avg_iv is not None and key_level.get("iv") is not None:
            relation = "thấp hơn" if key_level["iv"] < avg_iv else "cao hơn" if key_level["iv"] > avg_iv else "bằng"
            notes.append(f"IV tại mốc {relation} ATM IV; IV không tự dự báo hướng đi của giá")
    if trigger_level is not None:
        if trigger_level["evidence_status"] == "supported":
            notes.append(
                f"Delta Flip {trigger_level['price']:.2f}: OI {trigger_level['oi']:.0f} tại strike "
                f"{trigger_level['oi_strike']:.2f} nổi bật; IV {trigger_level['iv'] * 100:.1f}% "
                f"so với ATM {trigger_level['atm_iv'] * 100:.1f}%"
            )
        elif trigger_level["evidence_status"] == "weak_dex":
            notes.append("Delta Flip gần spot có một phía net DEX quá nhỏ so với vùng lân cận; chưa dùng làm Trigger")
        elif trigger_level["evidence_status"] == "missing_iv":
            notes.append("Delta Flip gần spot chưa có IV hợp lệ tại vùng OI hoặc ATM; chỉ xem là mốc tham khảo")
        elif trigger_level["evidence_status"] == "unknown_oi":
            notes.append("Delta Flip gần spot có quá ít strike mang OI để so sánh; chưa đủ xác nhận cho Trigger")
        else:
            notes.append("Delta Flip gần spot có OI chưa nổi bật; chưa đủ xác nhận cho Trigger")
    else:
        notes.append("Không có điểm net DEX đổi dấu gần spot trong dữ liệu strike")
    if distance_pct is not None:
        notes.append(f"Spot lệch {distance_pct * 100:.2f}% so với spot của dữ liệu {basis.upper()}")

    support = _num(summary.get("put_support"))
    resistance = _num(summary.get("call_resistance"))
    plan = {"status": "reference", "setup": "Mốc tham chiếu", "condition": "Chờ spot live để đánh giá vị trí giá so với các mốc.", "invalidation": ""}
    if current_spot is not None:
        if not by_strike or not key_levels or gamma_regime == "unknown" or all(level["field"] == "high_oi" for level in key_levels):
            plan = {"status": "insufficient", "setup": "Chưa đủ dữ liệu", "condition": "Cần mốc GEX/DEX gần spot; OI đơn lẻ chưa tạo plan.", "invalidation": ""}
        elif basis == "eod" and distance_pct is not None and distance_pct > threshold_pct:
            plan = {"status": "stale", "setup": "Mốc EOD đã xa spot", "condition": "Cập nhật level intraday trước khi đánh giá kịch bản.", "invalidation": ""}
        elif trigger_level is None or trigger_level["evidence_status"] != "supported":
            plan = {"status": "insufficient", "setup": "Chờ Delta Flip + OI/IV", "condition": "Cần điểm net DEX đổi dấu gần spot với OI nổi bật và IV hợp lệ.", "invalidation": ""}
        elif gamma_regime == "positive" and support is not None and resistance is not None and support < current_spot < resistance:
            plan = {
                "status": "watch", "setup": "Range có điều kiện",
                "condition": f"Theo dõi Delta Flip {trigger_level['price']:.2f} và phản ứng giá tại Put Support {support:.2f} / Call Resistance {resistance:.2f}; cần giá xác nhận giữ vùng.",
                "invalidation": "Bỏ kịch bản range nếu giá phá và giữ bên ngoài biên tương ứng; đánh giá lại khi đi qua Delta Flip.",
            }
        elif gamma_regime == "negative":
            plan = {
                "status": "watch", "setup": "Breakout có điều kiện",
                "condition": f"Theo dõi giá khi qua Delta Flip {trigger_level['price']:.2f}; cần xác nhận giá giữ được phía vừa phá.",
                "invalidation": "Bỏ kịch bản breakout nếu giá quay lại phía cũ của Delta Flip.",
            }
        else:
            plan = {"status": "watch", "setup": "Chờ cấu trúc rõ hơn", "condition": "Giá chưa nằm trong range hợp lệ hoặc thiếu trigger gần spot.", "invalidation": ""}

    confidence_label = {
        "reference": "Chỉ là mốc tham chiếu", "insufficient": "Thiếu dữ liệu để lập plan",
        "stale": "Cần cập nhật intraday", "watch": "Chờ giá xác nhận",
    }[plan["status"]]
    return {
        "status": "ready",
        "basis": basis,
        "iv_eod_pct": (avg_iv * 100) if avg_iv is not None else None,
        "gamma_regime": gamma_regime,
        "vanna_regime": vanna_regime,
        "dex_regime": dex_regime,
        "key_level": key_level,
        "key_levels": key_levels,
        "trigger_level": trigger_level,
        "confidence_label": confidence_label,
        "plan": plan,
        "notes": notes,
        "distance_pct": distance_pct,
        "spot_ref": spot_ref,
        "current_spot": current_spot,
        "snapshot_utc": summary.get("snapshot_utc"),
        "gamma_flip": gamma_flip,
        "delta_flip_method": "per_strike_net_dex_sign_change",
    }
