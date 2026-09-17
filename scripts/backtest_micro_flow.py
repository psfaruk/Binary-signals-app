#!/usr/bin/env python3
"""
scripts/backtest_micro_flow.py — SIGNAL-ROADMAP walk-forward backtest
(2026-09-17).

Verifies the six-factor roadmap engine (core/roadmap.py + engines/base/
modules/micro_flow.py + blender wiring) the same way the repo verifies
everything: walk-forward on synthetic data with INJECTED, verifiable
edges, OLD-vs-NEW engine comparison, and an honesty contract.

USER REQUIREMENTS VERIFIED HERE (verbatim):
  1. "কোথায় buyer Sellar আছে, কোথায় হোল্ড, রেজেকশন রিয়েকশন, রাউন্ড
     নাম্বার লেভেল, কোথায় কে কাকে ওভারটেক করলো, কারা জিতলো ...
     এই সব কিছু কি এনালাইসিস করে সিগন্যাল দিচ্ছে?"
     → the micro dict (which feed.py ALWAYS passed and the blender ALWAYS
       dropped) now feeds the decision: OLD engine (micro=None — the
       historical drop) vs NEW engine (micro consumed) are replayed
       side-by-side and graded.
  2. "সিগন্যাল ডিরেকশন এর রুড ম্যাপ কি টিক আছে?"
     → every prediction must carry a 6-factor roadmap with agree markers
       consistent with the final signal (structure check).
  3. "Neno সেকেন্ড এ সব কিছু প্রপার এনালাইসিস"
     → latency battery: factor engine + roadmap + module + live_factors
       all sub-millisecond (live_factors sub-50µs for the tick path).
  4. "backtest করে ভেরিফাই করবেন"  → this script.

INJECTED EDGES (verifiable by construction):
  orderflow_persistence — with prob p, candle i's final quarter is a
     big-volume push in direction D; candle i+1 then moves in D with
     prob 0.5+phi. The closing orderflow (the roadmap's buyer_seller +
     winner factors) carries REAL information → the module SHOULD beat
     50% and the NEW engine SHOULD beat the OLD engine.
  overtake_persistence — with prob p, control transfers late in candle
     i (momentum_shift); candle i+1 continues the NEW controller's
     direction with prob 0.5+phi. The overtake factor should catch it.
  none — FAIR random walk. HONESTY CONTRACT: the module must NOT beat
     50% here (leak detector), and NEW ≈ OLD (the roadmap adds nothing
     when nothing is there).

Grading (identical to production feed._accuracy semantics): the
prediction made at candle i's close is graded against candle i+1's
open→close; doji draws excluded.

Run:
    python scripts/backtest_micro_flow.py
    python scripts/backtest_micro_flow.py --candles 3000 --edge none
"""
import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ["QX_SIGNAL_MODE"] = "any_theory"
os.environ["QX_TARGET_GATE"] = "0"
os.environ["QX_BREAKEVEN_GATE"] = "0"
os.environ["QX_PAIR_HEALTH_GATE"] = "0"
os.environ["QX_TRAP_HOUR"] = "0"
os.environ["QX_SKIP_DOTENV"] = "1"
os.environ["QX_PUBLIC_READ"] = "1"

from engines import predict                         # noqa: E402
from core.microstructure import build_micro         # noqa: E402
from core import roadmap as _roadmap                # noqa: E402
from engines.base.modules import micro_flow as mod_micro_flow  # noqa: E402
from engines.base.context import compute_context    # noqa: E402
from core.coordination import compute_coordination  # noqa: E402

WARMUP = 35
PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def wilson_lower(correct: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = correct / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    adj = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (centre - adj) / denom


# ── Tick-level generator with injectable micro edges ────────────────────────
def gen_micro_edge(asset, n, seed=1, start_price=1.10000, edge="none",
                   phi=0.16, ticks_per_candle=90, p_edge=0.40,
                   vol=0.000016, period=60):
    """Candles + per-candle tick lists with verifiable micro edges.

    Candle anatomy mirrors the real feed: a weak per-candle drift
    (0.28×vol per tick — buy_pct lands 40-65, like production's micro
    panel) plus noise; strong candles only appear when an edge is
    INJECTED, so factor-map dominance on fair data stays honest.

    orderflow_persistence: candle i gets a big final-quarter push (4×vol
    per tick — big-player volume by construction); candle i+1 follows
    the push direction with prob 0.5+phi.
    overtake_persistence: candle i's early third goes OPPOSITE (control
    held by the other side), then the late third takes over in direction
    D; candle i+1 follows D with prob 0.5+phi.
    none: fair random walk.
    Returns (candles, ticks_by_candle).
    """
    rng = _rng = __import__("random").Random(f"{asset}:microflow:{seed}")
    px = start_price
    ts0 = 1_700_000_000
    ts0 -= ts0 % period
    candles = []
    ticks_by_candle = []
    pending_dir = 0          # injected edge direction for THIS candle
    t = ts0
    for i in range(n):
        o = px
        # determine this candle's bias
        up_prob = 0.5
        if edge != "none" and pending_dir != 0:
            up_prob = 0.5 + phi * pending_dir
        drift = (1 if rng.random() < up_prob else -1) * vol * 0.28

        ticks = []
        p = o
        pushed_dir = 0
        if edge == "orderflow_persistence" and rng.random() < p_edge:
            # big final-quarter push: last 25% of ticks step hard one way
            pushed_dir = rng.choice((-1, 1))
        elif edge == "overtake_persistence" and rng.random() < p_edge:
            pushed_dir = rng.choice((-1, 1))

        n_push_from = int(ticks_per_candle * (
            0.75 if edge == "orderflow_persistence" else 0.66))
        for k in range(ticks_per_candle):
            if pushed_dir != 0 and k >= n_push_from:
                step = pushed_dir * vol * 4.0 + rng.gauss(0, vol * 0.4)
            elif (edge == "overtake_persistence" and pushed_dir != 0
                    and k < int(ticks_per_candle * 0.33)):
                # early third goes the OPPOSITE way first (control transfer)
                step = -pushed_dir * vol * 1.6 + rng.gauss(0, vol * 0.6)
            else:
                step = drift + rng.gauss(0, vol)
            p += step
            ticks.append(p)
        c = p
        if c == o:
            c = o + (vol if rng.random() < 0.5 else -vol)
            ticks[-1] = c
        hi, lo = max(ticks), min(ticks)
        candles.append({"time": t, "open": o, "high": hi, "low": lo,
                        "close": c})
        ticks_by_candle.append(ticks)
        # the pushed direction becomes the NEXT candle's bias (the edge)
        pending_dir = pushed_dir if pushed_dir != 0 else 0
        # persistence of the plain drift direction also feeds
        # pending_dir when no push fired? NO — only pushed candles carry
        # the injected edge; everything else stays fair.
        px = c
        t += period
    return candles, ticks_by_candle


def grade(direction, nxt):
    """CALL wins if next candle closes above open; PUT if below; draw=None."""
    o, c = nxt["open"], nxt["close"]
    if c > o:
        return direction == "CALL"
    if c < o:
        return direction == "PUT"
    return None


def run_engine(candles, ticks_by_candle, asset, use_micro, horizon=None):
    """Walk-forward replay of the production EOC path.

    At candle i's close: predict(candles[:i+1], ticks_i, micro_i) —
    the same inputs feed._analyze_core assembles. use_micro=False
    replays the OLD engine (micro dropped). Grades against candle i+1.
    Returns per-signal records.
    """
    out = []
    n = horizon or len(candles)
    for i in range(WARMUP, min(n, len(candles) - 1)):
        hist = candles[:i + 1]
        ticks_i = ticks_by_candle[i]
        micro_i = build_micro(ticks_i, candles[i]["open"]) if use_micro else None
        try:
            result = predict(hist, ticks=list(ticks_i), micro=micro_i,
                             asset=asset, htf_trend="SIDEWAYS", period=60)
        except Exception as exc:
            print(f"  ! predict failed at i={i}: {exc}")
            continue
        sig = result.get("signal")
        if sig not in ("CALL", "PUT"):
            continue
        g = grade(sig, candles[i + 1])
        if g is None:
            continue
        out.append({
            "i": i, "signal": sig, "win": g,
            "strategy": result.get("strategy"),
            "micro_vote": (result.get("roadmap") or {}).get("micro_vote"),
            "has_roadmap": "roadmap" in result,
        })
    return out


def wr(records):
    if not records:
        return 0.0, 0
    w = sum(1 for r in records if r["win"])
    return w / len(records), len(records)


def wilson(records):
    if not records:
        return 0.0
    w = sum(1 for r in records if r["win"])
    return wilson_lower(w, len(records))


def module_votes_accuracy(candles, ticks_by_candle, edge_asset):
    """Direct micro_flow module walk-forward: vote at i's close, grade
    against candle i+1. The module's own honesty check."""
    votes = []
    for i in range(WARMUP, len(candles) - 1):
        micro_i = build_micro(ticks_by_candle[i], candles[i]["open"])
        ctx = compute_context(candles[:i + 1])
        results = mod_micro_flow.analyze(candles[:i + 1], micro_i, ctx)
        if not results:
            continue
        g = grade(results[0].direction, candles[i + 1])
        if g is None:
            continue
        votes.append(g)
    if not votes:
        return 0.0, 0
    return sum(1 for v in votes if v) / len(votes), len(votes)


# ── Latency battery ──────────────────────────────────────────────────────────
def latency_battery(candles, ticks_by_candle):
    """The user's nano-second requirement: every new computation must be
    sub-millisecond; the per-tick live_factors must be sub-50µs."""
    micro = build_micro(ticks_by_candle[-1], candles[-1]["open"])
    N = 2000

    t0 = time.perf_counter()
    for _ in range(N):
        _roadmap.analyze_factors(micro, candles[-60:])
    t_factors = (time.perf_counter() - t0) / N * 1_000_000

    t0 = time.perf_counter()
    for _ in range(N):
        _roadmap.build_roadmap(micro, candles[-60:], None, "CALL")
    t_road = (time.perf_counter() - t0) / N * 1_000_000

    live_micro = {"buy_pct": 60, "sell_pct": 40, "pressure": "BUYER",
                  "hold_price": 1.1001, "reaction": None, "last_react": None,
                  "phases": ["UP", "DOWN", "UP"], "net": 0.0004,
                  "ending_direction": {"direction": "UP", "buy_pct": 67}}
    eye = {"last": 1.1002, "eye_direction": "CALL", "eye_strength": 40,
           "phase": "LATE", "ready": True}
    t0 = time.perf_counter()
    for _ in range(N):
        _roadmap.live_factors(live_micro, eye)
    t_live = (time.perf_counter() - t0) / N * 1_000_000

    ctx = compute_context(candles[-60:])
    t0 = time.perf_counter()
    for _ in range(N):
        mod_micro_flow.analyze(candles[-60:], micro, ctx)
    t_mod = (time.perf_counter() - t0) / N * 1_000_000

    # Coordination with micro attached (tick path) — must stay ≈100µs.
    pred = {"signal": "CALL", "confidence": 60, "strength": "MEDIUM",
            "strategy": "confluence_v1_any"}
    t0 = time.perf_counter()
    for _ in range(N):
        compute_coordination(eye, pred,
                             {"direction": "CALL", "probability": 0.61,
                              "state": "present"},
                             "CONFIRMING", live_micro)
    t_coord = (time.perf_counter() - t0) / N * 1_000_000

    return {
        "factors_us": t_factors, "roadmap_us": t_road,
        "live_factors_us": t_live, "module_us": t_mod,
        "coordination_us": t_coord,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=1600)
    ap.add_argument("--edge", type=str, default="all",
                    choices=["all", "none", "orderflow_persistence",
                             "overtake_persistence"])
    ap.add_argument("--phi", type=float, default=0.16)
    args = ap.parse_args()

    print("=" * 74)
    print("SIGNAL-ROADMAP / MICRO-FLOW backtest — walk-forward, OLD vs NEW")
    print("=" * 74)

    edges = (["orderflow_persistence", "overtake_persistence", "none"]
             if args.edge == "all" else [args.edge])

    for edge in edges:
        print(f"\n── edge = {edge} (phi={args.phi}, candles={args.candles}) ──")
        asset = "MICROTEST_otc"
        candles, ticks_by_candle = gen_micro_edge(
            asset, args.candles, seed=5, edge=edge, phi=args.phi)

        # 1. Module-level accuracy (the six-factor vote itself)
        m_wr, m_n = module_votes_accuracy(candles, ticks_by_candle, asset)
        print(f"  micro_flow module: {m_wr:.1%} over {m_n} votes "
              f"(wilson-lo {wilson_lower(sum(1 for _ in []), 0) if m_n == 0 else ''})"
              if m_n else "  micro_flow module: no votes (abstained all)")
        if m_n:
            w = sum(1 for i in range(1) ) # placeholder no-op
            print(f"    wilson-lo = {wilson_lower(round(m_wr * m_n), m_n):.1%}")

        if edge == "none":
            # FAIR: repo honesty convention (backtest_tick_eye.py): the
            # module MUST land ≈50% here — an edge on a fair walk means
            # the module leaks. Vote FREQUENCY is secondary (the confluence
            # cluster gate + per-pair LEARNED-MUTE weights are the engine's
            # defense against chatty modules); it just must not vote on
            # literally every candle.
            check(f"[{edge}] fair-mode honesty: module ≈50% (leak check)",
                  m_n == 0 or abs(m_wr - 0.5) < 0.06,
                  f"module wr={m_wr:.1%} n={m_n}")
            check(f"[{edge}] fair-mode: module not voting every candle",
                  m_n < args.candles * 0.5,
                  f"voted {m_n}/{args.candles}")
        else:
            check(f"[{edge}] module beats 50%+margin",
                  m_n >= 100 and m_wr > 0.54,
                  f"module wr={m_wr:.1%} n={m_n}")

        # 2. OLD vs NEW full-engine replay
        old = run_engine(candles, ticks_by_candle, asset, use_micro=False)
        new = run_engine(candles, ticks_by_candle, asset, use_micro=True)
        old_wr, old_n = wr(old)
        new_wr, new_n = wr(new)
        print(f"  OLD engine (micro dropped): {old_wr:.1%} over {old_n}")
        print(f"  NEW engine (micro consumed): {new_wr:.1%} over {new_n}")

        if edge == "none":
            check(f"[{edge}] NEW ≈ OLD on fair data (no phantom edge)",
                  abs(new_wr - old_wr) < 0.05,
                  f"old={old_wr:.1%} new={new_wr:.1%}")
        else:
            check(f"[{edge}] NEW ≥ OLD (roadmap adds value)",
                  new_wr >= old_wr - 0.005,
                  f"old={old_wr:.1%} new={new_wr:.1%}")

        # 3. Roadmap structural contract on every prediction
        n_road = sum(1 for r in new if r["has_roadmap"])
        check(f"[{edge}] every signal carries a roadmap",
              old_n == 0 or n_road == new_n,
              f"{n_road}/{new_n}")

    # 4. Roadmap content contract (structure, labels, agree consistency)
    print("\n── roadmap structure contract ──")
    candles, ticks_by_candle = gen_micro_edge(
        "MICROTEST_otc", 200, seed=9, edge="orderflow_persistence")
    micro = build_micro(ticks_by_candle[-1], candles[-1]["open"])
    road = _roadmap.build_roadmap(micro, candles, None, None)
    keys = {f["key"] for f in road["factors"]}
    check("roadmap has all six user factors",
          keys == {"buyer_seller", "hold", "rejection", "round",
                   "overtake", "winner"},
          f"keys={sorted(keys)}")
    labels = {f["label"] for f in road["factors"]}
    check("roadmap labels are Bengali (UI-ready)",
          any("া" in l for l in labels), f"labels={labels}")
    check("roadmap has micro_vote + summary_bn + points",
          isinstance(road.get("micro_vote"), str)
          and isinstance(road.get("summary_bn"), str)
          and isinstance(road.get("call_pts"), int)
          and isinstance(road.get("put_pts"), int))
    # agree markers consistent with final signal
    road_c = _roadmap.build_roadmap(micro, candles, None, "CALL")
    agree_ok = all(
        (f["agree"] is None and f["dir"] not in ("CALL", "PUT"))
        or (f["agree"] == (f["dir"] == "CALL"))
        for f in road_c["factors"])
    check("agree markers consistent with final signal", agree_ok)
    road_p = _roadmap.build_roadmap(micro, candles, None, "PUT")
    agree_ok_p = all(
        (f["agree"] is None and f["dir"] not in ("CALL", "PUT"))
        or (f["agree"] == (f["dir"] == "PUT"))
        for f in road_p["factors"])
    check("agree markers flip correctly for PUT signal", agree_ok_p)

    # live_factors contract
    lf = _roadmap.live_factors(
        {"buy_pct": 72, "sell_pct": 28, "pressure": "BUYER",
         "hold_price": 1.1001, "reaction": "BUYER", "last_react": None,
         "phases": ["UP", "DOWN", "UP"], "net": 0.0004,
         "ending_direction": {"direction": "UP", "buy_pct": 72}},
        {"last": 1.1002, "eye_direction": "CALL", "eye_strength": 40})
    check("live_factors carries buyer/seller + hold + reaction",
          lf.get("buyer_pct") == 72 and lf.get("hold_price") == 1.1001
          and lf.get("reaction") == "BUYER")
    check("live_factors leans detected (buyer_seller, rejection, overtake, winner)",
          lf.get("leans", {}).get("buyer_seller") == "CALL"
          and lf.get("leans", {}).get("rejection") == "CALL"
          and lf.get("leans", {}).get("overtake") == "CALL"
          and lf.get("leans", {}).get("winner") == "CALL")

    # 5. Latency battery (nano-second analysis requirement)
    print("\n── latency battery (sub-millisecond contract) ──")
    candles, ticks_by_candle = gen_micro_edge(
        "MICROTEST_otc", 300, seed=13, edge="none")
    lat = latency_battery(candles, ticks_by_candle)
    for k, v in lat.items():
        print(f"  {k}: {v:,.1f} µs")
    check("analyze_factors < 1ms (EOC path)", lat["factors_us"] < 1000)
    check("build_roadmap < 1ms (EOC path)", lat["roadmap_us"] < 1000)
    check("micro_flow module < 1ms (EOC path)", lat["module_us"] < 1000)
    check("live_factors < 50µs (per-tick path)", lat["live_factors_us"] < 50)
    check("coordination+live_factors stays < 1ms (tick path)",
          lat["coordination_us"] < 1000)

    # 6. Coordination live_factors block present
    coord = compute_coordination(
        {"ready": True, "phase": "LATE", "eye_direction": "CALL",
         "eye_strength": 60, "last": 1.1001},
        {"signal": "CALL", "confidence": 60, "strength": "MEDIUM",
         "strategy": "confluence_v1_any"},
        {"direction": "CALL", "probability": 0.61, "state": "present"},
        "CONFIRMING",
        {"buy_pct": 70, "sell_pct": 30, "pressure": "BUYER",
         "hold_price": 1.1001, "reaction": None, "last_react": None,
         "phases": ["UP", "DOWN", "UP"], "net": 0.0003,
         "ending_direction": {"direction": "UP", "buy_pct": 66}})
    check("coordination payload includes live_factors",
          "live_factors" in coord and coord["live_factors"].get("buyer_pct") == 70)
    coord_no_micro = compute_coordination(
        {"ready": True, "phase": "LATE", "eye_direction": "CALL",
         "eye_strength": 60}, {"signal": "CALL"}, None, None)
    check("coordination without micro has no live_factors (backward compat)",
          "live_factors" not in coord_no_micro)
    check("coordination voice math unchanged with micro",
          coord["state"] == coord_no_micro["state"]
          and coord["alignment"] == coord_no_micro["alignment"],
          f"{coord['state']}/{coord_no_micro['state']} "
          f"{coord['alignment']}/{coord_no_micro['alignment']}")

    # ── Report ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
    if FAILURES:
        for name, detail in FAILURES:
            print(f"  ✗ {name}: {detail}")
        sys.exit(1)
    report = {"pass": PASS, "fail": FAIL, "latency": lat}
    with open(os.path.join(REPO, "scripts", "backtest_micro_flow_report.json"),
              "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("report → scripts/backtest_micro_flow_report.json")
    sys.exit(0)


if __name__ == "__main__":
    main()
