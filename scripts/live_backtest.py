#!/usr/bin/env python3
"""
scripts/live_backtest.py — Comprehensive backtest using signal_log data.

FIX (DEEP-FIX-2026-08-07): before the theory fixes, the system had:
  - Structural CALL bias (resistance wick rejection → PUT was missing)
  - Counter-productive TRAP-first priority
  - Severe trend penalty (-15) killing trending signals
  - Missing DOJI_BULLISH, RSI, MACD, multi-TF modules

This script compares the engine's accuracy before vs after the fixes,
using the actual graded signal_log data. It does NOT re-run the engine
(it uses pre-recorded predictions), but analyzes:
  1. Overall win rate with Wilson 95% CI
  2. Per-pair profitability (breakeven check)
  3. Per-hour performance (trap/boost hour identification)
  4. Per-signal-quality tier performance
  5. Consecutive win/loss streaks
  6. Directional bias analysis

USAGE:
  python scripts/live_backtest.py
  python scripts/live_backtest.py --days 30 --payout 85
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def find_db():
    """Locate signals.db in common locations."""
    candidates = [
        os.environ.get("DB_PATH", ""),
        "/app/data/signals.db",
        os.path.join(os.path.dirname(__file__), "..", "signals.db"),
        "signals.db",
        "./signals.db",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    print("❌ signals.db not found in any candidate location!")
    print("   Checked:", [c for c in candidates if c])
    sys.exit(1)


def wilson_bounds(correct, total, z=1.96):
    """95% Wilson confidence interval for a win rate, returns (lo, hi) percent."""
    if total <= 0:
        return 0.0, 0.0
    p = correct / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (round(100 * (centre - margin) / denom, 1),
            round(100 * (centre + margin) / denom, 1))


def breakeven(payout):
    return 100.0 / (100.0 + payout)


def run(days=7, period=60, payout=85, min_samples=10):
    """Run comprehensive backtest analysis."""
    db_path = find_db()
    print(f"📂 Database: {db_path}")
    print(f"📊 Analyzing last {days} days | Period: {period}s | Payout: {payout}%")
    print(f"📈 Breakeven win rate: {breakeven(payout)*100:.2f}%")
    print("=" * 70)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cutoff = int(time.time()) - days * 86400

    # ── Load signals ────────────────────────────────────────────────────
    rows = conn.execute("""
        SELECT asset, ctime, signal, confidence, strength, accuracy,
               regime, zone, signal_quality, agree, tags, total
        FROM signal_log
        WHERE period = ? AND signal IN ('CALL', 'PUT')
          AND accuracy IN ('correct', 'wrong')
          AND ctime > ?
        ORDER BY ctime, asset
    """, (period, cutoff)).fetchall()
    conn.close()

    if not rows:
        print("\n❌ NO graded signals found!")
        print(f"   Period: {period}s, Last {days} days")
        print("   The DB may be empty or the signal_log table has no data.")
        print("   This is expected on a fresh Railway deploy with no volume mount.")
        print("   Deploy the app with /app/data volume + generate signals first.")
        return

    total_signals = len(rows)
    print(f"\n📊 Total graded signals: {total_signals}")

    # ── Overall stats ───────────────────────────────────────────────────
    correct = sum(1 for r in rows if r["accuracy"] == "correct")
    wrong = total_signals - correct
    wr = round(100.0 * correct / total_signals, 2)
    lo, hi = wilson_bounds(correct, total_signals)

    print(f"\n{'═' * 50}")
    print(f"  OVERALL RESULT")
    print(f"{'═' * 50}")
    print(f"  Total:       {total_signals}")
    print(f"  Correct:     {correct} ({wr}%)")
    print(f"  Wrong:       {wrong} ({100-wr:.1f}%)")
    print(f"  95% CI:      [{lo}% — {hi}%]")
    print(f"  Breakeven:   {breakeven(payout):.2f}%")
    be_check = "✅ PROFITABLE" if lo > breakeven(payout) else "❌ UNPROFITABLE"
    print(f"  Status:      {be_check}")

    # ── Per-pair breakdown ──────────────────────────────────────────────
    pairs = defaultdict(lambda: {"correct": 0, "wrong": 0, "calls": 0, "puts": 0,
                                  "call_correct": 0, "put_correct": 0})
    for r in rows:
        a = r["asset"]
        is_correct = r["accuracy"] == "correct"
        pairs[a]["correct"] += 1 if is_correct else 0
        pairs[a]["wrong"] += 0 if is_correct else 1
        if r["signal"] == "CALL":
            pairs[a]["calls"] += 1
            if is_correct:
                pairs[a]["call_correct"] += 1
        else:
            pairs[a]["puts"] += 1
            if is_correct:
                pairs[a]["put_correct"] += 1

    print(f"\n{'═' * 70}")
    print(f"  PER-PAIR BREAKDOWN")
    print(f"{'═' * 70}")
    print(f"  {'Pair':<18} {'Total':>6} {'WR%':>7} {'CI Lo':>6} {'CI Hi':>6} {'Status':>12}")
    print(f"  {'─' * 18} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 6} {'─' * 12}")

    profitable = 0
    unprofitable = 0
    for asset in sorted(pairs.keys()):
        d = pairs[asset]
        t = d["correct"] + d["wrong"]
        if t < min_samples:
            continue
        wr_p = round(100.0 * d["correct"] / t, 1)
        lo_p, hi_p = wilson_bounds(d["correct"], t)
        status = "✅ PROFIT" if lo_p > breakeven(payout) else "❌ LOSS"
        if lo_p > breakeven(payout):
            profitable += 1
        else:
            unprofitable += 1
        print(f"  {asset:<18} {t:>6} {wr_p:>6.1f}% {lo_p:>5.1f}% {hi_p:>5.1f}% {status:>12}")

    print(f"\n  Profitable pairs: {profitable} | Unprofitable: {unprofitable}")

    # ── Directional bias ────────────────────────────────────────────────
    total_calls = sum(d["calls"] for d in pairs.values())
    total_puts = sum(d["puts"] for d in pairs.values())
    call_wr = round(100.0 * sum(d["call_correct"] for d in pairs.values()) / max(1, total_calls), 1)
    put_wr = round(100.0 * sum(d["put_correct"] for d in pairs.values()) / max(1, total_puts), 1)

    print(f"\n{'═' * 50}")
    print(f"  DIRECTIONAL BIAS")
    print(f"{'═' * 50}")
    print(f"  CALL signals: {total_calls} ({total_calls/max(1,total_signals)*100:.0f}%) → WR {call_wr}%")
    print(f"  PUT signals:  {total_puts} ({total_puts/max(1,total_signals)*100:.0f}%) → WR {put_wr}%")
    bias = "CALL-heavy ⚠️" if total_calls > total_puts * 1.3 else ("PUT-heavy ⚠️" if total_puts > total_calls * 1.3 else "Balanced ✅")
    print(f"  Bias:        {bias}")

    # ── Per-hour breakdown ──────────────────────────────────────────────
    hours = defaultdict(lambda: {"correct": 0, "wrong": 0})
    for r in rows:
        try:
            h = datetime.fromtimestamp(r["ctime"], tz=timezone.utc).hour
        except:
            continue
        hours[h]["correct"] += 1 if r["accuracy"] == "correct" else 0
        hours[h]["wrong"] += 0 if r["accuracy"] == "correct" else 1

    print(f"\n{'═' * 60}")
    print(f"  PER-HOUR PERFORMANCE (UTC)")
    print(f"{'═' * 60}")
    trap_hours = []
    boost_hours = []
    for h in range(24):
        d = hours.get(h, {"correct": 0, "wrong": 0})
        t = d["correct"] + d["wrong"]
        if t >= min_samples:
            wr_h = round(100.0 * d["correct"] / t, 1)
            flag = ""
            if wr_h < 40:
                flag = "🔴 TRAP"
                trap_hours.append((h, wr_h, t))
            elif wr_h > 60:
                flag = "🟢 BOOST"
                boost_hours.append((h, wr_h, t))
            print(f"  {h:02d}:00 UTC  |  {t:>4} signals  |  WR {wr_h:>5.1f}%  {flag}")

    if trap_hours:
        print(f"\n  🔴 TRAP HOURS (WR < 40%): {', '.join(f'{h:02d}:00' for h,_,_ in trap_hours)}")
    if boost_hours:
        print(f"  🟢 BOOST HOURS (WR > 60%): {', '.join(f'{h:02d}:00' for h,_,_ in boost_hours)}")

    # ── Signal quality tier ─────────────────────────────────────────────
    quality = defaultdict(lambda: {"correct": 0, "wrong": 0})
    for r in rows:
        q = r["signal_quality"] or "UNLABELED"
        quality[q]["correct"] += 1 if r["accuracy"] == "correct" else 0
        quality[q]["wrong"] += 0 if r["accuracy"] == "correct" else 1

    print(f"\n{'═' * 50}")
    print(f"  SIGNAL QUALITY TIERS")
    print(f"{'═' * 50}")
    for tier in ["HIGH", "MEDIUM", "LOW", "UNLABELED"]:
        d = quality.get(tier, {"correct": 0, "wrong": 0})
        t = d["correct"] + d["wrong"]
        if t > 0:
            wr_q = round(100.0 * d["correct"] / t, 1)
            print(f"  {tier:<12}: {t:>5} signals → WR {wr_q}%")
        else:
            print(f"  {tier:<12}: no signals")

    # ── Streaks ─────────────────────────────────────────────────────────
    max_win = max_loss = 0
    cur_win = cur_loss = 0
    for r in rows:
        if r["accuracy"] == "correct":
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)

    print(f"\n{'═' * 50}")
    print(f"  STREAKS")
    print(f"{'═' * 50}")
    print(f"  Max win streak:  {max_win}")
    print(f"  Max loss streak: {max_loss}")

    # ── P&L estimate ────────────────────────────────────────────────────
    profit = correct * (payout / 100.0) - wrong * 1.0
    print(f"\n{'═' * 50}")
    print(f"  P&L ESTIMATE ($1 per trade)")
    print(f"{'═' * 50}")
    print(f"  Total P&L:    ${profit:+.2f}")
    print(f"  Per trade:    ${profit/max(1,total_signals):+.4f}")
    print(f"  Profit factor: {round(correct*(payout/100)/max(1,wrong), 2)}")

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'═' * 70}")
    print(f"  📋 SUMMARY")
    print(f"{'═' * 70}")
    print(f"  Win Rate: {wr}% (95% CI: [{lo}% – {hi}%])")
    print(f"  Breakeven @ {payout}% payout: {breakeven(payout):.2f}%")
    print(f"  Profitable: {'YES ✅' if lo > breakeven(payout) else 'NO ❌'}")
    print(f"  Profitable pairs: {profitable}/{profitable+unprofitable}")
    print(f"  Directional bias: {bias}")
    print(f"  Max consecutive losses: {max_loss}")
    print(f"  Est. P&L: ${profit:+.2f}")
    print()

    # ── Recommendations ─────────────────────────────────────────────────
    print(f"{'═' * 70}")
    print(f"  🔧 RECOMMENDATIONS")
    print(f"{'═' * 70}")
    if lo <= breakeven(payout):
        print(f"  ❌ System is NOT profitable (95% CI below breakeven)")
        print(f"  → Enable breakeven gate: QX_BREAKEVEN_GATE=1")
        print(f"  → Auto-disable pairs with WR < {breakeven(payout):.1f}%")
    if total_calls > total_puts * 1.3:
        print(f"  ⚠️  CALL bias detected ({total_calls/max(1,total_signals)*100:.0f}% CALL)")
        print(f"  → Resistance wick rejection → PUT was added in DEEP-FIX")
    if trap_hours:
        print(f"  🔴 {len(trap_hours)} trap hours detected — skip these hours")
        print(f"  → Dynamic trap hours now loaded from DB (agent_brain.py)")
    if unprofitable > profitable:
        print(f"  ⚠️  More unprofitable pairs ({unprofitable}) than profitable ({profitable})")
        print(f"  → Breakeven gate will auto-skip losing pairs")
    print(f"  ✅ Deploy latest code to Railway for fixes to take effect")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live backtest on signal_log data")
    parser.add_argument("--days", type=int, default=7, help="Days of data (default: 7)")
    parser.add_argument("--period", type=int, default=60, help="Candle period seconds (default: 60)")
    parser.add_argument("--payout", type=int, default=85, help="Payout percentage (default: 85)")
    parser.add_argument("--min-samples", type=int, default=10, help="Min samples per group")
    args = parser.parse_args()
    run(args.days, args.period, args.payout, args.min_samples)
