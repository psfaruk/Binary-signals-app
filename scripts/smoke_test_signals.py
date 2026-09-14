#!/usr/bin/env python3
"""
scripts/smoke_test_signals.py — pipeline health check.

UPDATED (2026-09-14) for the ANY-THEORY signal mode (QX_SIGNAL_MODE default
"any_theory"). The user's standing requirements:

  "প্রত্যেক ক্যান্ডেল এ সিগন্যাল প্রধান করতে হবে, কিন্তু fallback signals
   দেওয়া যাবে না। ... যে কোনো একটি পাস হলেই সিগন্যাল দিবে। মডিউল ইঞ্জিন
   থেকে সিগন্যাল আসলো না — ML model থেকে সিগন্যাল টি আসবে।"

What this test verifies (strategy-engine-only view — the ML hand-off for
zero-theory candles is verified by scripts/backtest_any_theory.py):
  ✓ zero engine errors across all candles
  ✓ ~full candle coverage — every candle where ANY theory voted yields
    CALL/PUT; the rare zero-theory candle returns NEUTRAL (the live feed
    then takes the ML model's frozen T+1 prediction — ml_model_t1)
  ✓ strict signals (strategy "confluence_v1") carry confidence >= floor
  ✓ ANY-THEORY signals (strategy "confluence_v1_any") are REAL theory
    signals: confidence in [ANY_CONF_BASE, ANY_CONF_CAP], quality
    MEDIUM/LOW, source "strategy"
  ✓ ZERO fallback signals (strategy "confluence_v1_fallback" / quality
    FALLBACK / fallback=True are BANNED)
  ✓ no legacy fallback strategies (smart_fallback / smart_evidence_vote)
  ✓ direction is deterministic (same window → same signal)

Run:
    python scripts/smoke_test_signals.py
    python scripts/smoke_test_signals.py --pairs USDZAR_otc,EURUSD
    python scripts/smoke_test_signals.py --candles 200
"""
import argparse
import os
import random
import sys
import time
from collections import Counter
from typing import List, Dict

# Ensure project root is on sys.path
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# ANY-THEORY mode is the production default — explicit here for clarity.
os.environ.setdefault("QX_SIGNAL_MODE", "any_theory")

from engines.base import confluence as cf_mod


def gen_synthetic_candles(n: int = 100, base_price: float = 1.1000,
                          volatility: float = 0.0010, seed: int = 42,
                          drift: float = 0.0) -> List[Dict]:
    """Generate n synthetic OHLC candles with realistic forex-like movement."""
    rng = random.Random(seed)
    candles = []
    now = int(time.time())
    start = now - (now % 60) - (n * 60)
    price = base_price
    for i in range(n):
        drift_term = drift + (base_price - price) * 0.05
        shock = rng.gauss(0, volatility)
        open_p = price
        close_p = open_p + drift_term + shock
        body = abs(close_p - open_p)
        wick = rng.uniform(0, volatility * 1.5)
        high = max(open_p, close_p) + wick
        low = min(open_p, close_p) - wick
        candles.append({
            "time": start + i * 60,
            "open": round(open_p, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close_p, 5),
        })
        price = close_p
    return candles


def run_backtest(pairs: List[str], n_candles: int = 100) -> Dict:
    """Run engine.predict() on synthetic candles for each pair."""
    from engines import predict

    results = {}
    for pair in pairs:
        if "JPY" in pair:
            base_price = 110.00
            vol = 0.05
        else:
            base_price = 1.1000
            vol = 0.0010

        # Three regimes per pair: trend up, trend down, random walk
        candles_tu = gen_synthetic_candles(n_candles, base_price, vol,
                                           seed=hash(pair) % 1000, drift=0.00035)
        candles_td = gen_synthetic_candles(n_candles, base_price, vol,
                                           seed=(hash(pair) + 7) % 1000, drift=-0.00035)
        candles_rw = gen_synthetic_candles(n_candles, base_price, vol,
                                           seed=(hash(pair) + 13) % 1000, drift=0.0)

        signals = []
        for label, candles in (("trend_up", candles_tu),
                               ("trend_down", candles_td),
                               ("random_walk", candles_rw)):
            for i in range(35, len(candles)):
                window = candles[max(0, i - 300):i + 1]
                try:
                    pred = predict(
                        candles=window,
                        ticks=None,
                        micro=None,
                        asset=pair,
                        htf_trend="SIDEWAYS",
                        period=60,
                    )
                    signals.append({
                        "ctime": candles[i]["time"],
                        "scenario": label,
                        "signal": pred.get("signal", "UNKNOWN"),
                        "confidence": pred.get("confidence", 0),
                        "strength": pred.get("strength", "NONE"),
                        "strategy": pred.get("strategy", "unknown"),
                        "quality": pred.get("signal_quality", ""),
                        "source": pred.get("signal_source", ""),
                        "fallback": bool(pred.get("fallback")),
                        "gate": pred.get("confluence_reject_gate", ""),
                        "agree": pred.get("agree", 0),
                    })
                except Exception as e:
                    signals.append({
                        "ctime": candles[i]["time"],
                        "scenario": label,
                        "signal": "ERROR",
                        "confidence": 0,
                        "strength": "NONE",
                        "strategy": "error",
                        "quality": "",
                        "source": "",
                        "fallback": False,
                        "gate": f"{type(e).__name__}: {e}",
                        "agree": 0,
                    })
        results[pair] = signals
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="USDZAR_otc,NZDUSD_otc,EURUSD")
    ap.add_argument("--candles", type=int, default=120)
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    print("=" * 72)
    print("SMOKE TEST — ANY-THEORY MODE (theory-backed signals, ML hand-off)")
    print("=" * 72)
    print(f"Pairs: {pairs}  |  Candles/scenario: {args.candles}")
    print(f"Mode: SIGNAL_MODE={cf_mod.SIGNAL_MODE}  "
          f"MIN_AGREE_CLUSTERS={cf_mod.MIN_AGREE_CLUSTERS}  "
          f"MIN_CONFIDENCE={cf_mod.MIN_CONFIDENCE}\n")

    results = run_backtest(pairs, args.candles)

    overall_ok = True
    for pair, signals in results.items():
        counts = Counter(s["signal"] for s in signals)
        n_total = len(signals)
        n_err = counts.get("ERROR", 0)
        fired = [s for s in signals if s["signal"] in ("CALL", "PUT")]
        neutral = [s for s in signals if s["signal"] == "NEUTRAL"]
        strat_counter = Counter(s["strategy"] for s in signals)

        # ── Checks ────────────────────────────────────────────────────────
        err_ok = (n_err == 0)
        # ANY-THEORY coverage: nearly every candle gets a CALL/PUT from a
        # theory vote. Rare NEUTRALs are EXPECTED (zero theories voted) —
        # the live feed hands those candles to the ML model (verified in
        # backtest_any_theory.py). Strategy-engine-only floor: >= 95%.
        coverage_pct = 100.0 * len(fired) / max(1, n_total)
        coverage_ok = (coverage_pct >= 95.0)
        # Strict signals: confluence_v1 + conf >= floor + MEDIUM/STRONG
        strict_ok = all(
            s["strategy"] == "confluence_v1"
            and s["confidence"] >= cf_mod.MIN_CONFIDENCE
            and s["strength"] in ("MEDIUM", "STRONG")
            for s in fired if s["strategy"] == "confluence_v1")
        # ANY-THEORY signals: real theory signals in the ANY band
        any_ok = all(
            s["strategy"] == "confluence_v1_any"
            and cf_mod.ANY_CONF_BASE <= s["confidence"] <= cf_mod.ANY_CONF_CAP
            and s["quality"] in ("MEDIUM", "LOW")
            and s["source"] == "strategy"
            for s in fired if s["strategy"] == "confluence_v1_any")
        # NO-FALLBACK (USER 2026-09-14): banned labels must NEVER appear
        no_fallback = all(
            s["strategy"] != "confluence_v1_fallback"
            and not s["fallback"]
            and s["quality"] != "FALLBACK"
            for s in signals)
        # No legacy fallback strategies may appear
        legacy = {"smart_fallback", "smart_evidence_vote", "error", "unknown"}
        no_legacy = not (set(strat_counter.keys()) & legacy)

        pair_ok = (err_ok and coverage_ok and strict_ok and any_ok
                   and no_fallback and no_legacy)
        overall_ok &= pair_ok

        status = "✓ PASS" if pair_ok else "✗ FAIL"
        print(f"{pair} ({n_total} candles) {status}")
        print(f"  CALL: {counts.get('CALL', 0)}  PUT: {counts.get('PUT', 0)}  "
              f"NEUTRAL: {counts.get('NEUTRAL', 0)}  ERROR: {n_err}")
        print(f"  Coverage: {len(fired)}/{n_total} "
              f"({coverage_pct:.1f}% — the ML model covers the zero-theory "
              f"candles live; floor 95%)")
        print(f"  Strategies: {dict(strat_counter)}")
        if fired:
            confs = [s["confidence"] for s in fired]
            print(f"  Conf range: {min(confs)}–{max(confs)}")
        if n_err:
            examples = [s["gate"] for s in signals if s["signal"] == "ERROR"][:3]
            print(f"  ⚠ ERRORS: {examples}")
        if not coverage_ok:
            print(f"  ⚠ COVERAGE FAIL — {100 - coverage_pct:.1f}% of candles "
                  f"without a strategy signal (zero-theory rate too high)")
        if not strict_ok:
            bad = [s for s in fired
                   if s["strategy"] == "confluence_v1"
                   and not (s["confidence"] >= cf_mod.MIN_CONFIDENCE
                            and s["strength"] in ("MEDIUM", "STRONG"))][:3]
            print(f"  ⚠ BAD STRICT SIGNALS: {bad}")
        if not any_ok:
            bad = [s for s in fired
                   if s["strategy"] == "confluence_v1_any"
                   and not (cf_mod.ANY_CONF_BASE <= s["confidence"]
                            <= cf_mod.ANY_CONF_CAP
                            and s["quality"] in ("MEDIUM", "LOW"))][:3]
            print(f"  ⚠ BAD ANY-THEORY SIGNALS: {bad}")
        if not no_fallback:
            bad = [s for s in signals
                   if s["strategy"] == "confluence_v1_fallback"
                   or s["fallback"] or s["quality"] == "FALLBACK"][:3]
            print(f"  ⚠ FALLBACK SIGNALS SEEN (BANNED): {bad}")
        if not no_legacy:
            print(f"  ⚠ LEGACY strategy appeared: {dict(strat_counter)}")
        print()

    # ── Determinism check: same window must give the same signal ──────────
    candles = gen_synthetic_candles(120, 1.1000, 0.0010, seed=99, drift=0.0002)
    from engines import predict as _predict
    window = candles[:100]
    p1 = _predict(candles=window, asset="EURUSD", htf_trend="SIDEWAYS", period=60)
    p2 = _predict(candles=window, asset="EURUSD", htf_trend="SIDEWAYS", period=60)
    det_ok = (p1.get("signal") == p2.get("signal")
              and p1.get("confidence") == p2.get("confidence"))
    print(f"Determinism (same window → same signal): {'✓ PASS' if det_ok else '✗ FAIL'}")
    overall_ok &= det_ok

    print("=" * 72)
    if overall_ok:
        print("  ✅ PASS — pipeline healthy: theory-backed signals, zero errors,")
        print("     strict signals >= confidence floor, any-theory signals in")
        print("     the 55-64 band, ZERO fallback signals, ML hand-off covers")
        print("     the rare zero-theory candles (see backtest_any_theory.py).")
    else:
        print("  ❌ FAIL — see details above.")
    print("=" * 72)
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
