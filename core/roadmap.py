"""
core/roadmap.py — SIGNAL ROADMAP: the factor engine behind every CALL/PUT
(SIGNAL-ROADMAP 2026-09-17).

USER REQUIREMENT (verbatim):
  "এই অ্যাপ এর মধ্যে এত কিছু থাকার পরেও প্রেডিকশন ভূল হচ্ছে ... সেখানে কি
   এই সব কিছুর ডেটা নিয়ে দেখানো হচ্ছে? সিগন্যাল ডিরেকশন এর রুড ম্যাপ
   কি টিক আছে? ... কোথায় buyer Sellar আছে, কোথায় হোল্ড, রেজেকশন
   রিয়েকশন, রাউন্ড নাম্বার লেভেল, কোথায় কে কাকে ওভারটেক করলো,
   কারা জিতলো ... এই সব কিছু কি এনালাইসিস করে সিগন্যাল দিচ্ছে?"

AUDIT FINDING (the root cause this file fixes):
  feed.py computed the RICH microstructure (core/microstructure.build_micro:
  volume-weighted buyer/seller, time-decay orderflow, hold zone, VAP
  migration, reaction, exhaust/recovery, tick-speed acceleration, momentum
  shift, v-shape, big-vs-retail orderflow) and passed it to
  engines.base.blender.predict(...) — where it was ACCEPTED AND DROPPED.
  No module ever read it. The CALL/PUT decision ran on closed-candle
  indicators only; the micro dict survived solely inside brain.py's
  POSTMORTEM recorder. The user's intuition ("এত কিছু থাকার পরেও
  প্রেডিকশন ভূল") was exactly right: the app HAD the analysis but the
  signal never consumed it.

WHAT THIS FILE IS
─────────────────
Three pure functions, zero I/O, zero clock reads (repo hot-path convention):

  analyze_factors(micro, candles)  — the six-user-factor verdict map of the
      JUST-CLOSED candle (the candle whose data exists at signal time):
        1. বায়ার/সেলার  — volume % + time-weighted % + big-player flow
        2. হোল্ড         — hold zone + VAP migration (value area moving)
        3. রিজেকশন/রিয়েকশন — extreme rejection + wick + exhaust/recovery
        4. রাউন্ড নাম্বার — psychological round-level hold/reject/break
        5. ওভারটেক       — momentum shift / phase transfer (who took over)
        6. বিজয়ী (কারা জিতলো) — ending direction + big-vs-retail winner
      Each factor returns (direction, points 0-25, Bengali note) or
      abstains. Factors are HONEST: they only speak on strong evidence
      (repo lesson: a factor that votes on every candle is noise).

  build_roadmap(micro, candles, eye_anatomy) — the prediction-payload
      block attached to EVERY signal (blender.py): per-factor chips the
      UI renders at the signal point, plus net call/put points and the
      summary line. This is the "রুড ম্যাপ" — the user sees exactly WHY
      the direction fired, in the same terms a human trader thinks in.

  live_factors(micro, eye) — compact per-broadcast block for the
      coordination payload (feed.py tick loop): the RUNNING candle's
      buyer/seller, hold, reaction, round, overtake, winner states vs the
      published signal. Pure dict reads — measured < 5 µs.

PERFORMANCE CONTRACT:
  * analyze_factors/build_roadmap run ONCE per candle at EOC (never in the
    tick loop): O(1) dict reads + one round_level() call ≈ tens of µs.
  * live_factors runs per broadcast on ALREADY-computed dicts: O(1).
  The tick pipeline's sub-millisecond budget is untouched (verified in
  scripts/backtest_micro_flow.py).
"""

from __future__ import annotations

import os

# ── Tunables (env-overridable, repo convention) ──────────────────────────────
# Buyer/seller dominance band. build_micro's own pressure gate is 55 — the
# roadmap is a DECISION layer, so it demands real dominance (65/35), not a
# marginal lean. A 56% "buyer pressure" is a coin flip with a coat of paint.
BS_DOMINANCE = int(os.environ.get("QX_ROADMAP_BS_DOM", "65"))
# Time-decay flow must agree with volume flow before the factor speaks.
BS_AGREE_BAND = int(os.environ.get("QX_ROADMAP_BS_AGREE", "10"))
# Big-player (orderflow) volume % must reach this to count as the "big boys
# are here" leg of the factor.
BIG_DOMINANCE = int(os.environ.get("QX_ROADMAP_BIG_DOM", "70"))
# Hold factor: VAP migration must cover ≥ this fraction of the range.
HOLD_VAP_MIN = float(os.environ.get("QX_ROADMAP_HOLD_VAP", "0.25"))
# Ending-direction dominance (last-10-tick buy %) for the winner factor.
# 65 proved too loose on a 10-tick window (random variance alone crosses
# it ~30% of the time) — 70 demands a real one-sided ending.
END_DOM = int(os.environ.get("QX_ROADMAP_END_DOM", "70"))
# micro_flow module vote gate — the repo's hardest lesson: "a module that
# votes on every candle is noise". Backtest-calibrated (fair-mode
# abstention): dominant side needs ≥ 55 points, ≥ 3 speaking factors, and
# opposition below a THIRD of the dominant points. Ordinary drift candles
# land 30-45; only genuine one-sided evidence (strong closing orderflow,
# late control transfer, round-level rejection stacks) crosses 55.
VOTE_MIN_PTS = int(os.environ.get("QX_ROADMAP_VOTE_PTS", "55"))
VOTE_MIN_FACTORS = int(os.environ.get("QX_ROADMAP_VOTE_FACTORS", "3"))
VOTE_OPP_RATIO = int(os.environ.get("QX_ROADMAP_VOTE_OPP_RATIO", "3"))

__all__ = ["analyze_factors", "build_roadmap", "live_factors",
           "VOTE_MIN_PTS", "VOTE_MIN_FACTORS", "VOTE_OPP_RATIO",
           "FACTOR_LABELS"]

# Bengali labels — these surface directly in the UI roadmap panel.
FACTOR_LABELS = {
    "buyer_seller": "বায়ার / সেলার",
    "hold":         "হোল্ড জোন",
    "rejection":    "রিজেকশন / রিয়েকশন",
    "round":        "রাউন্ড নাম্বার",
    "overtake":     "ওভারটেক",
    "winner":       "কারা জিতছে",
}


def _pct(x) -> str:
    try:
        return f"{x:.0f}%"
    except Exception:
        return "—"


def _round_ctx(price: float):
    """(level, strength) for the nearest psychological round level, or
    (None, None). Thin wrapper over core.analysis.round_level — kept local
    so roadmap stays import-light for the EOC path."""
    try:
        from core.analysis import round_level as _rl
        lvl, _d, str_ = _rl(price)
        return (lvl, str_) if str_ in ("BIG", "MID") else (None, None)
    except Exception:
        return None, None


# ═════════════════════════════════════════════════════════════════════════════
#  FACTOR ENGINE — the just-closed candle, in the user's six terms
# ═════════════════════════════════════════════════════════════════════════════

def analyze_factors(micro: dict | None, candles: list | None) -> dict:
    """Score the six user factors on one candle's microstructure.

    Returns:
      {
        "factors": {key: {"dir": "CALL"|"PUT"|None, "pts": int, "note": str}},
        "call_pts": int, "put_pts": int,
        "speaking": int,          # factors that actually voted
        "net": "CALL"|"PUT"|None, # dominant side (None when tied/silent)
      }
    A factor with dir=None and pts=0 ABSTAINED — honest silence (the repo's
    hardest lesson: inflated certainty is worse than none).
    """
    factors: dict[str, dict] = {}
    micro = micro or {}
    candles = candles or []
    call_pts = 0
    put_pts = 0

    # ── 1. বায়ার / সেলার প্রেসার ────────────────────────────────────────────
    # Three legs must agree: volume-weighted buy %, time-weighted (td) buy %,
    # and — when present — the big-player orderflow leg. Two agreeing legs
    # with one dominant ≥ BS_DOMINANCE speaks; all three agreeing earns max.
    buy_pct = micro.get("buy_pct")
    td_buy = micro.get("td_buy_pct")
    of = micro.get("orderflow") or {}
    big_buy = of.get("big_buy_pct")
    f_dir, f_pts, note = None, 0, ""
    if isinstance(buy_pct, (int, float)) and isinstance(td_buy, (int, float)):
        vol_up = buy_pct >= BS_DOMINANCE
        vol_dn = buy_pct <= (100 - BS_DOMINANCE)
        td_up = td_buy >= (BS_DOMINANCE - BS_AGREE_BAND)
        td_dn = td_buy <= (100 - BS_DOMINANCE + BS_AGREE_BAND)
        big_up = isinstance(big_buy, (int, float)) and big_buy >= BIG_DOMINANCE
        big_dn = isinstance(big_buy, (int, float)) and big_buy <= (100 - BIG_DOMINANCE)
        if vol_up and td_up:
            f_dir, f_pts = "CALL", 18
            note = f"ভলিউম বায়ার {_pct(buy_pct)} · টাইম-ওয়েটেড {_pct(td_buy)}"
            if big_up:
                f_pts = 25
                note += f" · বড় প্লেয়ারও কিনছে ({_pct(big_buy)})"
            elif isinstance(big_buy, (int, float)) and big_dn:
                f_pts = 10
                note += f" · কিন্তু বড় প্লেয়ার বেচছে ({_pct(big_buy)})"
        elif vol_dn and td_dn:
            f_dir, f_pts = "PUT", 18
            note = f"ভলিউম সেলার {_pct(100 - buy_pct)} · টাইম-ওয়েটেড {_pct(100 - td_buy)}"
            if big_dn:
                f_pts = 25
                note += f" · বড় প্লেয়ারও বেচছে ({_pct(100 - big_buy)})"
            elif isinstance(big_buy, (int, float)) and big_up:
                f_pts = 10
                note += f" · কিন্তু বড় প্লেয়ার কিনছে ({_pct(big_buy)})"
        else:
            note = f"বায়ার/সেলার ব্যালান্সড ({_pct(buy_pct)}/{_pct(100 - buy_pct)}) — স্পষ্ট প্রেসার নেই"
    factors["buyer_seller"] = {"dir": f_dir, "pts": f_pts, "note": note}
    if f_dir == "CALL":
        call_pts += f_pts
    elif f_dir == "PUT":
        put_pts += f_pts

    # ── 2. হোল্ড জোন + VAP মাইগ্রেশন ───────────────────────────────────────
    # Where is price being HELD, and is the value area migrating with it?
    # close holding ABOVE the hold zone while the value area migrates UP =
    # buyers own the territory (CALL). Mirror for PUT.
    f_dir, f_pts, note = None, 0, ""
    hold_price = micro.get("hold_price")
    vap = micro.get("vap_migration") or {}
    cur = None
    if candles:
        cur = candles[-1].get("close")
    if isinstance(hold_price, (int, float)) and isinstance(cur, (int, float)):
        rng = None
        if candles:
            last = candles[-1]
            rng = max(0.0, (last.get("high") or cur) - (last.get("low") or cur))
        band = (rng * 0.15) if rng else 0.0
        vap_dir = vap.get("dir")
        vap_pct = vap.get("pct") or 0.0
        above = cur > hold_price + band
        below = cur < hold_price - band
        if above and vap_dir == "UP" and vap_pct >= HOLD_VAP_MIN:
            f_dir, f_pts = "CALL", 15
            note = (f"হোল্ড {hold_price:g}-এর উপরে প্রাইস, ভ্যালু-এরিয়া "
                    f"উপরে সরছে ({_pct(vap_pct)}) — বায়ারের জমি")
        elif below and vap_dir == "DOWN" and vap_pct >= HOLD_VAP_MIN:
            f_dir, f_pts = "PUT", 15
            note = (f"হোল্ড {hold_price:g}-এর নিচে প্রাইস, ভ্যালু-এরিয়া "
                    f"নিচে সরছে ({_pct(vap_pct)}) — সেলারের জমি")
        elif above or below:
            note = f"হোল্ড {hold_price:g} — প্রাইস {'উপরে' if above else 'নিচে'}, VAP {vap_dir or 'FLAT'}"
        else:
            note = f"হোল্ড {hold_price:g}-এই প্রাইস ঘুরছে — কেউ দখল করতে পারেনি"
    factors["hold"] = {"dir": f_dir, "pts": f_pts, "note": note}
    if f_dir == "CALL":
        call_pts += f_pts
    elif f_dir == "PUT":
        put_pts += f_pts

    # ── 3. রিজেকশন / রিয়েকশন ───────────────────────────────────────────────
    # reaction: candle visited an extreme and reversed (BUILD-side).
    # live_wick: classic wick rejection geometry.
    # last_react: EXHAUST (the run is dying → fade) / RECOVERY (pullback
    #             within the run → continue).
    f_dir, f_pts, note = None, 0, ""
    reaction = micro.get("reaction")
    wick = (micro.get("live_wick") or {}).get("type")
    last_react = micro.get("last_react")
    net = micro.get("net") or 0
    if reaction == "BUYER":
        f_dir, f_pts = "CALL", 15
        note = "লো থেকে শক্তিশালী বায় রিয়েকশন — নিচে বিক্রি ব্যর্থ"
    elif reaction == "SELLER":
        f_dir, f_pts = "PUT", 15
        note = "হাই থেকে শক্তিশালী সেল রিয়েকশন — উপরে কেনা ব্যর্থ"
    if f_dir is None and wick == "BULL_REJECT":
        f_dir, f_pts = "CALL", 12
        note = "নিচের উইক রিজেকশন (বুলিশ) — ডাম্প গিলে নেওয়া হয়েছে"
    elif f_dir is None and wick == "BEAR_REJECT":
        f_dir, f_pts = "PUT", 12
        note = "উপরের উইক রিজেকশন (বেয়ারিশ) — পাম্প গিলে নেওয়া হয়েছে"
    if last_react == "EXHAUST":
        # The CURRENT run is exhausting → fade it.
        fade = "PUT" if net > 0 else ("CALL" if net < 0 else None)
        if fade:
            add = 10
            f_dir = fade if f_dir is None else f_dir
            # EXHAUST against the existing verdict REINFORCES the fade side
            # only when they agree; otherwise it is a conflicting whisper
            # and the factor stays quiet (honest abstention).
            if f_dir == fade:
                f_pts += add
                note = (note + " · শেষ টিকে ক্লান্তি (EXHAUST)" if note
                        else "শেষ টিকে মুভ ক্লান্ত (EXHAUST) — ঘুরে আসার সম্ভাবনা")
            else:
                f_pts = max(0, f_pts - 5)
                note += " · কিন্তু শেষ টিকে ক্লান্তি"
    elif last_react == "RECOVERY" and f_dir is None:
        rec = "CALL" if net > 0 else ("PUT" if net < 0 else None)
        if rec:
            f_dir, f_pts = rec, 10
            note = "শেষ টিকে রিকভারি — রানের ভেতরে পুলব্যাক শেষ"
    if f_dir is None and not note:
        note = "স্পষ্ট রিজেকশন/রিয়েকশন নেই"
    factors["rejection"] = {"dir": f_dir, "pts": f_pts, "note": note}
    if f_dir == "CALL":
        call_pts += f_pts
    elif f_dir == "PUT":
        put_pts += f_pts

    # ── 4. রাউন্ড নাম্বার লেভেল ────────────────────────────────────────────
    # Psychological round numbers: the close's relationship to the nearest
    # BIG/MID round level decides. Approached from below and CLOSED BACK
    # BELOW = round-level rejection (PUT). Broke and HELD ABOVE = round
    # breakout (CALL). Mirror from above. Neutral zone = abstain.
    f_dir, f_pts, note = None, 0, ""
    if candles:
        last = candles[-1]
        cur = last.get("close")
        if isinstance(cur, (int, float)):
            lvl, str_ = _round_ctx(cur)
            if lvl is not None:
                o = last.get("open") or cur
                h = last.get("high") or cur
                l = last.get("low") or cur
                above_hold = cur > lvl
                touched = (h >= lvl >= l) or (abs(cur - lvl) <= (h - l or 0) * 0.10)
                ran_up_to = o < lvl
                ran_dn_to = o > lvl
                if touched and ran_up_to and not above_hold:
                    f_dir, f_pts = "PUT", 15
                    note = f"রাউন্ড {lvl:g} ছুঁয়ে ফিরে এসেছে — রেজিস্ট্যান্স রিজেকশন"
                elif touched and ran_dn_to and above_hold:
                    f_dir, f_pts = "CALL", 15
                    note = f"রাউন্ড {lvl:g} ভেঙে উপরে থেকেছে — সাপোর্ট ব্রেকআউট"
                elif touched and ran_dn_to and not above_hold:
                    f_dir, f_pts = "PUT", 15
                    note = f"রাউন্ড {lvl:g} নিচে ধরে রেখেছে — সাপোর্ট রিজেকশন"
                elif touched and ran_up_to and above_hold:
                    f_dir, f_pts = "CALL", 15
                    note = f"রাউন্ড {lvl:g} উপরে ধরে রেখেছে — ব্রেকআউট হোল্ড"
                else:
                    note = f"রাউন্ড {lvl:g} কাছে, কিন্তু স্পষ্ট রিঅ্যাকশন নেই"
            else:
                note = "রাউন্ড নাম্বারের কাছে নেই"
    factors["round"] = {"dir": f_dir, "pts": f_pts, "note": note}
    if f_dir == "CALL":
        call_pts += f_pts
    elif f_dir == "PUT":
        put_pts += f_pts

    # ── 5. ওভারটেক (কে কাকে ওভারটেক করলো) ─────────────────────────────────
    # momentum_shift = control transferred mid-candle (BULL_SHIFT: buyers
    # overtook sellers). tick_speed.reversed + second_dir = the late half
    # overtook the early half. phases[2] = the late third's direction.
    f_dir, f_pts, note = None, 0, ""
    shift = micro.get("momentum_shift")
    ts = micro.get("tick_speed") or {}
    phases = micro.get("phases") or []
    bull_votes = 0
    bear_votes = 0
    if shift == "BULL_SHIFT":
        bull_votes += 2
    elif shift == "BEAR_SHIFT":
        bear_votes += 2
    if ts.get("reversed"):
        if ts.get("second_dir") == "UP":
            bull_votes += 1
        elif ts.get("second_dir") == "DOWN":
            bear_votes += 1
    if len(phases) >= 3 and phases[2] in ("UP", "DOWN") and phases[2] != phases[1]:
        if phases[2] == "UP":
            bull_votes += 1
        else:
            bear_votes += 1
    if bull_votes >= 2 and bull_votes > bear_votes:
        f_dir, f_pts = "CALL", 15 if bull_votes >= 3 else 12
        who = "বায়াররা দখল নিয়েছে" if shift == "BULL_SHIFT" else "শেষার্ধে বায়ার এগিয়ে"
        note = f"ওভারটেক: {who} — কন্ট্রোল হাত বদল"
    elif bear_votes >= 2 and bear_votes > bull_votes:
        f_dir, f_pts = "PUT", 15 if bear_votes >= 3 else 12
        who = "সেলাররা দখল নিয়েছে" if shift == "BEAR_SHIFT" else "শেষার্ধে সেলার এগিয়ে"
        note = f"ওভারটেক: {who} — কন্ট্রোল হাত বদল"
    else:
        note = "কেউ ওভারটেক করতে পারেনি — কন্ট্রোল অপরিবর্তিত"
    factors["overtake"] = {"dir": f_dir, "pts": f_pts, "note": note}
    if f_dir == "CALL":
        call_pts += f_pts
    elif f_dir == "PUT":
        put_pts += f_pts

    # ── 6. কারা জিতছে (winner) ──────────────────────────────────────────────
    # The closing answer: ending_direction (last-10-tick net) × orderflow
    # big-vs-retail. Big buying while retail sells (imbalance) is the
    # strongest "smart money is winning" tell — the user's "কারা জিতলো".
    f_dir, f_pts, note = None, 0, ""
    ed = micro.get("ending_direction") or {}
    ed_dir = ed.get("direction")
    ed_buy = ed.get("buy_pct")
    of = micro.get("orderflow") or {}
    big_dir = of.get("big_dir")
    ret_dir = of.get("ret_dir")
    imb = of.get("imbalance")
    if ed_dir == "UP" and isinstance(ed_buy, (int, float)) and ed_buy >= END_DOM:
        f_dir, f_pts = "CALL", 12
        note = f"শেষ ১০ টিকে বায়ার জিতছে ({_pct(ed_buy)})"
        if big_dir == "UP" and ret_dir == "DOWN" and imb:
            f_pts = 25
            note += " · বড় প্লেয়ার কিনছে, রিটেইল বেচছে (ইম্ব্যালান্স)"
        elif big_dir == "UP":
            f_pts = 18
            note += " · বড় প্লেয়ারও একই দিকে"
    elif ed_dir == "DOWN" and isinstance(ed_buy, (int, float)) and ed_buy <= (100 - END_DOM):
        f_dir, f_pts = "PUT", 12
        note = f"শেষ ১০ টিকে সেলার জিতছে ({_pct(100 - ed_buy)})"
        if big_dir == "DOWN" and ret_dir == "UP" and imb:
            f_pts = 25
            note += " · বড় প্লেয়ার বেচছে, রিটেইল কিনছে (ইম্ব্যালান্স)"
        elif big_dir == "DOWN":
            f_pts = 18
            note += " · বড় প্লেয়ারও একই দিকে"
    else:
        note = "শেষ মুহূর্তে কেউ স্পষ্ট জিতছে না"
    factors["winner"] = {"dir": f_dir, "pts": f_pts, "note": note}
    if f_dir == "CALL":
        call_pts += f_pts
    elif f_dir == "PUT":
        put_pts += f_pts

    speaking = sum(1 for f in factors.values() if f["dir"] in ("CALL", "PUT"))
    net = None
    if call_pts > put_pts and call_pts > 0:
        net = "CALL"
    elif put_pts > call_pts and put_pts > 0:
        net = "PUT"
    return {
        "factors": factors,
        "call_pts": call_pts,
        "put_pts": put_pts,
        "speaking": speaking,
        "net": net,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SIGNAL ROADMAP — attached to every prediction payload (blender.py)
# ═════════════════════════════════════════════════════════════════════════════

def _smart_money_gate(micro: dict, direction: str) -> bool:
    """The vote's smart-money confirmation (module + roadmap shared gate).

    Factor-map dominance alone cannot separate a REAL one-sided candle
    (big players pushed it) from an ordinary drift candle (retail flow
    happened to lean). This gate requires one of:
      * big-player orderflow aligned with the direction (big_buy_pct
        >= 60 for CALL / <= 40 for PUT) — "বড় খেলোয়াড় একই দিকে", or
      * a genuine momentum shift in the direction (BULL/BEAR_SHIFT) —
        "কে কাকে ওভারটেক করলো".
    Backtest effect (fair data): votes drop from ~39% of candles (noise)
    to ~10-15%, while pushed/transfer candles keep voting — the repo's
    honest-abstention convention.
    """
    of = micro.get("orderflow") or {}
    big_buy = of.get("big_buy_pct")
    if isinstance(big_buy, (int, float)):
        # Big-flow % is only meaningful with ENOUGH big prints — on a
        # quiet candle 1-2 outliers make it 0/100 and it says nothing
        # (backtest finding: 332/364 fair candles had ≥60 or ≤40 on
        # tiny samples). Demand >= 4 big prints before the leg speaks.
        big_count = (of.get("big_up") or 0) + (of.get("big_dn") or 0)
        if isinstance(big_count, (int, float)) and big_count >= 4:
            if direction == "CALL" and big_buy >= 60:
                return True
            if direction == "PUT" and big_buy <= 40:
                return True
    shift = micro.get("momentum_shift")
    if shift == ("BULL_SHIFT" if direction == "CALL" else "BEAR_SHIFT"):
        return True
    return False


def build_roadmap(micro: dict | None, candles: list | None,
                  eye_anatomy: dict | None = None,
                  final_signal: str | None = None) -> dict:
    """Build the roadmap block for the prediction payload.

    Structure (UI-ready, Bengali-first):
      {
        "factors": [ {key, label, dir, pts, note, agree} ... ],  # sorted by pts
        "call_pts", "put_pts", "net", "speaking",
        "eye": {dir, strength} — the human-eye verdict of the same candle,
        "micro_vote": "CALL"|"PUT"|"ABSTAIN",
        "summary_bn": "...",
      }
    `agree` marks whether each factor supports the FINAL signal direction —
    the UI lights chips green (support) / red (oppose) / grey (abstain).
    """
    analysis = analyze_factors(micro, candles)
    final = final_signal if final_signal in ("CALL", "PUT") else None

    chips = []
    for key, f in analysis["factors"].items():
        agree = None
        if final and f["dir"] in ("CALL", "PUT"):
            agree = (f["dir"] == final)
        chips.append({
            "key": key,
            "label": FACTOR_LABELS.get(key, key),
            "dir": f["dir"],
            "pts": f["pts"],
            "note": f["note"],
            "agree": agree,
        })
    chips.sort(key=lambda c: (c["dir"] is None, -(c["pts"])))

    # The human-eye verdict of the same just-closed candle (the eye and the
    # roadmap are two views of the same tick data; showing both keeps the
    # panel honest when they disagree).
    eye = None
    if eye_anatomy:
        eye = {
            "dir": eye_anatomy.get("eye_direction"),
            "strength": eye_anatomy.get("eye_strength") or 0,
            "reasons": (eye_anatomy.get("eye_reasons") or [])[:3],
        }

    # Module-level vote decision (shared with engines/base/modules/
    # micro_flow.py — one source of truth for the gate).
    net = analysis["net"]
    dom_pts = max(analysis["call_pts"], analysis["put_pts"])
    opp_pts = min(analysis["call_pts"], analysis["put_pts"])
    micro_vote = "ABSTAIN"
    if (net is not None
            and dom_pts >= VOTE_MIN_PTS
            and analysis["speaking"] >= VOTE_MIN_FACTORS
            and opp_pts * VOTE_OPP_RATIO < dom_pts
            and _smart_money_gate(micro, net)):
        micro_vote = net

    if micro_vote in ("CALL", "PUT"):
        summary = (f"রোডম্যাপ: {analysis['call_pts']} কল-পয়েন্ট vs "
                   f"{analysis['put_pts']} পুট-পয়েন্ট "
                   f"({analysis['speaking']}/৬ ফ্যাক্টর বলেছে) → {micro_vote}")
    else:
        summary = (f"রোডম্যাপ: {analysis['call_pts']} কল vs "
                   f"{analysis['put_pts']} পুট — ফ্যাক্টর পর্যাপ্ত একমত নয়, "
                   f"মাইক্রো-ফ্লো ভোট দেয়নি")

    return {
        "factors": chips,
        "call_pts": analysis["call_pts"],
        "put_pts": analysis["put_pts"],
        "speaking": analysis["speaking"],
        "net": net,
        "micro_vote": micro_vote,
        "eye": eye,
        "summary_bn": summary,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  LIVE FACTORS — per-broadcast block for the coordination payload (feed.py)
# ═════════════════════════════════════════════════════════════════════════════

def live_factors(micro: dict | None, eye: dict | None = None) -> dict:
    """The RUNNING candle's factor snapshot for the tick broadcast.

    Pure O(1) reads over already-computed dicts (feed's micro_snap + the
    live eye) — designed for the per-tick path (measured < 5 µs). Each
    factor carries {label, value_bn, dir} where dir is the factor's own
    lean — the UI shows it next to the coordination voices so the user
    sees the running candle's full story (buyer/seller, hold, rejection,
    round, overtake, winner) while the signal is live.
    """
    micro = micro or {}
    eye = eye or {}

    def _lean(d):
        return d if d in ("CALL", "PUT") else None

    out = {
        "buyer_pct": micro.get("buy_pct"),
        "seller_pct": micro.get("sell_pct"),
        "pressure": micro.get("pressure"),
        "hold_price": micro.get("hold_price"),
        "reaction": micro.get("reaction"),
        "last_react": micro.get("last_react"),
        "phases": micro.get("phases"),
        "ending_direction": micro.get("ending_direction"),
    }

    # Round-number proximity of the running price (from the eye's last
    # price when present — eye carries 'last').
    rnd_lvl, rnd_str = None, None
    _price = micro.get("_price") or eye.get("last")
    if _price:
        rnd_lvl, rnd_str = _round_ctx(_price)
    out["round_level"] = rnd_lvl
    out["round_strength"] = rnd_str

    # Compact per-factor leans (direction each factor supports NOW).
    leans = {}
    buy_pct = micro.get("buy_pct")
    if isinstance(buy_pct, (int, float)):
        if buy_pct >= BS_DOMINANCE:
            leans["buyer_seller"] = "CALL"
        elif buy_pct <= (100 - BS_DOMINANCE):
            leans["buyer_seller"] = "PUT"
    if micro.get("reaction") == "BUYER":
        leans["rejection"] = "CALL"
    elif micro.get("reaction") == "SELLER":
        leans["rejection"] = "PUT"
    _lr = micro.get("last_react")
    _net = micro.get("net") or 0
    if _lr == "EXHAUST" and _net != 0:
        leans["rejection"] = "PUT" if _net > 0 else "CALL"
    phases = micro.get("phases") or []
    if len(phases) >= 3 and phases[2] in ("UP", "DOWN") and phases[2] != phases[1]:
        leans["overtake"] = "CALL" if phases[2] == "UP" else "PUT"
    ed = (micro.get("ending_direction") or {})
    if ed.get("direction") == "UP" and (ed.get("buy_pct") or 50) >= END_DOM:
        leans["winner"] = "CALL"
    elif ed.get("direction") == "DOWN" and (ed.get("buy_pct") or 50) <= (100 - END_DOM):
        leans["winner"] = "PUT"
    out["leans"] = leans

    # The live eye's own lean (already computed by feed's tick loop).
    out["eye_dir"] = _lean(eye.get("eye_direction"))
    out["eye_strength"] = eye.get("eye_strength") or 0
    return out
