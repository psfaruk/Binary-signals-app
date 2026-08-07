"""Engines package — category-aware prediction router (otc / real)."""
import copy
import os
import time as _time
from datetime import datetime as _datetime, timezone as _timezone

from engines import otc as _otc_engine
from engines import real as _real_engine
from engines.base.trap_hours import is_trap_hour as _is_trap_hour, trap_reason as _trap_reason
from engines.base.disabled_pairs import pair_penalty as _pair_penalty, penalty_reason as _penalty_reason

__all__ = ["predict", "otc", "real", "category_of"]


def category_of(asset: str) -> str:
    """Return the category for an asset name ('EURUSD_otc' -> 'otc', 'EURUSD' -> 'real')."""
    asset_lower = (asset or "").lower()
    if asset_lower.endswith("otc"):
        return "otc"
    return "real"


def predict(candles, ticks=None, micro=None, asset="", htf_trend="SIDEWAYS",
            period: int = 60, category: str = None, recent_accuracy=None) -> dict:
    """Route to the correct engine based on `category` (auto-detected from asset if None)."""
    if isinstance(category, str):
        category = category.lower()

    if category == "alltime_otc":
        category = "otc"

    # ── BREAKEVEN GATE (DEEP-FIX-2026-08-07) ──────────────────────────────
    # Before running the full prediction pipeline (7 modules × weighted blend
    # × agent evaluation), check whether this pair historically beats its
    # payout-adjusted breakeven rate. If the pair is a proven loser, skip
    # the expensive prediction and return NEUTRAL immediately.
    # This prevents chronic-loser pairs (BRLUSD_otc 44.3%, USDCOP_otc 44.8%,
    # USDBDT_otc 45.4%) from generating losing signals all day.
    # Disabled via QX_BREAKEVEN_GATE=0.
    try:
        if os.environ.get("QX_BREAKEVEN_GATE", "1") == "1" and asset:
            from core.breakeven import is_pair_profitable as _is_profitable
            _is_prof, _breason, _wr, _be, _n = _is_profitable(asset, period)
            if not _is_prof:
                print(f"[engines] ⛔ BREAKEVEN GATE: {_breason}")
                # Send alert (debounced per-asset so it doesn't spam)
                try:
                    import alerts as _alerts
                    from core.breakeven import breakeven_for_payout as _be_for_payout
                    _alerts.breakeven_gate_triggered(asset, _wr, _be)
                except Exception:
                    pass
                return {
                    "signal": "NEUTRAL",
                    "confidence": 0,
                    "strength": "WEAK",
                    "score": 0.0,
                    "reasons": [_breason],
                    "modules": {},
                    "regime": "breakeven_gate",
                    "category": category if category in ("otc", "real") else "otc",
                    "breakeven_gate": True,
                    "pair_win_rate": _wr,
                    "breakeven_pct": _be,
                    "sample_count": _n,
                    "skipped": True,
                }
    except Exception as _be_gate_exc:
        # Never let the breakeven gate crash the prediction pipeline.
        print(f"[engines] breakeven gate check failed for {asset}: {_be_gate_exc}")

    # ── PAIR HEALTH GATE (DEEP-FIX-2026-08-07) ────────────────────────────
    # Check if the pair is in cooldown (≥8 consecutive losses → skip 30 min).
    # This is a faster-acting gate than the breakeven gate — it catches
    # intraday regime changes before they accumulate into a bad day.
    try:
        if os.environ.get("QX_PAIR_HEALTH_GATE", "1") == "1" and asset:
            from core.pair_health import is_pair_healthy as _is_healthy
            _healthy, _hreason = _is_healthy(asset)
            if not _healthy:
                print(f"[engines] ⛔ PAIR HEALTH: {_hreason}")
                return {
                    "signal": "NEUTRAL",
                    "confidence": 0,
                    "strength": "WEAK",
                    "score": 0.0,
                    "reasons": [f"[PAIR_HEALTH] {_hreason}"],
                    "modules": {},
                    "regime": "pair_health_gate",
                    "category": category if category in ("otc", "real") else "otc",
                    "pair_health_gate": True,
                    "skipped": True,
                }
    except Exception as _ph_exc:
        print(f"[engines] pair health check failed for {asset}: {_ph_exc}")

    # Trap-hour auto-skip: NEUTRAL for (asset, hour) pairs the live brain flagged.
    try:
        _now_utc = _datetime.now(_timezone.utc)
        _hour_utc = _now_utc.hour
        if _is_trap_hour(asset, _hour_utc):
            _reason = _trap_reason(asset, _hour_utc)
            return {
                "signal": "NEUTRAL",
                "confidence": 0,
                "strength": "WEAK",
                "score": 0.0,
                "reasons": [_reason] if _reason else ["trap hour suppression"],
                "modules": {},
                "regime": "trap_hour",
                "category": category if category in ("otc", "real") else "otc",
                "trap_hour": True,
                "trap_hour_utc": _hour_utc,
                "trap_reason": _reason,
                "skipped": True,
            }
    except Exception as _trap_exc:
        print(f"[engines] trap-hour check failed for {asset}: {_trap_exc}")

    # Pair confidence penalty: dampen confidence for chronically low win-rate pairs.
    _pair_mult = 1.0
    _pair_pen_reason = ""
    try:
        _pair_mult = _pair_penalty(asset)
        if _pair_mult < 1.0:
            _pair_pen_reason = _penalty_reason(asset)
    except Exception as _pen_exc:
        print(f"[engines] pair-penalty check failed for {asset}: {_pen_exc}")

    if category is None:
        category = category_of(asset)
    elif category not in ("otc", "real"):
        pass
    else:
        detected = category_of(asset)
        if category != detected:
            raise ValueError(
                f"category/asset mismatch: category={category!r} but asset "
                f"{asset!r} implies category={detected!r}. Pass a consistent "
                f"pair, or omit category to auto-detect.")

    if category == "otc":
        engine = _otc_engine
    elif category == "real":
        engine = _real_engine
    else:
        raise ValueError(
            f"unknown category {category!r}; expected 'otc' or 'real'")

    result = engine.predict(
        candles, ticks, micro, asset=asset,
        htf_trend=htf_trend, period=period,
        recent_accuracy=recent_accuracy)

    # Echo resolved category + deep-copy to isolate from engine internals.
    result = copy.deepcopy(result)
    result["category"] = category

    # Apply pair confidence penalty (preserves direction, dampens conviction).
    if _pair_mult < 1.0 and result.get("signal") in ("CALL", "PUT"):
        _orig_conf = result.get("confidence", 0)
        _new_conf = round(_orig_conf * _pair_mult)
        result["confidence"] = _new_conf
        if _new_conf < 25:
            result["signal"] = "NEUTRAL"
            result["confidence"] = 0
            result["strength"] = "NEUTRAL"
        if _pair_pen_reason:
            result.setdefault("reasons", []).append(
                f"{_pair_pen_reason} (confidence {_orig_conf} -> {_new_conf})")

    # Tiered high-confidence filter (env toggle: QX_TIERED_FILTER=1).
    try:
        if os.environ.get("QX_TIERED_FILTER", "0") == "1":
            from engines.base.tiered_filter import apply_tiered_filter
            _call_mods = []
            _put_mods = []
            _modules = result.get("modules") or {}
            if isinstance(_modules, dict):
                for _mod_name, _mod_data in _modules.items():
                    if isinstance(_mod_data, dict):
                        _mod_dir = _mod_data.get("direction")
                        if _mod_dir == "CALL":
                            _call_mods.append(_mod_name)
                        elif _mod_dir == "PUT":
                            _put_mods.append(_mod_name)
            result = apply_tiered_filter(
                result, asset,
                voting_modules_call=_call_mods,
                voting_modules_put=_put_mods)
    except Exception as _tier_exc:
        print(f"[engines] tiered-filter failed for {asset}: {_tier_exc}")

    return result


otc = _otc_engine
real = _real_engine
