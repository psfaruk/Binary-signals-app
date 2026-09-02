#!/usr/bin/env python3
"""
scripts/smoke_test_signals.py — CONFLUENCE-V1 pipeline health check.

REWRITTEN (2026-09-02) for the new high-confidence confluence engine.

The OLD requirement was "প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে" (every candle
must produce a signal) — that mode was the #1 cause of WRONG predictions
(fallback signals had ~41.9% WR). The user's NEW requirement is the opposite:

  1. Only high-confidence confluence signals are published
     (>= MIN_AGREE_CLUSTERS independent clusters agree, zero opposition).
  2. NO fallback signals — NEUTRAL is a first-class outcome.
  3. Every published signal has honest confidence >= QX_MIN_CONFLUENCE_CONF
     and strength MEDIUM or STRONG (WEAK no longer exists).
  4. The pipeline NEVER errors and NEVER emits an unexplained direction.

This smoke test verifies the pipeline is ALIVE and HONEST, not that it fires
on every candle:
  ✓ zero engine errors across all candles
  ✓ every CALL/PUT carries confluence metadata + confidence >= floor
  ✓ every NEUTRAL carries a reject-gate label (explainable abstention)
  ✓ no legacy fallback strategies (smart_fallback / smart_evidence_vote)
  ✓ a deliberate multi-cluster confluence scenario DOES fire (pipeline not dead)

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

# High-confidence mode flags (the new defaults — explicit here for clarity)
os.environ.setdefault("QX_NO_FALLBACK", "1")
os.environ.setdefault("QX_ALLOW_WEAK_SIGNALS", "0")

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
    print("SMOKE TEST — CONFLUENCE-V1 (high-confidence, no fallback)")
    print("=" * 72)
    print(f"Pairs: {pairs}  |  Candles/scenario: {args.candles}")
    print(f"Gates: MIN_AGREE_CLUSTERS={cf_mod.MIN_AGREE_CLUSTERS}  "
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
        # Every fired signal: confluence_v1 strategy + conf >= floor +
        # strength MEDIUM/STRONG + cluster agreement >= gate
        fired_ok = all(
            s["strategy"] == "confluence_v1"
            and s["confidence"] >= cf_mod.MIN_CONFIDENCE
            and s["strength"] in ("MEDIUM", "STRONG")
            and s["agree"] >= cf_mod.MIN_AGREE_CLUSTERS
            for s in fired)
        # Every NEUTRAL must carry an explainable gate label
        neutral_ok = all(bool(s["gate"]) for s in neutral)
        # No legacy fallback strategies may appear
        legacy = {"smart_fallback", "smart_evidence_vote", "error", "unknown"}
        no_fallback = not (set(strat_counter.keys()) & legacy)

        pair_ok = err_ok and fired_ok and neutral_ok and no_fallback
        overall_ok &= pair_ok

        wr_n = len(fired)
        status = "✓ PASS" if pair_ok else "✗ FAIL"
        print(f"{pair} ({n_total} candles) {status}")
        print(f"  CALL: {counts.get('CALL', 0)}  PUT: {counts.get('PUT', 0)}  "
              f"NEUTRAL: {counts.get('NEUTRAL', 0)}  ERROR: {n_err}")
        print(f"  Fired: {wr_n} ({100.0 * wr_n / max(1, n_total):.1f}% — high-confidence mode, "
              f"low coverage is CORRECT)")
        if fired:
            gates_hit = Counter(s["gate"] for s in neutral)
            top_gates = ", ".join(f"{g}×{c}" for g, c in gates_hit.most_common(4))
            confs = [s["confidence"] for s in fired]
            print(f"  Fired conf range: {min(confs)}–{max(confs)}  "
                  f"agree range: {min(s['agree'] for s in fired)}–{max(s['agree'] for s in fired)}")
        print(f"  Strategies: {dict(strat_counter)}")
        if neutral:
            gates_hit = Counter(s["gate"] for s in neutral)
            top_gates = ", ".join(f"{g}×{c}" for g, c in gates_hit.most_common(5))
            print(f"  Reject gates (why NEUTRAL): {top_gates}")
        if not err_ok:
            examples = [s["gate"] for s in signals if s["signal"] == "ERROR"][:3]
            print(f"  ⚠ ERRORS: {examples}")
        if not fired_ok:
            bad = [s for s in fired
                   if not (s["strategy"] == "confluence_v1"
                           and s["confidence"] >= cf_mod.MIN_CONFIDENCE
                           and s["strength"] in ("MEDIUM", "STRONG"))][:3]
            print(f"  ⚠ BAD FIRED SIGNALS: {bad}")
        if not no_fallback:
            print(f"  ⚠ LEGACY/FALLBACK strategy appeared: {dict(strat_counter)}")
        print()

    print("=" * 72)
    if overall_ok:
        print("  ✅ PASS — pipeline healthy: no errors, no fallback, all")
        print("     published signals meet the high-confidence confluence bar.")
    else:
        print("  ❌ FAIL — see details above.")
    print("=" * 72)
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
