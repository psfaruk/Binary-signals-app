#!/usr/bin/env python3
"""
scripts/backtest_every_candle.py — offline backtest of the EVERY-CANDLE mode.

USER REQUIREMENTS VERIFIED HERE (2026-09-07):
  1. "প্রত্যেক ক্যান্ডেল এ সিগন্যাল লাগবে"        → coverage must be 100%
  2. "প্রত্যেক পেয়ার এ কল ও put আলাদা উইন রেট"   → per-pair, per-direction
                                                    (CALL/PUT) win rates
  3. "প্রত্যেকটি সিগন্যাল হিস্টোরি"               → every signal graded +
                                                    recorded with its ctime

Method — walk-forward replay (FIXED 2026-09-07, was look-ahead biased):
  * Synthetic OHLC series per pair (GBM with mean reversion + regime drift;
    the SAME generator the repo's smoke test uses, extended with more seeds).
  * At candle i (>= warmup), the engine sees candles[:i] ONLY — strictly
    BEFORE candle i, mirroring the live pipeline: feed.py closes candle N-1,
    then _run_eoc builds the prediction for candle N from the CLOSED candles
    (< N). The old window (candles[:i+1]) handed the engine candle i
    INCLUDING ITS CLOSE and then graded on that same candle — the 81.2%
    result in backtest_every_candle_results.json was an artifact of reading
    the answer, not engine edge.
  * The signal is graded against candle i's own open→close move
    (signal issued at candle open, settled at candle close = 1-minute expiry,
    identical to feed.py _accuracy()).
  * Every graded signal is appended to the in-memory history with its ctime —
    mirroring db.signal_log rows.

⚠ HONESTY NOTE on absolute win rates: the synthetic generator has strong
  mean-reversion structure (drift pulls price back to base), which the
  engine's mean-reversion modules can genuinely learn — so absolute WR
  here (often 70-80%) is INFLATED vs real markets. This backtest verifies
  MECHANICS: 100% coverage, zero errors, per-pair CALL/PUT separation,
  correct grading, and a complete signal history — NOT real-market edge.
  Real-market quality is tracked live via /api/winrate (per-pair,
  per-direction, draws excluded).

Outputs:
  * Per-pair: total / CALL / PUT win rates, draws, coverage
  * Per-quality-tier: strict vs fallback win rates
  * JSON dump to scripts/backtest_every_candle_results.json

Run:
    python scripts/backtest_every_candle.py
    python scripts/backtest_every_candle.py --candles 400 --pairs EURUSD,USDZAR_otc
"""
import argparse
import json
import os
import random
import sys
import time
import zlib
from collections import defaultdict
from typing import List, Dict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.setdefault("QX_SIGNAL_MODE", "every_candle")

from engines import predict
from engines.base import confluence as cf_mod

WARMUP = 35          # first candle index that gets a signal (matches smoke test)
WINDOW = 300         # engine window cap (matches feed.py snapshot cap)


def gen_candles(n, base_price, vol, seed, drift=0.0):
    rng = random.Random(seed)
    out = []
    now = int(time.time())
    start = now - (now % 60) - (n * 60)
    price = base_price
    for i in range(n):
        drift_term = drift + (base_price - price) * 0.05
        shock = rng.gauss(0, vol)
        o = price
        c = o + drift_term + shock
        wick = rng.uniform(0, vol * 1.5)
        out.append({
            "time": start + i * 60,
            "open": round(o, 5),
            "high": round(max(o, c) + wick, 5),
            "low": round(min(o, c) - wick, 5),
            "close": round(c, 5),
        })
        price = c
    return out


def grade(candle: Dict, signal: str):
    """feed.py _accuracy() equivalent: close>open ⇒ UP; CALL matches UP."""
    o, c = candle["open"], candle["close"]
    if abs(c - o) < 1e-9:
        if candle.get("high") == candle.get("low") == o == c:
            return "skip"
        return "draw"
    actual_up = c > o
    pred_up = signal == "CALL"
    return "correct" if actual_up == pred_up else "wrong"


def run_pair(pair: str, n_candles: int):
    if "JPY" in pair:
        base_price, vol = 110.00, 0.05
    else:
        base_price, vol = 1.1000, 0.0010

    scenarios = [
        ("trend_up", 0.00035), ("trend_down", -0.00035), ("random_walk", 0.0),
    ]
    history = []   # every signal with ctime — the "signal history" requirement
    errors = 0

    for si, (label, drift) in enumerate(scenarios):
        # FIX (DETERMINISTIC-SEED-2026-09-07): `hash((pair, label))` uses
        # Python's per-process randomized string hash (PYTHONHASHSEED), so
        # two runs produced DIFFERENT series and unreproducible results.
        # zlib.crc32 is stable across runs and platforms.
        seed = zlib.crc32(f"{pair}|{label}".encode()) % 100000
        candles = gen_candles(n_candles, base_price, vol, seed=seed, drift=drift)
        for i in range(WARMUP, len(candles)):
            # FIX (LOOKAHEAD-2026-09-07, CRITICAL): was candles[...:i+1] —
            # the engine saw candle i's CLOSE (the answer) and was then
            # graded on candle i's own open→close move. Live never does
            # this: the prediction for candle N is made from CLOSED candles
            # only (feed.py _close_running_and_start_new → _run_eoc). Use
            # a strictly-preceding window: candles[:i].
            window = candles[max(0, i - WINDOW):i]
            try:
                pred = predict(candles=window, ticks=None, micro=None,
                               asset=pair, htf_trend="SIDEWAYS", period=60)
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  ⚠ predict error {pair}@{i}: {type(e).__name__}: {e}")
                continue
            sig = pred.get("signal")
            if sig not in ("CALL", "PUT"):
                continue   # coverage violation — counted below
            acc = grade(candles[i], sig)
            history.append({
                "asset": pair,
                "ctime": candles[i]["time"],
                "scenario": label,
                "signal": sig,
                "accuracy": acc,
                "confidence": pred.get("confidence", 0),
                "strategy": pred.get("strategy", ""),
                "quality": pred.get("signal_quality", ""),
            })
    return history, errors


def summarize(history: List[Dict], label: str = "") -> Dict:
    graded = [h for h in history if h["accuracy"] in ("correct", "wrong")]
    correct = sum(1 for h in graded if h["accuracy"] == "correct")
    draws = sum(1 for h in history if h["accuracy"] == "draw")
    wr = (100.0 * correct / len(graded)) if graded else None

    def bucket(rows):
        g = [h for h in rows if h["accuracy"] in ("correct", "wrong")]
        c = sum(1 for h in g if h["accuracy"] == "correct")
        return {
            "total": len(g) + sum(1 for h in rows if h["accuracy"] == "draw"),
            "graded": len(g),
            "correct": c,
            "win_pct": (round(100.0 * c / len(g), 1) if g else None),
        }

    out = {
        "signals": len(history),
        "graded": len(graded),
        "correct": correct,
        "draws": draws,
        "win_pct": (round(wr, 1) if wr is not None else None),
        "call": bucket([h for h in history if h["signal"] == "CALL"]),
        "put": bucket([h for h in history if h["signal"] == "PUT"]),
        "strict": bucket([h for h in history if h["quality"] not in ("FALLBACK",)]),
        "fallback": bucket([h for h in history if h["quality"] == "FALLBACK"]),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs",
                    default="USDZAR_otc,NZDUSD_otc,USDMXN_otc,EURUSD,EURGBP,USDJPY")
    ap.add_argument("--candles", type=int, default=300,
                    help="candles per scenario (3 scenarios per pair)")
    ap.add_argument("--out", default=os.path.join(REPO, "scripts",
                                                  "backtest_every_candle_results.json"))
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    print("=" * 76)
    print("BACKTEST — EVERY-CANDLE MODE (walk-forward, no look-ahead)")
    print("⚠ synthetic data — absolute WR inflated by mean-reversion structure;")
    print("  this run verifies MECHANICS (coverage / per-direction split / grading)")
    print("=" * 76)
    print(f"Pairs: {pairs}   Candles/scenario: {args.candles}   "
          f"Scenarios: trend_up/trend_down/random_walk")
    print(f"Mode: {cf_mod.SIGNAL_MODE}  conf floor: {cf_mod.MIN_CONFIDENCE}  "
          f"fallback band: {cf_mod.FALLBACK_CONF_BASE}-{cf_mod.FALLBACK_CONF_CAP}\n")

    report = {"mode": cf_mod.SIGNAL_MODE, "candles_per_scenario": args.candles,
              "pairs": {}}
    total_expected = 0
    total_signal_rows = 0
    all_histories = []

    for pair in pairs:
        history, errors = run_pair(pair, args.candles)
        s = summarize(history)
        n_candles_total = 3 * max(0, args.candles - WARMUP)
        coverage = 100.0 * len(history) / max(1, n_candles_total)
        report["pairs"][pair] = {"summary": s, "errors": errors,
                                 "coverage_pct": round(coverage, 1)}
        all_histories.extend(history)
        total_expected += n_candles_total
        total_signal_rows += len(history)

        print(f"{pair}")
        print(f"  Coverage : {len(history)}/{n_candles_total} candles "
              f"({coverage:.1f}%)   errors: {errors}")
        print(f"  Overall  : {s['win_pct']}%   "
              f"({s['correct']}W/{s['graded'] - s['correct']}L/"
              f"{s['draws']}D of {s['graded']} graded)")
        print(f"  CALL     : {s['call']['win_pct']}%  "
              f"({s['call']['correct']}/{s['call']['graded']})")
        print(f"  PUT      : {s['put']['win_pct']}%  "
              f"({s['put']['correct']}/{s['put']['graded']})")
        print(f"  Strict   : n={s['strict']['graded']}  "
              f"wr={s['strict']['win_pct']}%")
        print(f"  Fallback : n={s['fallback']['graded']}  "
              f"wr={s['fallback']['win_pct']}%")
        print()

    overall = summarize(all_histories)
    coverage_all = 100.0 * total_signal_rows / max(1, total_expected)
    report["overall"] = overall
    report["coverage_pct"] = round(coverage_all, 1)

    print("-" * 76)
    print(f"ALL PAIRS  coverage: {coverage_all:.1f}%   "
          f"overall WR: {overall['win_pct']}%")
    print(f"  CALL: {overall['call']['win_pct']}%  "
          f"PUT: {overall['put']['win_pct']}%")
    print(f"  strict n={overall['strict']['graded']} "
          f"(wr {overall['strict']['win_pct']}%)   "
          f"fallback n={overall['fallback']['graded']} "
          f"(wr {overall['fallback']['win_pct']}%)")

    # ── Verdict ───────────────────────────────────────────────────────────
    # FIX (HONEST-CHECKS-2026-09-07): "history_rows_match_signals" was
    # hardcoded True and validated nothing. Now: every row must carry a
    # ctime + signal + accuracy, and (asset, scenario, ctime) must be unique
    # (mirrors the signal_log UPSERT contract).
    seen_keys = set()
    rows_complete = True
    for h in all_histories:
        if h.get("ctime") is None or not h.get("signal") or not h.get("accuracy"):
            rows_complete = False
            break
        key = (h["asset"], h.get("scenario"), h["ctime"])
        if key in seen_keys:
            rows_complete = False
            break
        seen_keys.add(key)
    checks = {
        "coverage_100": coverage_all >= 99.9,
        "zero_errors": all(v["errors"] == 0 for v in report["pairs"].values()),
        "call_put_separated": (overall["call"]["graded"] > 0
                               and overall["put"]["graded"] > 0),
        "history_rows_match_signals": (
            rows_complete and len(seen_keys) == total_signal_rows),
    }
    report["checks"] = checks
    ok = all(checks.values())
    print()
    for k, v in checks.items():
        print(f"  {'✓' if v else '✗'} {k}")
    print("=" * 76)
    print(f"  {'✅ BACKTEST PASS' if ok else '❌ BACKTEST FAIL'}")
    print("=" * 76)

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Results saved → {args.out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
