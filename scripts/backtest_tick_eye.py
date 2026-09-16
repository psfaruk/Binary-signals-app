#!/usr/bin/env python3
"""
scripts/backtest_tick_eye.py — TICK-EYE walk-forward backtest (2026-09-16).

Verifies the human-eye tick module (engines/base/modules/tick_eye.py,
backed by core/tick_eye.py) the same way the repo verifies everything:
walk-forward on synthetic data with INJECTED, verifiable edges.

USER REQUIREMENTS VERIFIED HERE (verbatim):
  1. "লাস্ট 10 সেকেন্ড এ একটি ক্যান্ডেল এ কি ঘটে ... অনেক কিছু
     কনফার্মেশন আছে?"            → the eye's final-segment anatomy
     (velocity / flow / flip / burst / wick) is measured and voted on.
  2. "একটি ক্যান্ডেল যদি লাস্ট সেকেন্ড এ ক্যান্ডেল এর কালার পরিবর্তন
     করে ... এটার লজিক কি?"       → REAL flips (multi-tick push) are
     read as continuation; 1-2-tick print spikes are read as mean-
     reverting noise. Both are tested separately below.
  3. "backtest করে ভেরিফাই করবেন"  → this script.

METHOD — walk-forward, mirrors the live EOC path exactly:
  * At candle i's close the eye sees candle i's ticks ONLY (base_ticks).
  * Its vote is graded against candle i+1's open→close (feed._accuracy
    semantics; draws excluded).
  * Module votes require eye_strength >= 45 (the module's own gate) —
    exactly the production threshold.

HONESTY CONTRACT (same as backtest_any_theory.py):
  * edge="none"           → FAIR random walk. The eye MUST land ≈50%.
                            If it shows an edge here, the module leaks.
  * edge="flip_persistence" → real late flips continue into the next
                            candle (phi). The eye SHOULD beat 50%.
  * edge="spike_noise"    → 1-2-tick giant spikes mean-revert (phi).
                            The eye's spike-noise read SHOULD beat 50%.

Run:
    python scripts/backtest_tick_eye.py
    python scripts/backtest_tick_eye.py --candles 4000 --edge none
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

from engines.base.modules import tick_eye as mod_tick_eye  # noqa: E402
from engines.base.context import compute_context           # noqa: E402
from scripts.synthetic_otc import gen_candles_with_ticks   # noqa: E402

WARMUP = 35


def wilson_lower(correct: int, total: int, z: float = 1.96) -> float:
    """Wilson 95% lower bound — the repo's standard honesty statistic."""
    if total <= 0:
        return 0.0
    p = correct / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    adj = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (centre - adj) / denom


def run_edge(edge: str, candles: int, seed: int, phi: float = 0.12,
             flip_prob: float = 0.30) -> dict:
    data = gen_candles_with_ticks(
        "EURUSD_otc", candles, seed=seed, edge=edge, phi=phi,
        flip_prob=flip_prob)

    voted = correct = wrong = draws = 0
    flips_seen = real_flips = spike_flips = 0
    # Conditional tracking: WR of votes cast ON flip-candles specifically —
    # isolates the mechanism from the bulk of ordinary votes.
    cond = {"real": [0, 0], "spike": [0, 0], "noflip": [0, 0]}
    by_bucket = defaultdict(lambda: [0, 0])   # eye_strength bucket -> [ok, n]
    latencies = []

    for i in range(WARMUP, len(data) - 1):
        t0 = time.perf_counter()
        closed_hist = [d[0] for d in data[:i + 1]]
        cur_candle, cur_ticks = data[i]
        nxt = data[i + 1][0]

        # ── Module path (production code, unmodified) ───────────────────────
        ctx = compute_context(closed_hist[-120:])
        votes = mod_tick_eye.analyze(closed_hist, cur_ticks, ctx)

        # Bookkeeping: flip anatomy stats for the report
        from core.tick_eye import analyze_candle_ticks
        an = analyze_candle_ticks(cur_ticks, cur_candle["open"], 60)
        flip_kind = "noflip"
        if an and an.get("late_flip"):
            flips_seen += 1
            if an["late_flip"].get("is_real"):
                real_flips += 1
                flip_kind = "real"
            elif an["late_flip"].get("is_spike_noise"):
                spike_flips += 1
                flip_kind = "spike"

        if not votes:
            continue
        v = votes[0]
        if v.direction not in ("CALL", "PUT"):
            continue

        voted += 1
        # Grade: next candle open → close
        if nxt["close"] > nxt["open"]:
            actual = "CALL"
        elif nxt["close"] < nxt["open"]:
            actual = "PUT"
        else:
            draws += 1
            continue
        ok = (v.direction == actual)
        if ok:
            correct += 1
        else:
            wrong += 1
        cond[flip_kind][0] += ok
        cond[flip_kind][1] += 1

        bucket = min(80, (v.confidence // 10) * 10)
        by_bucket[bucket][0] += ok
        by_bucket[bucket][1] += 1
        latencies.append((time.perf_counter() - t0) * 1000)

    total = correct + wrong
    wr = (correct / total * 100) if total else 0.0
    lb = wilson_lower(correct, total) * 100
    cond_out = {}
    for kind, (ok, n) in cond.items():
        cond_out[kind] = {
            "n": n,
            "wr": round(ok / n * 100, 2) if n else None,
            "wilson_lb": round(wilson_lower(ok, n) * 100, 2) if n else None,
        }
    return {
        "edge": edge,
        "candles": candles,
        "seed": seed,
        "phi": phi,
        "flip_prob": flip_prob,
        "votes": voted,
        "coverage_pct": round(voted / max(1, len(data) - 1 - WARMUP) * 100, 1),
        "correct": correct,
        "wrong": wrong,
        "draws": draws,
        "win_rate_pct": round(wr, 2),
        "wilson_lb_pct": round(lb, 2),
        "flips_seen": flips_seen,
        "real_flips": real_flips,
        "spike_flips": spike_flips,
        "conditional": cond_out,
        "avg_module_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0,
        "by_confidence_bucket": {
            str(k): {"wr": round(v[0] / max(1, v[1]) * 100, 1), "n": v[1]}
            for k, v in sorted(by_bucket.items())},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--edge", default="all",
                    choices=["all", "none", "flip_persistence", "spike_noise"])
    args = ap.parse_args()

    edges = (["none", "flip_persistence", "spike_noise"]
             if args.edge == "all" else [args.edge])

    print(f"{'edge':<18} {'votes':>6} {'WR%':>7} {'LB95%':>7}  verdict")
    print("-" * 74)
    results = {}
    for edge in edges:
        r = run_edge(edge, args.candles, args.seed)
        results[edge] = r
        if edge == "none":
            ok = abs(r["win_rate_pct"] - 50.0) < 3.0
            verdict = ("FAIR ✓ (~50%, no leak)" if ok
                       else "⚠ DEVIATES FROM 50% — CHECK FOR LEAK")
        else:
            # Mechanism check: the votes cast on the relevant flip-candles
            # must beat 50% with Wilson LB — isolates the eye's flip logic
            # from the bulk of ordinary (coin-flip) votes.
            kind = "real" if edge == "flip_persistence" else "spike"
            c = r["conditional"].get(kind) or {}
            clb = c.get("wilson_lb")
            cn = c.get("n") or 0
            if clb is not None and cn >= 30 and clb > 50.0:
                verdict = (f"EDGE CAPTURED ✓ (on-{kind}-flip votes: "
                           f"WR {c.get('wr')}%, LB {clb}%, n={cn})")
            else:
                verdict = (f"✗ edge NOT captured (on-{kind}-flip votes: "
                           f"WR {c.get('wr')}%, LB {clb}, n={cn})")
        print(f"{edge:<18} {r['votes']:>6} {r['win_rate_pct']:>7.2f} "
              f"{r['wilson_lb_pct']:>7.2f}  {verdict}")
        c = r["conditional"]
        print(f"   overall coverage={r['coverage_pct']}%  | on-real-flip: "
              f"n={c['real']['n']} WR={c['real']['wr']}%  | on-spike-flip: "
              f"n={c['spike']['n']} WR={c['spike']['wr']}%  | "
              f"latency={r['avg_module_latency_ms']}ms")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "backtest_tick_eye_report.json")
    with open(out, "w") as f:
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "candles": args.candles, "results": results}, f, indent=2)
    print(f"\nreport → {out}")
    return results


if __name__ == "__main__":
    main()
