"""
Module 2: Running Candle Tick Engine

Analyzes the running candle's tick-level microstructure. Collapses
3 sub-signals (ending direction, pressure, reaction) into ONE composite
vote to avoid confidence inflation.

Sub-signals:
  1. Ending direction (last 10 ticks — UP/BUYER or DOWN/SELLER)
  2. Buyer/seller pressure (tick-weighted volume, ≥65% = strong)
  3. Reaction (visited extreme then reversed)

All three come from the same tick data source → collapsed into 1 vote.

FIX (2026-07-18, structural bias): the old composite_type logic was
broken — it marked a vote as CONTINUATION only when ALL sub-votes
agreed (call_n > 0 and put_n == 0), and REVERSAL otherwise. This
conflated "sub-vote agreement" with "trend continuation", which are
completely different concepts. A mixed-vote CALL in a downtrend is
still a REVERSAL; a unanimous CALL in an uptrend is a CONTINUATION.

The new logic determines signal_type by comparing the composite vote
direction against the PRIOR CLOSED candle's body direction:
  - Vote direction AGREES with prior candle body → CONTINUATION
  - Vote direction OPPOSES prior candle body → REVERSAL
  - Prior candle was doji (no clear direction) → fall back to NEUTRAL
    classification based on streak agreement
"""
from engines.base.types import ModuleResult, MarketContext


def analyze(candles, ticks, micro, ctx: MarketContext) -> list:
    """Analyze running candle tick microstructure.

    Returns list with 0 or 1 ModuleResult (composite vote).
    """
    if not micro:
        return []

    sub_votes = []  # (direction, score, reason)

    # ── Sub-signal 1: Ending direction ───────────────────────────────────
    ed = micro.get("ending_direction", {})
    ed_dir = ed.get("direction", "FLAT")
    ed_dom = ed.get("dominance", "FIGHT")
    ed_buy = ed.get("buy_pct", 50)

    if ed_dir == "UP" and ed_dom == "BUYER":
        sub_votes.append(("CALL", 2, f"5-sec ending UP/BUYER ({ed_buy}%)"))
    elif ed_dir == "DOWN" and ed_dom == "SELLER":
        sub_votes.append(("PUT", 2, f"5-sec ending DOWN/SELLER ({ed_buy}%)"))

    # ── Sub-signal 2: Buyer/seller pressure ──────────────────────────────
    buy_pct = micro.get("buy_pct", 50)
    pressure = micro.get("pressure", "FIGHT")
    if pressure == "BUYER":
        score = 3 if buy_pct >= 70 else 2
        sub_votes.append(("CALL", score, f"Buyer pressure ({buy_pct}%)"))
    elif pressure == "SELLER":
        sell_pct = 100 - buy_pct
        score = 3 if sell_pct >= 70 else 2
        sub_votes.append(("PUT", score, f"Seller pressure ({sell_pct}%)"))

    # ── Sub-signal 3: Reaction ───────────────────────────────────────────
    reaction = micro.get("reaction")
    if reaction == "BUYER":
        sub_votes.append(("CALL", 2, "Buyer reaction from low"))
    elif reaction == "SELLER":
        sub_votes.append(("PUT", 2, "Seller reaction from high"))

    if not sub_votes:
        return []

    # ── Collapse into ONE composite vote ─────────────────────────────────
    call_sum = sum(s for d, s, _ in sub_votes if d == "CALL")
    put_sum = sum(s for d, s, _ in sub_votes if d == "PUT")

    reasons_str = " | ".join(r for _, _, r in sub_votes)

    if call_sum == put_sum:
        return []  # exact tie — no vote

    # FIX (2026-07-18): Determine composite_type by comparing the vote
    # direction against the PRIOR CLOSED candle's body direction, NOT by
    # whether sub-votes unanimously agreed. This is what CONTINUATION vs
    # REVERSAL actually means in the regime-weighting context.
    prior_dir = 0  # 1=up, -1=down, 0=doji/unknown
    if len(candles) >= 2:
        prev = candles[-2]
        prev_body = prev["close"] - prev["open"]
        if prev_body > 0:
            prior_dir = 1
        elif prev_body < 0:
            prior_dir = -1

    if call_sum > put_sum:
        composite_score = min(4, call_sum - put_sum)
        # FIX M1 (2026-07-19): removed dead `vote_dir = 1` — assigned but never read.
        if prior_dir == 1:
            composite_type = "CONTINUATION"  # ticks pushing up after up candle
            type_reason = "continues prior up"
        elif prior_dir == -1:
            composite_type = "REVERSAL"  # ticks pushing up after down candle
            type_reason = "reverses prior down"
        else:
            # Prior was doji — use streak alignment as fallback
            streak_dir = ctx.stats.get("streak_direction", 0)
            composite_type = "CONTINUATION" if streak_dir == 1 else "REVERSAL"
            type_reason = "prior doji, streak-aligned"
        return [ModuleResult(
            module_name="running_tick", direction="CALL", score=composite_score,
            confidence=min(60, composite_score * 20),
            signal_type=composite_type, reliability="MICRO", group="MICRO",
            reasons=[f"Micro composite CALL ({type_reason}): {reasons_str}"])]

    # put_sum > call_sum
    composite_score = min(4, put_sum - call_sum)
    # FIX M1 (2026-07-19): removed dead `vote_dir = -1` — assigned but never read.
    if prior_dir == -1:
        composite_type = "CONTINUATION"  # ticks pushing down after down candle
        type_reason = "continues prior down"
    elif prior_dir == 1:
        composite_type = "REVERSAL"  # ticks pushing down after up candle
        type_reason = "reverses prior up"
    else:
        streak_dir = ctx.stats.get("streak_direction", 0)
        composite_type = "CONTINUATION" if streak_dir == -1 else "REVERSAL"
        type_reason = "prior doji, streak-aligned"
    return [ModuleResult(
        module_name="running_tick", direction="PUT", score=composite_score,
        confidence=min(60, composite_score * 20),
        signal_type=composite_type, reliability="MICRO", group="MICRO",
        reasons=[f"Micro composite PUT ({type_reason}): {reasons_str}"])]
