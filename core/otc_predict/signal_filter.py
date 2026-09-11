"""core/otc_predict/signal_filter.py — signal scoring & confidence tiers
(PART 13 calibration hook + PART 14 score + PART 24 quality rules).

USER SPEC (PART 14 — Signal Score):

    ML prediction 50% + Momentum 15% + Trend 10% + Level reaction 10%
    + Volatility 5% + Candle structure 10% = 100 points
      80–100 → HIGH | 70–79 → GOOD | 60–69 → WATCH | <60 → NO SIGNAL
    এই thresholdগুলো fixed truth নয়; backtesting দিয়ে optimize করতে হবে।

PART 24 — Signal Quality Rules (all must pass, else NO SIGNAL):
    ✓ Live data available        ✓ Candle data complete   ✓ No data gap
    ✓ Model confidence sufficient ✓ Price action confirmation
    ✓ Volatility acceptable      ✓ No conflicting signals

PART 16 — Prediction Freeze: the caller persists the returned payload
ONCE (tracker.insert_prediction uses INSERT OR IGNORE on a UNIQUE key) —
a signal can never be edited after creation.

The probability fed in should be CALIBRATED (PART 13): models.py applies
Platt scaling on a time-ordered validation tail; this module treats the
input probability as an honest win-probability.
"""

import os

__all__ = ["score_signal", "TIER_ORDER", "tier_min_score"]

TIER_ORDER = ("HIGH", "GOOD", "WATCH", "NO_SIGNAL")

# PART 14 tier boundaries — env-tunable (walk-forward tuning), defaults
# follow the spec's 80/70/60 lines.
_TIER_SCORES = {
    "HIGH": int(os.environ.get("QX_PRED_TIER_HIGH", "80")),
    "GOOD": int(os.environ.get("QX_PRED_TIER_GOOD", "70")),
    "WATCH": int(os.environ.get("QX_PRED_TIER_WATCH", "60")),
}

# The minimum tier that may be EMITTED as a tradeable signal. The spec's
# core promise is "শুধু মাত্র উচ্চ confidence setup এ signal দিবে" — default
# GOOD (>=70). WATCH is recorded for tracking but displayed as NO TRADE.
_EMIT_TIER = os.environ.get("QX_PRED_EMIT_TIER", "GOOD")

# Weights per PART 14 (percent points)
_W = {"ml": 50, "momentum": 15, "trend": 10, "level": 10,
      "vol": 5, "structure": 10}


def tier_min_score(tier):
    return _TIER_SCORES.get(tier, 100)


def score_signal(probability, direction_up, pa, regime_info, quality):
    """Combine ML probability + price action into the 100-point score.

    Parameters
    ----------
    probability  : calibrated P(UP) from the model (PART 13)
    direction_up : ML direction (prob >= 0.5)
    pa           : price_action_confirm() output
    regime_info  : detect_regime() output
    quality      : dict of PART 24 booleans from the predictor
                   (data_complete, no_gap, model_loaded, vol_acceptable,
                   no_conflict)

    Returns payload dict:
        prediction CALL/PUT, probability, tier, score, emit (bool),
        reason (Bengali-friendly english code), components breakdown.
    """
    d = 1 if direction_up else -1
    c = pa.get("components", {})

    # ML component — distance from coin-flip, capped at 1.0
    ml_conf = min(1.0, max(0.0, abs(probability - 0.5) * 2.0))

    momentum = c.get("momentum", 0.5)
    trend = c.get("trend", 0.5)
    level = c.get("level", 0.5)
    structure = c.get("structure", 0.5)

    vol = 1.0
    if regime_info.get("regime") == "HIGH_VOL":
        vol = 0.0
    elif regime_info.get("vol_state") == "low":
        vol = 0.3

    score = round(_W["ml"] * ml_conf
                  + _W["momentum"] * momentum
                  + _W["trend"] * trend
                  + _W["level"] * level
                  + _W["vol"] * vol
                  + _W["structure"] * structure)

    # tier from the score lines (PART 14)
    if score >= _TIER_SCORES["HIGH"]:
        tier = "HIGH"
    elif score >= _TIER_SCORES["GOOD"]:
        tier = "GOOD"
    elif score >= _TIER_SCORES["WATCH"]:
        tier = "WATCH"
    else:
        tier = "NO_SIGNAL"

    # ── PART 24 quality gates ──────────────────────────────────────────
    failed = [k for k, v in (quality or {}).items() if not v]
    reasons = []
    if failed:
        reasons.append("quality_fail:" + ",".join(failed))
    if pa.get("veto"):
        reasons.append(f"veto:{pa['veto']}")
    if pa.get("against_count", 0) >= 3:
        reasons.append("pa_conflict")

    emit_tier_idx = TIER_ORDER.index(_EMIT_TIER) if _EMIT_TIER in TIER_ORDER else 1
    tier_idx = TIER_ORDER.index(tier)
    tier_ok = tier_idx <= emit_tier_idx and tier != "NO_SIGNAL"

    emit = bool(tier_ok and not failed
                and pa.get("veto") is None
                and pa.get("against_count", 0) < 3)

    if emit and not reasons:
        reasons.append(f"tier:{tier}")

    return {
        "prediction": "CALL" if d == 1 else "PUT",
        "probability": round(float(probability), 4),
        "tier": tier,
        "score": int(score),
        "emit": emit,
        "reason": ";".join(reasons) if reasons else "below_tier",
        "components": {
            "ml": round(ml_conf, 3), "momentum": momentum,
            "trend": trend, "level": level, "vol": vol,
            "structure": structure,
        },
        "pa_agreed": bool(pa.get("agreed")),
        "regime": regime_info.get("regime", "RANGING"),
    }
