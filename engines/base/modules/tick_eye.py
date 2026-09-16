"""Module: TICK-EYE — the human-eye tick anatomy as a prediction module
(TICK-EYE 2026-09-16).

Wraps core.tick_eye.analyze_candle_ticks on the JUST-CLOSED candle's tick
sequence (base_ticks at EOC) and converts STRONG eye evidence into ONE net
module vote. The strict confluence engine (engines.base.confluence) then
treats it exactly like every other module vote — it can never emit a signal
on its own, it only adds/suppresses evidence.

WHAT COUNTS AS STRONG (vote emitted):
  A) REAL late flip          — control transfer confirmed by travel + tick
                               count (user's 57-58s RED → 59-60s GREEN case)
  B) Decisive ending velocity + aligned ending flow
  C) Late wick rejection + aligned close position

Anything less → the module abstains (no vote). This follows the repo's
hardest lesson (see core/constants.py): a module that votes on every candle
is noise; a module that votes only on strong evidence can actually help.

Reliability: MICRO (0.7 multiplier) — tick-anatomy evidence is real but the
weakest-confidence class, matching tickrun. Default weight is conservative
(1.0) and the per-pair adapter will calibrate it from live win rates.
"""
from engines.base.types import ModuleResult, MarketContext
from core import tick_eye as _eye

__all__ = ["analyze"]

# Minimum eye-strength (0-100 honest scale, see core.tick_eye.eye_verdict)
# for the module to vote at all.
MIN_EYE_STRENGTH = 45      # ≥ 2 aligned eye-signals (e.g. flip+flow)
STRONG_EYE_STRENGTH = 60   # ≥ 3 aligned eye-signals → higher confidence


def analyze(candles, ticks, ctx: MarketContext) -> list:
    """Run the human-eye anatomy on the just-closed candle's ticks.

    `ticks` is the closed candle's raw tick sequence (base_ticks at EOC —
    the same input tickrun receives). `candles` is unused for the anatomy
    itself but kept for signature symmetry + ATR context.
    """
    if not ticks or len(ticks) < _eye.MIN_TICKS or not candles:
        return []

    last = candles[-1]
    anatomy = _eye.analyze_candle_ticks(ticks, last.get("open"), period=60)
    if anatomy is None:
        return []

    direction = anatomy.get("eye_direction")
    strength = anatomy.get("eye_strength") or 0
    if direction not in ("CALL", "PUT") or strength < MIN_EYE_STRENGTH:
        # Weak/neutral eye — abstain honestly.
        return []

    confidence = 55 if strength < STRONG_EYE_STRENGTH else 62
    score = 2 if strength < STRONG_EYE_STRENGTH else 3

    # Trend alignment bonus (same convention as candle_reaction):
    # the eye's continuation evidence is worth more when the regime agrees.
    regime = ctx.regime
    if regime.get("is_trending"):
        if direction == "CALL" and regime.get("regime") == "TREND_UP":
            score, confidence = 3, 64
        elif direction == "PUT" and regime.get("regime") == "TREND_DOWN":
            score, confidence = 3, 64

    reasons = list(anatomy.get("eye_reasons") or [])
    reasons.insert(
        0, f"TICK-EYE শক্তি {strength}% — বন্ধ ক্যান্ডেলের টিক-অ্যানাটমি "
           f"({anatomy.get('tick_count')} টিক, ফাইনাল সেগমেন্ট "
           f"{anatomy.get('final_ticks')} টিক)")

    return [ModuleResult(
        module_name="tick_eye",
        direction=direction,
        score=score,
        confidence=confidence,
        signal_type="CONTINUATION",
        reliability="MICRO",
        group="TICK_EYE",
        reasons=reasons,
    )]
