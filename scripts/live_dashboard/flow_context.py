from __future__ import annotations

import math

from .key_level_summary import (
    HIGH_OI_NEAR_SPOT_WINDOW,
    _matched_strike_row,
    _num,
    _oi_at,
    _strike_spacing,
)

# Fixed catalog of signal "families" a strike can collect for the Confluence
# Top score. GEX wall fields (Call Resistance / Put Support / Gamma Wall) all
# count as one family so a single structural level can't inflate the score
# on its own — matches how the manual gameplan read confluence.
CONFLUENCE_FAMILY_OF_FIELD = {
    "call_resistance": "gex_wall",
    "put_support": "gex_wall",
    "gamma_wall_abs": "gex_wall",
    "vanna_wall_abs": "vanna_wall",
    "charm_wall_abs": "charm_wall",
    "high_oi": "high_oi",
    "delta_sign_flip": "delta_flip",
}
MAX_CONFLUENCE_SCORE = 5


def _label_families(level: dict) -> set[str]:
    """`_dedupe_levels` merges multiple candidates into one entry whose
    `field` is just whichever candidate was inserted first — so the family
    count must be read from the (possibly multi-source) label text, not the
    single leftover `field`."""
    text = level.get("label") or ""
    families: set[str] = set()
    if "Call Resistance" in text or "Put Support" in text or "Gamma Wall" in text:
        families.add("gex_wall")
    if "Vanna Wall" in text:
        families.add("vanna_wall")
    if "Charm Wall" in text:
        families.add("charm_wall")
    if "High OI" in text:
        families.add("high_oi")
    if "Delta Flip" in text:
        families.add("delta_flip")
    if not families:
        families.add(CONFLUENCE_FAMILY_OF_FIELD.get(level.get("field"), level.get("field")))
    return families


def _oi_iv_trough(by_strike: list[dict], spot: float | None, window: float) -> dict | None:
    if spot is None:
        return None
    candidates = []
    for row in by_strike:
        strike = _num(row.get("strike"))
        iv = _num(row.get("iv"))
        if strike is None or iv is None or iv <= 0:
            continue
        if abs(strike - spot) > window:
            continue
        candidates.append((strike, iv))
    if not candidates:
        return None
    strike, iv = min(candidates, key=lambda item: item[1])
    return {"strike": strike, "iv_pct": iv * 100}


def compute_confluence_top(
    key_levels: list[dict], by_strike: list[dict], spot: float | None
) -> dict | None:
    """Re-rank the already-deduped Key Levels by how many independent signal
    families land on the same strike, breaking ties toward strikes divisible
    by 5 (round numbers concentrate OI / act as magnets) and then proximity
    to spot. Score is out of MAX_CONFLUENCE_SCORE signal families."""
    if not key_levels or spot is None:
        return None

    def score_of(level: dict) -> int:
        families = _label_families(level)
        trough = _oi_iv_trough(by_strike, spot, HIGH_OI_NEAR_SPOT_WINDOW)
        if trough is not None and abs(trough["strike"] - level["price"]) <= max(1.0, _strike_spacing(by_strike)):
            families.add("oi_iv_trough")
        return min(MAX_CONFLUENCE_SCORE, len(families))

    ranked = sorted(
        key_levels,
        key=lambda lvl: (
            -score_of(lvl),
            0 if _num(lvl["price"]) is not None and float(lvl["price"]) % 5 == 0 else 1,
            abs(lvl["price"] - spot),
        ),
    )
    top = ranked[0]
    return {
        "price": top["price"],
        "label": top["label"],
        "score": score_of(top),
        "max_score": MAX_CONFLUENCE_SCORE,
        "distance_from_spot": top["price"] - spot,
        "divisible_by_5": float(top["price"]) % 5 == 0,
    }


def compute_flow_context(
    *,
    dealer_verdict: dict | None,
    expected_move_anchor: dict | None,
    key_levels: list[dict],
    by_strike: list[dict],
    candles: list[dict] | None,
    spot: float | None,
) -> dict:
    """The "Flow Context" 6-cell block: IV EOD verdict, strike ref, spot vs
    strike, expected move, overnight path, and the top confluence strike."""
    strike_ref = dealer_verdict.get("strike_ref") if dealer_verdict else None
    spot_above = dealer_verdict.get("spot_above_strike") if dealer_verdict else None
    spot_ref = dealer_verdict.get("spot_900et") if dealer_verdict else spot

    em_move_abs = _num(expected_move_anchor.get("move_abs")) if expected_move_anchor else None
    em_multiple = None
    diff = None
    if spot_ref is not None and strike_ref is not None:
        diff = spot_ref - strike_ref
        if em_move_abs:
            em_multiple = abs(diff) / em_move_abs

    overnight = None
    if candles:
        highs = [c["h"] for c in candles if _num(c.get("h")) is not None]
        lows = [c["l"] for c in candles if _num(c.get("l")) is not None]
        opens = [c["o"] for c in candles if _num(c.get("o")) is not None]
        if highs and lows and opens:
            overnight = {
                "open": opens[0],
                "high": max(highs),
                "low": min(lows),
                "last": candles[-1].get("c"),
            }

    confluence = compute_confluence_top(key_levels, by_strike, spot_ref)

    return {
        "iv_verdict": dealer_verdict.get("verdict") if dealer_verdict else None,
        "strike_ref": strike_ref,
        "strike_ref_date": dealer_verdict.get("strike_ref_date") if dealer_verdict else None,
        "spot_ref": spot_ref,
        "spot_above_strike": spot_above,
        "spot_strike_diff": diff,
        "expected_move": {
            "move_abs": em_move_abs,
            "move_pct": _num(expected_move_anchor.get("move_pct")) if expected_move_anchor else None,
            "band_low": (_num(expected_move_anchor.get("spot")) - em_move_abs)
            if expected_move_anchor and em_move_abs is not None and _num(expected_move_anchor.get("spot")) is not None
            else None,
            "band_high": (_num(expected_move_anchor.get("spot")) + em_move_abs)
            if expected_move_anchor and em_move_abs is not None and _num(expected_move_anchor.get("spot")) is not None
            else None,
            "em_multiple_to_strike": em_multiple,
        } if expected_move_anchor else None,
        "overnight_path": overnight,
        "confluence_top": confluence,
    }


def _vex_chex_cell(summary: dict, flip_field: str, wall_field: str, call_field: str, put_field: str) -> dict:
    call_total = _num(summary.get(call_field))
    put_total = _num(summary.get(put_field))
    balance = None
    if call_total is not None and put_total is not None and (call_total or put_total):
        denom = max(abs(call_total), abs(put_total)) or 1.0
        balance = (call_total + put_total) / denom
    return {
        "flip": _num(summary.get(flip_field)),
        "wall": _num(summary.get(wall_field)),
        "call_total": call_total,
        "put_total": put_total,
        "balance": balance,
    }


def compute_level_map(
    *,
    spot: float | None,
    spot_time: str | None,
    strike_ref: float | None,
    flow_context: dict,
    greeks_structure: dict,
) -> list[dict]:
    """Flat, price-sorted list of notable strikes near spot — same idea as
    the manual gameplan's "LEVEL MAP": one row per structural level, tagged
    with what it is and why it matters, built only from GEX/DEX/VEX/CHEX and
    OI×IV data already computed (no narrative judgment)."""
    rows: list[dict] = []

    def add(price, tag, note, accent="w"):
        if price is None:
            return
        price = _num(price)
        if price is None:
            return
        rows.append({"price": price, "tag": tag, "note": note, "accent": accent})

    em = flow_context.get("expected_move") or {}
    add(em.get("band_high"), "EM UPPER EDGE", "Spot + Expected Move.", "c")
    add(em.get("band_low"), "EM LOWER EDGE", "Spot − Expected Move.", "c")

    confluence = flow_context.get("confluence_top")
    if confluence:
        add(
            confluence["price"], f"CONFLUENCE · {confluence['score']}/{confluence['max_score']}",
            f"{confluence['label']} hội tụ gần nhau.", "a",
        )

    overnight = flow_context.get("overnight_path")
    if overnight:
        add(overnight.get("high"), "OVERNIGHT HIGH", "Đỉnh phiên qua đêm.", "w")
        add(overnight.get("low"), "OVERNIGHT LOW", "Đáy phiên qua đêm.", "w")

    add(strike_ref, "STRIKE REF", "Giá đóng cửa phiên trước.", "c")
    if spot is not None:
        add(spot, f"SPOT {spot_time or ''}".strip(), "Giá đánh giá hiện tại.", "w")

    gd = greeks_structure.get("gamma_delta") or {}
    add(gd.get("call_resistance"), "CALL RESISTANCE", "Gamma — kháng cự phía trên.", "g")
    add(gd.get("put_support"), "PUT SUPPORT", "Gamma — hỗ trợ phía dưới.", "g")
    add(gd.get("gamma_wall"), "GAMMA WALL", "Mốc GEX tuyệt đối lớn nhất.", "g")
    add(gd.get("gamma_flip"), "GAMMA FLIP", "Nơi Net GEX đổi dấu.", "r")
    add(gd.get("delta_flip"), "DELTA FLIP", "Nơi Net DEX đổi dấu.", "r")

    vanna = greeks_structure.get("vanna") or {}
    add(vanna.get("wall"), "VANNA WALL", "Mốc VEX tuyệt đối lớn nhất.", "v")
    add(vanna.get("flip"), "VANNA FLIP", "Nơi Net VEX đổi dấu.", "v")

    charm = greeks_structure.get("charm") or {}
    add(charm.get("wall"), "CHARM WALL", "Mốc CHEX tuyệt đối lớn nhất.", "v")
    add(charm.get("flip"), "CHARM FLIP", "Nơi Net CHEX đổi dấu.", "v")

    oi = greeks_structure.get("open_interest") or {}
    for c in (oi.get("calls") or [])[:2]:
        if c.get("oi"):
            add(c["strike"], f"CALL OI {c['oi']/1000:.1f}K", "Call OI nổi bật gần spot.", "g")
    for p in (oi.get("puts") or [])[:2]:
        if p.get("oi"):
            add(p["strike"], f"PUT OI {p['oi']/1000:.1f}K", "Put OI nổi bật gần spot.", "p")

    oi_iv = greeks_structure.get("oi_iv") or {}
    if oi_iv.get("trough_strike") is not None:
        add(oi_iv["trough_strike"], "OI×IV TROUGH", f"IV rẻ nhất vùng gần spot ({oi_iv.get('trough_iv_pct'):.1f}%).", "a")

    # Merge rows that land on (near enough) the same strike into one entry.
    rows.sort(key=lambda r: -r["price"])
    merged: list[dict] = []
    for row in rows:
        match = next((m for m in merged if abs(m["price"] - row["price"]) < 0.05), None)
        if match:
            if row["tag"] not in match["tag"]:
                match["tag"] = match["tag"] + " + " + row["tag"]
            if row["note"] not in match["note"]:
                match["note"] = match["note"] + " " + row["note"]
        else:
            merged.append(dict(row))
    return merged


def compute_greeks_structure(
    *,
    summary: dict | None,
    by_strike: list[dict],
    spot: float | None,
    skew_tenors: list[dict] | None,
) -> dict:
    """The "Greeks & Structure" 6-cell block: Gamma/Delta, Vanna, Charm, Open
    Interest, OI×IV, and Skew — all off data the dashboard already computes."""
    if not summary:
        return {"status": "pending"}

    gamma_delta = {
        "gamma_flip": _num(summary.get("gamma_flip")),
        "gamma_wall": _num(summary.get("gamma_wall_abs")),
        "call_resistance": _num(summary.get("call_resistance")),
        "put_support": _num(summary.get("put_support")),
        "net_gex": _num(summary.get("net_gex")),
        "delta_flip": _num(summary.get("delta_flip")),
        "net_dex": _num(summary.get("net_dex")),
        "spot_above_all": all(
            spot is not None and level is not None and spot > level
            for level in (
                _num(summary.get("gamma_flip")),
                _num(summary.get("gamma_wall_abs")),
                _num(summary.get("call_resistance")),
                _num(summary.get("put_support")),
                _num(summary.get("delta_flip")),
            )
        ) if spot is not None else None,
    }

    vanna = _vex_chex_cell(summary, "vanna_flip", "vanna_wall_abs", "total_call_vex", "total_put_vex")
    charm = _vex_chex_cell(summary, "charm_flip", "charm_wall_abs", "total_call_chex", "total_put_chex")

    near_spot = by_strike
    if spot is not None:
        near_spot = [
            row for row in by_strike
            if (strike := _num(row.get("strike"))) is not None and abs(strike - spot) <= HIGH_OI_NEAR_SPOT_WINDOW
        ]
    top_calls = sorted(
        (row for row in near_spot if (_num(row.get("call_oi")) or 0) > 0),
        key=lambda r: _num(r.get("call_oi")) or 0, reverse=True,
    )[:3]
    top_puts = sorted(
        (row for row in near_spot if (_num(row.get("put_oi")) or 0) > 0),
        key=lambda r: _num(r.get("put_oi")) or 0, reverse=True,
    )[:3]
    open_interest = {
        "calls": [{"strike": _num(r.get("strike")), "oi": _num(r.get("call_oi"))} for r in top_calls],
        "puts": [{"strike": _num(r.get("strike")), "oi": _num(r.get("put_oi"))} for r in top_puts],
    }

    trough = _oi_iv_trough(by_strike, spot, HIGH_OI_NEAR_SPOT_WINDOW)
    spot_row = _matched_strike_row(by_strike, spot, _strike_spacing(by_strike)) if spot is not None else None
    spot_iv = _num(spot_row.get("iv")) if spot_row else None
    oi_iv = {
        "trough_strike": trough.get("strike") if trough else None,
        "trough_iv_pct": trough.get("iv_pct") if trough else None,
        "spot_iv_pct": spot_iv * 100 if spot_iv is not None else None,
    }

    skew = {"available": False}
    if skew_tenors:
        nearest = min(
            (t for t in skew_tenors if _num(t.get("dte")) is not None),
            key=lambda t: abs(_num(t.get("dte")) or math.inf),
            default=None,
        )
        if nearest is not None:
            atm_iv = _num(nearest.get("atm_iv"))
            skew = {
                "available": True,
                "expiry": nearest.get("expiry"),
                "dte": _num(nearest.get("dte")),
                "atm_iv_pct": atm_iv * 100 if atm_iv is not None and atm_iv < 1 else atm_iv,
                "skew_25d": _num(nearest.get("skew_25d")),
                "skew_slope": _num(nearest.get("skew_slope")),
            }

    return {
        "status": "ready",
        "gamma_delta": gamma_delta,
        "vanna": vanna,
        "charm": charm,
        "open_interest": open_interest,
        "oi_iv": oi_iv,
        "skew": skew,
    }
