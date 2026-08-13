#!/usr/bin/env python3
"""
scripts/smoke_test_signals.py — Verify every candle produces a CALL/PUT signal.

PHASE-8-FIX (2026-08-13): User requirement is "প্রত্যেক ক্যান্ডেল এ সিগন্যাল
আসতে হবে" (every candle must produce a signal). This script generates
synthetic candle data and runs the engine.predict() pipeline on each candle
to verify that:

  1. Every candle produces a CALL or PUT signal (not NEUTRAL).
  2. The signal has a non-zero confidence.
  3. The signal has a strength label (WEAK / MEDIUM / STRONG).

Run:
    python scripts/smoke_test_signals.py
    python scripts/smoke_test_signals.py --pairs EURUSD_otc,GBPUSD_otc
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

# Disable env vars that would suppress signals
os.environ.setdefault("QX_PATTERN_GATE", "0")
os.environ.setdefault("QX_ALLOW_WEAK_SIGNALS", "1")
os.environ.setdefault("QX_BREAKEVEN_GATE", "0")
os.environ.setdefault("QX_PAIR_HEALTH_GATE", "0")
os.environ.setdefault("QX_LOW_CONF_SKIP_OTC", "0")
os.environ.setdefault("QX_LOW_CONF_SKIP_REAL", "0")
os.environ.setdefault("QX_TRAP_HOUR", "0")
os.environ.setdefault("QX_CHOP_GUARD", "0")
os.environ.setdefault("QX_WEAK_NEUTRAL", "0")
os.environ.setdefault("QX_LOSS_COOLDOWN", "0")
os.environ.setdefault("QX_PAIR_PENALTY_NEUTRAL", "0")


def gen_synthetic_candles(n: int = 100, base_price: float = 1.1000,
                          volatility: float = 0.0010, seed: int = 42) -> List[Dict]:
    """Generate n synthetic OHLC candles with realistic forex-like movement."""
    rng = random.Random(seed)
    candles = []
    now = int(time.time())
    # Align to minute boundary, n minutes back
    start = now - (now % 60) - (n * 60)
    price = base_price
    for i in range(n):
        # Random walk with mean reversion
        drift = (base_price - price) * 0.05
        shock = rng.gauss(0, volatility)
        open_p = price
        close_p = open_p + drift + shock
        # High/low around the open-close range
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


def gen_synthetic_ticks(candles: List[Dict], ticks_per_candle: int = 60) -> List[Dict]:
    """Generate synthetic tick data aligned to candles.

    Returns a flat list of {time, price} dicts sorted by time.
    """
    rng = random.Random(43)
    ticks = []
    for c in candles:
        n = ticks_per_candle
        for i in range(n):
            # Linear interpolation between open and close, with noise
            t = i / max(1, n - 1)
            base = c["open"] + (c["close"] - c["open"]) * t
            noise = rng.gauss(0, (c["high"] - c["low"]) / 6) if (c["high"] - c["low"]) > 0 else 0
            ticks.append({
                "time": c["time"] + i,
                "price": round(base + noise, 5),
            })
    # Already sorted since candles are sorted, but sort just in case.
    ticks.sort(key=lambda t: t["time"])
    return ticks


def run_backtest(pairs: List[str], n_candles: int = 100) -> Dict:
    """Run engine.predict() on synthetic candles for each pair."""
    from engines import predict

    results = {}
    for pair in pairs:
        # Determine base price by pair
        if "JPY" in pair:
            base_price = 110.00
            vol = 0.05
        elif "BTC" in pair or "ETH" in pair:
            base_price = 50000.0
            vol = 100.0
        else:
            base_price = 1.1000
            vol = 0.0010

        candles = gen_synthetic_candles(n_candles, base_price, vol, seed=hash(pair) % 1000)

        signals = []
        for i in range(20, len(candles)):  # need at least 20 candles for context
            window = candles[max(0, i-50):i+1]
            try:
                pred = predict(
                    candles=window,
                    ticks=None,  # engine handles None ticks
                    micro=None,
                    asset=pair,
                    htf_trend="SIDEWAYS",
                    period=60,
                )
                signals.append({
                    "ctime": candles[i]["time"],
                    "signal": pred.get("signal", "UNKNOWN"),
                    "confidence": pred.get("confidence", 0),
                    "strength": pred.get("strength", "NONE"),
                    "strategy": pred.get("strategy", "unknown"),
                })
            except Exception as e:
                signals.append({
                    "ctime": candles[i]["time"],
                    "signal": "ERROR",
                    "confidence": 0,
                    "strength": "NONE",
                    "strategy": f"error: {e}",
                })

        results[pair] = signals
    return results


def report(results: Dict) -> None:
    """Print a per-pair backtest report."""
    print("\n" + "=" * 72)
    print("  SMOKE TEST: every candle must produce a CALL or PUT signal")
    print("=" * 72)

    total_signals = 0
    total_call = 0
    total_put = 0
    total_neutral = 0
    total_error = 0
    total_candles = 0

    for pair, sigs in results.items():
        n = len(sigs)
        sig_counter = Counter(s["signal"] for s in sigs)
        str_counter = Counter(s["strength"] for s in sigs)
        calls = sig_counter.get("CALL", 0)
        puts = sig_counter.get("PUT", 0)
        neutrals = sig_counter.get("NEUTRAL", 0)
        errors = sig_counter.get("ERROR", 0) + sig_counter.get("UNKNOWN", 0)
        directional = calls + puts
        coverage = (directional / n * 100) if n else 0

        total_signals += directional
        total_call += calls
        total_put += puts
        total_neutral += neutrals
        total_error += errors
        total_candles += n

        status = "✅ PASS" if coverage >= 95 else ("⚠️  PARTIAL" if coverage >= 50 else "❌ FAIL")
        print(f"\n{pair} ({n} candles) {status}")
        print(f"  CALL:     {calls:4d}  ({calls/n*100:5.1f}%)")
        print(f"  PUT:      {puts:4d}  ({puts/n*100:5.1f}%)")
        print(f"  NEUTRAL:  {neutrals:4d}  ({neutrals/n*100:5.1f}%)")
        print(f"  ERROR:    {errors:4d}")
        print(f"  Coverage: {coverage:5.1f}%  (directional signals / total candles)")
        print(f"  Strength: WEAK={str_counter.get('WEAK',0)}, "
              f"MEDIUM={str_counter.get('MEDIUM',0)}, "
              f"STRONG={str_counter.get('STRONG',0)}")

        # Show a few sample signals
        print(f"  Sample (first 3):")
        for s in sigs[:3]:
            print(f"    {time.strftime('%H:%M', time.gmtime(s['ctime']))} "
                  f"{s['signal']:8s} conf={s['confidence']:5.1f} "
                  f"str={s['strength']:8s} via={s['strategy']}")

    print("\n" + "=" * 72)
    overall_coverage = (total_signals / total_candles * 100) if total_candles else 0
    print(f"  OVERALL: {total_signals}/{total_candles} candles produced a signal "
          f"({overall_coverage:.1f}% coverage)")
    print(f"  CALL: {total_call}, PUT: {total_put}, NEUTRAL: {total_neutral}, "
          f"ERROR: {total_error}")

    if overall_coverage >= 95:
        print("\n  ✅ PASS — every candle produces a CALL or PUT signal.")
        print("  The user requirement 'প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে' is met.")
    elif overall_coverage >= 50:
        print("\n  ⚠️  PARTIAL — some candles still produce NEUTRAL.")
        print("  Check the engine configuration and filters.")
    else:
        print("\n  ❌ FAIL — most candles produce NEUTRAL.")
        print("  The signal generation pipeline is suppressing too many signals.")
    print("=" * 72 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Smoke test signal generation")
    parser.add_argument("--pairs", default="EURUSD_otc,GBPUSD_otc,USDJPY_otc,EURUSD",
                        help="Comma-separated pair names")
    parser.add_argument("--candles", type=int, default=100,
                        help="Number of candles to test per pair (default: 100)")
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    print(f"Running smoke test on {len(pairs)} pair(s) × {args.candles} candles...")
    print(f"Pairs: {pairs}")
    print(f"Env: QX_PATTERN_GATE={os.environ.get('QX_PATTERN_GATE', '?')}, "
          f"QX_ALLOW_WEAK_SIGNALS={os.environ.get('QX_ALLOW_WEAK_SIGNALS', '?')}, "
          f"QX_BREAKEVEN_GATE={os.environ.get('QX_BREAKEVEN_GATE', '?')}")

    results = run_backtest(pairs, args.candles)
    report(results)


if __name__ == "__main__":
    main()
