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

EDGE-GUARD (2026-09-13) — "ডিরেকশন wrong দেখানো হয়, লস বেশি হচ্ছে":

The repo's own REAL-data backtest (14 days, 12 pairs) measured the ML
models at 49.9% y1 / 50.2% y2 — coin-flip — while the calibration table
shows the 0.55–0.65 band landing at 48.6% actual UP. The old emit rule
could still emit those signals because the non-ML components (momentum /
trend / level / structure / strategy = 60 points) lifted a weak-probability
row into the GOOD tier. Emission now demands EVIDENCE OF EDGE, not just
component agreement:

    1. VERIFIED model only (QX_PRED_REQUIRE_VERIFIED, default on) — a
       provisional model's rows stay frozen + tracked + graded, but they
       are display-only; it has not beaten its baselines yet.
    2. Calibrated probability beyond the emit band (QX_PRED_MIN_PROB,
       default 0.60): p in [0.40, 0.60) can never emit — exactly the band
       the calibration table proved anti-predictive.
    3. A p of exactly 0.5 (model missing / coin-flip) is NO SIGNAL — the
       old code called that direction CALL and drew a green candle for it.
    4. T+2 emission is OFF by default (QX_PRED_EMIT_T2=0): T+2 accuracy is
       statistically indistinguishable from coin-flip on real data, so the
       second horizon stays visible + tracked but not tradeable.
    5. SECOND VOICE: the classic strategies or the historical setup-match
       must agree with the ML direction (QX_PRED_SECOND_VOICE, default on)
       — Deep Report §V4's ensemble rule: one voice alone is not a signal.

Everything else (score, tiers, PART 24 gates, freeze) is unchanged — the
score still decides the TIER; the guards above decide EMISSION.
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

# ── EDGE-GUARD emission gates (2026-09-13) ────────────────────────────────
# A verified model whose calibrated probability clears the emit band AND
# has a second independent voice agreeing may emit. Defaults are strict
# because the measured real-data edge is ~zero — better to under-trade
# than to keep selling coin-flips as signals.
_REQUIRE_VERIFIED = os.environ.get("QX_PRED_REQUIRE_VERIFIED", "1") \
    not in ("0", "false", "no")
_MIN_PROB = float(os.environ.get("QX_PRED_MIN_PROB", "0.65"))
_EMIT_T2 = os.environ.get("QX_PRED_EMIT_T2", "0") not in ("0", "false", "no")
_SECOND_VOICE = os.environ.get("QX_PRED_SECOND_VOICE", "1") \
    not in ("0", "false", "no")

# Weights per PART 14 (percent points), UNIFIED-SIGNAL (2026-09-13):
# the user asked "পুরো সিস্টেম টি কে একটি সিস্টেম এর মধ্যে নিয়ে আসা যায়" —
# the 13 classic strategy modules now vote as a first-class component.
# PART 14's own text says the weights "fixed truth নয়; backtesting দিয়ে
# optimize করতে হবে", so ML's 50 points cede 10 to the new STRATEGY block:
#   ML 40 + Momentum 15 + Trend 10 + Level 10 + Vol 5 + Structure 10
#   + Strategy 10 = 100
# `strategy` = agreement of the classic strategies with the ML direction
# (1.0 = all 13 modules agree, 0.0 = all oppose, 0.5 = abstain/neutral).
_W = {"ml": 40, "momentum": 15, "trend": 10, "level": 10,
      "vol": 5, "structure": 10, "strategy": 10}


def tier_min_score(tier):
    return _TIER_SCORES.get(tier, 100)


def score_signal(probability, direction_up, pa, regime_info, quality,
                 strategy=None, model_status=None, horizon=1,
                 hist_agrees=None):
    """Combine ML probability + price action + classic strategies into the
    100-point UNIFIED score.

    Parameters
    ----------
    probability  : calibrated P(UP) from the model (PART 13)
    direction_up : ML direction (prob >= 0.5)
    pa           : price_action_confirm() output
    regime_info  : detect_regime() output
    quality      : dict of PART 24 booleans from the predictor
                   (data_complete, no_gap, model_loaded, vol_acceptable,
                   no_conflict)
    strategy     : UNIFIED-SIGNAL — the strategy_bridge summary dict for
                   this window ({direction, net, agree_count, against_count,
                   voters, per_module}). None/empty → neutral 0.5
                   contribution (old behaviour for non-unified callers).
    model_status : EDGE-GUARD — "verified" | "provisional" | None. Only a
                   VERIFIED model may emit; provisional rows stay frozen,
                   tracked and graded (display-only).
    horizon      : 1 or 2 — T+2 emission is off by default (its measured
                   accuracy is coin-flip; it stays visible + tracked).
    hist_agrees  : EDGE-GUARD second voice — True/False/None from the
                   historical setup-match engine's verdict vs the ML
                   direction (None = the engine abstained).

    Returns payload dict:
        prediction CALL/PUT, probability, tier, score, emit (bool),
        reason (Bengali-friendly english code), components breakdown,
        strategy_agree — the classic strategies' agreement with the final
        direction in [0,1] (displayed in the UI's unified verdict).
    """
    d = 1 if direction_up else -1
    c = pa.get("components", {})

    # ML component — distance from coin-flip, capped at 1.0
    ml_conf = min(1.0, max(0.0, abs(probability - 0.5) * 2.0))

    momentum = c.get("momentum", 0.5)
    trend = c.get("trend", 0.5)
    level = c.get("level", 0.5)
    structure = c.get("structure", 0.5)

    # UNIFIED-SIGNAL strategy component — agreement of the 13 classic
    # modules with the ML direction (0.5 neutral when they abstain).
    strat_agree = 0.5
    if strategy and strategy.get("voters"):
        strat_agree = min(1.0, max(0.0, 0.5 + 0.5 * strategy.get("net", 0.0) * d))

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
                  + _W["structure"] * structure
                  + _W["strategy"] * strat_agree)

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

    # ── EDGE-GUARD emission gates (2026-09-13) ────────────────────────
    # These run AFTER the legacy gates so the frozen row records WHY a
    # signal was suppressed (audit trail), never silently.
    edge_gates = {}
    if probability is not None and abs(float(probability) - 0.5) < 1e-9:
        edge_gates["coin_flip"] = False     # p == 0.5 has no direction
    if _REQUIRE_VERIFIED and model_status != "verified":
        edge_gates["model_not_verified"] = False
    if probability is not None:
        p = float(probability)
        if not (p >= _MIN_PROB or p <= 1.0 - _MIN_PROB):
            edge_gates["prob_below_band"] = False
    if horizon == 2 and not _EMIT_T2:
        edge_gates["t2_emit_disabled"] = False
    if _SECOND_VOICE:
        voices = []
        if strategy and strategy.get("voters"):
            voices.append((strategy.get("net", 0.0) * d) > 0)
        if hist_agrees is not None:
            voices.append(bool(hist_agrees))
        # Deep Report §V4 ensemble rule: EVERY available independent
        # voice must agree, and at least one must speak — a single lucky
        # agreement leaked through the fair-walk control (measured).
        if not voices or not all(voices):
            edge_gates["no_second_voice"] = False
    if edge_gates:
        emit = False
        reasons.append("edge_guard:" + ",".join(sorted(edge_gates)))

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
            "structure": structure, "strategy": round(strat_agree, 3),
        },
        "pa_agreed": bool(pa.get("agreed")),
        "regime": regime_info.get("regime", "RANGING"),
        # UNIFIED-SIGNAL: the classic strategies' verdict for this payload
        # (kept even when strategy=None — 0.5 = neutral).
        "strategy_agree": round(strat_agree, 3),
        # EDGE-GUARD: which emission gates failed (empty on emit)
        "edge_gates": edge_gates,
    }
