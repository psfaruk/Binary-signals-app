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
"""
from engines.types import ModuleResult, MarketContext


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
    if pressure == "BUYER" and buy_pct >= 65:
        sub_votes.append(("CALL", 2, f"Strong buyer pressure ({buy_pct}%)"))
    elif pressure == "SELLER" and buy_pct <= 35:
        sub_votes.append(("PUT", 2, f"Strong seller pressure ({buy_pct}%)"))

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
    call_n = sum(1 for d, s, _ in sub_votes if d == "CALL")
    put_n = sum(1 for d, s, _ in sub_votes if d == "PUT")

    reasons_str = " | ".join(r for _, _, r in sub_votes)

    if call_sum > put_sum:
        composite_score = min(4, call_sum - put_sum)
        composite_type = "CONTINUATION" if (call_n > 0 and put_n == 0) else "REVERSAL"
        return [ModuleResult(
            module_name="running_tick", direction="CALL", score=composite_score,
            confidence=min(60, composite_score * 20),
            signal_type=composite_type, reliability="MICRO", group="MICRO",
            reasons=[f"Micro composite: {reasons_str}"])]
    elif put_sum > call_sum:
        composite_score = min(4, put_sum - call_sum)
        composite_type = "CONTINUATION" if (put_n > 0 and call_n == 0) else "REVERSAL"
        return [ModuleResult(
            module_name="running_tick", direction="PUT", score=composite_score,
            confidence=min(60, composite_score * 20),
            signal_type=composite_type, reliability="MICRO", group="MICRO",
            reasons=[f"Micro composite: {reasons_str}"])]

    return []  # exact tie — no vote
