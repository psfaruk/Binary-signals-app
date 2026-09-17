"""Module: MICRO-FLOW — the user's six-factor roadmap as a module vote
(SIGNAL-ROADMAP 2026-09-17).

USER REQUIREMENT (verbatim):
  "কোথায় buyer Sellar আছে, কোথায় হোল্ড, রেজেকশন রিয়েকশন, রাউন্ড নাম্বার
   লেভেল, কোথায় কে কাকে ওভারটেক করলো, কারা জিতলো ... এই সব কিছু কি
   এনালাইসিস করে সিগন্যাল দিচ্ছে?"

THE GAP THIS CLOSES:
  feed.py computed core.microstructure.build_micro (buyer/seller volume,
  time-decay orderflow, hold zone, VAP migration, reaction, exhaust,
  tick-speed, momentum shift, big-vs-retail flow) and passed it to the
  blender — where it was dropped. This module is the first consumer:
  core.roadmap.analyze_factors turns the micro dict into the six user
  factors, and THIS module converts a strong, one-sided factor map into
  exactly ONE net vote. The strict confluence engine
  (engines.base.confluence) then treats it like every other module vote —
  it can never emit a signal alone, it only adds/suppresses evidence.

VOTE GATE (honest abstention — repo's hardest lesson):
  * dominant side ≥ VOTE_MIN_PTS (default 40) points,
  * ≥ VOTE_MIN_FACTORS (default 2) factors actually spoke,
  * opposition below half the dominant points,
  * ≥ 20 ticks of data (below that the micro dict is noise).
  Otherwise the module abstains — a module that votes on every candle
  is noise, not evidence.

Reliability: MICRO (0.7 multiplier) — same evidence class as tickrun /
tick_eye. Default weight conservative (1.0); the per-pair adapter
calibrates from live graded signals.

signal_type: REVERSAL when the vote fights the closed candle's own color
(reaction/round rejection dominated), CONTINUATION when it agrees.
"""
from engines.base.types import ModuleResult, MarketContext
from core import roadmap as _roadmap

__all__ = ["analyze", "MIN_TICKS"]

# Minimum ticks in the just-closed candle for the micro dict to mean
# anything (build_micro itself returns None below 10; we demand more).
MIN_TICKS = 20


def analyze(candles, micro, ctx: MarketContext) -> list:
    """Convert the just-closed candle's microstructure into one net vote.

    `micro` is core.microstructure.build_micro's dict for the JUST-CLOSED
    candle (what feed.py passes the blender at EOC). `candles` supplies
    the OHLC needed by the round-number and hold factors.
    """
    if not micro or not candles:
        return []
    if (micro.get("tick_count") or 0) < MIN_TICKS:
        return []

    analysis = _roadmap.analyze_factors(micro, candles)
    net = analysis["net"]
    dom = max(analysis["call_pts"], analysis["put_pts"])
    opp = min(analysis["call_pts"], analysis["put_pts"])

    if (net not in ("CALL", "PUT")
            or dom < _roadmap.VOTE_MIN_PTS
            or analysis["speaking"] < _roadmap.VOTE_MIN_FACTORS
            or opp * _roadmap.VOTE_OPP_RATIO >= dom
            or not _roadmap._smart_money_gate(micro, net)):
        # Weak / mixed factor map, or dominance without smart-money
        # confirmation (plain retail drift) — honest abstention.
        return []

    # Score + confidence scale with how completely one side won the map.
    score = 2
    confidence = 55
    if dom >= 60 and analysis["speaking"] >= 4:
        score, confidence = 3, 62
    elif dom >= 50 and analysis["speaking"] >= 3:
        score, confidence = 3, 58

    # Trend-regime alignment bonus (same convention as candle_reaction /
    # tick_eye): continuation evidence is worth more when the regime agrees.
    regime = ctx.regime
    if regime.get("is_trending"):
        if net == "CALL" and regime.get("regime") == "TREND_UP":
            score, confidence = max(score, 3), max(confidence, 64)
        elif net == "PUT" and regime.get("regime") == "TREND_DOWN":
            score, confidence = max(score, 3), max(confidence, 64)

    # signal_type: fighting the candle's own color = reversal evidence.
    last_close = candles[-1].get("close") or 0.0
    last_open = candles[-1].get("open") or 0.0
    candle_up = last_close > last_open
    signal_type = "CONTINUATION" if (candle_up == (net == "CALL")) else "REVERSAL"

    # Reasons: the per-factor notes ARE the roadmap — surface the speaking
    # factors so signal_log / UI show exactly which factors fired.
    reasons = [
        f"MICRO-FLOW {net} (কল {analysis['call_pts']} vs পুট "
        f"{analysis['put_pts']}, {analysis['speaking']}/৬ ফ্যাক্টর):"]
    for key in ("buyer_seller", "hold", "rejection", "round",
                "overtake", "winner"):
        f = analysis["factors"].get(key) or {}
        if f.get("dir") in ("CALL", "PUT") and f.get("pts", 0) > 0:
            reasons.append(
                f"  {_roadmap.FACTOR_LABELS.get(key, key)}: {f['dir']} "
                f"({f['pts']} পি) — {f.get('note', '')}")

    return [ModuleResult(
        module_name="micro_flow",
        direction=net,
        score=score,
        confidence=confidence,
        signal_type=signal_type,
        reliability="MICRO",
        group="MICRO_FLOW",
        reasons=reasons,
    )]
