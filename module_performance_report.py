#!/usr/bin/env python3
"""
Per-module performance report from signals.db.

Run on the machine where the app actually runs (where signals.db exists):
    python module_performance_report.py

Reads signal_log table, parses the reasons field (which contains
[module_name] prefixed reasons from the 6-module engine), and reports:
  - Per-module win rate
  - Per-module CALL vs PUT breakdown
  - Per-pair module performance
  - Overall accuracy
"""
import sys
import os
import sqlite3
import json
import time
from collections import defaultdict
from pathlib import Path

# Try to find signals.db
APP_DIR = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(APP_DIR / "signals.db"))

if not Path(DB_PATH).exists():
    print(f"❌ signals.db not found at: {DB_PATH}")
    print()
    print("This means the app hasn't run on this machine yet, or DB_PATH")
    print("is set to a different location. To find it:")
    print("  - Check if signals.db exists: ls signals.db")
    print("  - Or set DB_PATH env var: export DB_PATH=/path/to/signals.db")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ─── Check schema ─────────────────────────────────────────────────────────
tables = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(f"DB tables: {tables}")

if "signal_log" not in tables:
    print("❌ signal_log table missing — app hasn't logged any signals yet.")
    sys.exit(0)

total = cur.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
print(f"Total signals logged: {total}")
print()

if total == 0:
    print("No signals in DB yet. Run the app for a while to accumulate data.")
    sys.exit(0)

# ─── Parse module votes from reasons ──────────────────────────────────────
# reasons field is JSON list of strings like:
#   "[candle_reaction] 5+ UP streak → PUT reversal (eff=4)"
#   "[pattern] Bullish Engulfing → CALL (eff=9)"
# We extract module name + direction from each reason.

MODULE_NAMES = {
    "candle_reaction": "Candle Reaction",
    "running_tick": "Running Tick",
    "pattern": "Pattern",
    "indicator": "Indicator",
    "key_level": "Key Level",
    "otc_pattern": "OTC Pattern",
}

# Per-module stats: {module: {CALL: {correct, wrong}, PUT: {correct, wrong}}}
module_stats = defaultdict(lambda: {
    "CALL": {"correct": 0, "wrong": 0, "draw": 0},
    "PUT": {"correct": 0, "wrong": 0, "draw": 0},
})
# Per-pair per-module stats
pair_module_stats = defaultdict(lambda: defaultdict(lambda: {
    "CALL": {"correct": 0, "wrong": 0},
    "PUT": {"correct": 0, "wrong": 0},
}))

rows = cur.execute("""
    SELECT asset, signal, accuracy, reasons, regime
    FROM signal_log
    WHERE signal IN ('CALL', 'PUT')
    ORDER BY ts DESC
""").fetchall()

print(f"Processing {len(rows)} CALL/PUT signals...")
print()

parsed_count = 0
for row in rows:
    asset = row["asset"]
    final_signal = row["signal"]
    accuracy = row["accuracy"]  # 'correct', 'wrong', 'draw', or None
    reasons_raw = row["reasons"] or "[]"

    try:
        reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
    except (json.JSONDecodeError, TypeError):
        reasons = []

    if not isinstance(reasons, list):
        reasons = []

    # Parse each reason for module name + direction
    for reason in reasons:
        reason_str = str(reason)
        # Find module name in brackets: [module_name]
        if not reason_str.startswith("["):
            continue
        end_bracket = reason_str.find("]")
        if end_bracket == -1:
            continue
        module = reason_str[1:end_bracket].strip()

        # Skip non-module prefixes like [BODY collapsed], [_REGIME], etc.
        if module not in MODULE_NAMES:
            continue

        # Determine direction from reason text
        reason_upper = reason_str.upper()
        if "PUT" in reason_upper or "BEAR" in reason_upper or "SELLER" in reason_upper:
            direction = "PUT"
        elif "CALL" in reason_upper or "BULL" in reason_upper or "BUYER" in reason_upper:
            direction = "CALL"
        else:
            continue

        # Was this module's vote correct?
        # A module voting in the SAME direction as final_signal is "correct"
        # if accuracy=correct, "wrong" if accuracy=wrong.
        # A module voting OPPOSITE to final_signal is correct if accuracy=wrong
        # (it was right to disagree).
        if direction == final_signal:
            # Module agreed with final signal
            if accuracy == "correct":
                module_stats[module][direction]["correct"] += 1
                pair_module_stats[asset][module][direction]["correct"] += 1
            elif accuracy == "wrong":
                module_stats[module][direction]["wrong"] += 1
                pair_module_stats[asset][module][direction]["wrong"] += 1
            elif accuracy == "draw":
                module_stats[module][direction]["draw"] += 1
        else:
            # Module disagreed with final signal
            if accuracy == "wrong":
                # Final was wrong, module disagreed → module was RIGHT
                module_stats[module][direction]["correct"] += 1
                pair_module_stats[asset][module][direction]["correct"] += 1
            elif accuracy == "correct":
                # Final was correct, module disagreed → module was WRONG
                module_stats[module][direction]["wrong"] += 1
                pair_module_stats[asset][module][direction]["wrong"] += 1
            # draws don't count for disagreements

    parsed_count += 1

print(f"Parsed {parsed_count} signals.")
print()

# ─── Report: Per-module overall performance ──────────────────────────────
print("=" * 78)
print(" 📊 MODULE PERFORMANCE REPORT (overall)")
print("=" * 78)
print()
print(f"{'Module':<18} {'Total':>7} {'Correct':>9} {'Wrong':>7} {'Win%':>7} {'CALL win%':>10} {'PUT win%':>10}")
print("-" * 78)

module_results = []
for module_key, display_name in MODULE_NAMES.items():
    stats = module_stats.get(module_key)
    if not stats:
        print(f"{display_name:<18} {'—':>7} {'—':>9} {'—':>7} {'—':>7} {'—':>10} {'—':>10}")
        continue

    call_c = stats["CALL"]["correct"]
    call_w = stats["CALL"]["wrong"]
    put_c = stats["PUT"]["correct"]
    put_w = stats["PUT"]["wrong"]
    total_c = call_c + put_c
    total_w = call_w + put_w
    total_all = total_c + total_w

    if total_all == 0:
        print(f"{display_name:<18} {'0':>7} {'—':>9} {'—':>7} {'—':>7} {'—':>10} {'—':>10}")
        continue

    win_pct = (total_c / total_all * 100) if total_all else 0
    call_total = call_c + call_w
    call_win = (call_c / call_total * 100) if call_total else 0
    put_total = put_c + put_w
    put_win = (put_c / put_total * 100) if put_total else 0

    print(f"{display_name:<18} {total_all:>7} {total_c:>9} {total_w:>7} {win_pct:>6.1f}% {call_win:>9.1f}% {put_win:>9.1f}%")
    module_results.append((display_name, total_all, total_c, total_w, win_pct, call_win, put_win))

print()

# ─── Best and worst modules ──────────────────────────────────────────────
if module_results:
    print("=" * 78)
    print(" 🏆 BEST PERFORMING MODULES")
    print("=" * 78)
    sorted_best = sorted(module_results, key=lambda x: -x[4])
    for i, (name, total, c, w, win, call_w, put_w) in enumerate(sorted_best[:3], 1):
        emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉"
        print(f"  {emoji} {name}: {win:.1f}% win rate ({c} correct, {w} wrong, n={total})")
    print()
    print("=" * 78)
    print(" ❌ WORST PERFORMING MODULES")
    print("=" * 78)
    sorted_worst = sorted(module_results, key=lambda x: x[4])
    for name, total, c, w, win, call_w, put_w in sorted_worst[:3]:
        if total < 5:
            print(f"  ⚠️  {name}: {win:.1f}% win rate (n={total} — too few samples)")
        else:
            print(f"  ❌ {name}: {win:.1f}% win rate ({c} correct, {w} wrong, n={total})")

# ─── Per-pair breakdown ──────────────────────────────────────────────────
print()
print("=" * 78)
print(" 📊 PER-PAIR MODULE PERFORMANCE")
print("=" * 78)

for asset in sorted(pair_module_stats.keys()):
    pair_data = pair_module_stats[asset]
    print()
    print(f"  ── {asset} ──────────────────────────────────────")
    print(f"  {'Module':<18} {'CALL c/w':>10} {'PUT c/w':>10} {'Total':>7} {'Win%':>7}")
    for module_key, display_name in MODULE_NAMES.items():
        stats = pair_data.get(module_key)
        if not stats:
            continue
        call_c = stats["CALL"]["correct"]
        call_w = stats["CALL"]["wrong"]
        put_c = stats["PUT"]["correct"]
        put_w = stats["PUT"]["wrong"]
        total_c = call_c + put_c
        total_w = call_w + put_w
        total_all = total_c + total_w
        if total_all == 0:
            continue
        win_pct = (total_c / total_all * 100) if total_all else 0
        call_str = f"{call_c}/{call_w}"
        put_str = f"{put_c}/{put_w}"
        print(f"  {display_name:<18} {call_str:>10} {put_str:>10} {total_all:>7} {win_pct:>6.1f}%")

# ─── Overall accuracy ────────────────────────────────────────────────────
print()
print("=" * 78)
print(" 📈 OVERALL ACCURACY")
print("=" * 78)
acc_rows = cur.execute("""
    SELECT signal, accuracy, COUNT(*) as n
    FROM signal_log
    WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
    GROUP BY signal, accuracy
""").fetchall()

total_correct = 0
total_wrong = 0
call_correct = call_wrong = put_correct = put_wrong = 0
for r in acc_rows:
    if r["accuracy"] == "correct":
        total_correct += r["n"]
        if r["signal"] == "CALL":
            call_correct = r["n"]
        else:
            put_correct = r["n"]
    else:
        total_wrong += r["n"]
        if r["signal"] == "CALL":
            call_wrong = r["n"]
        else:
            put_wrong = r["n"]

total_graded = total_correct + total_wrong
if total_graded > 0:
    overall = total_correct / total_graded * 100
    print(f"  Total graded signals: {total_graded}")
    print(f"  Correct: {total_correct}  |  Wrong: {total_wrong}")
    print(f"  Overall win rate: {overall:.1f}%")
    print()
    call_total = call_correct + call_wrong
    put_total = put_correct + put_wrong
    if call_total:
        print(f"  CALL signals: {call_correct}/{call_total} = {call_correct/call_total*100:.1f}% win")
    if put_total:
        print(f"  PUT signals:  {put_correct}/{put_total} = {put_correct/put_total*100:.1f}% win")
else:
    print("  No graded signals yet.")

# ─── Time range ──────────────────────────────────────────────────────────
print()
time_range = cur.execute("""
    SELECT MIN(ts) as oldest, MAX(ts) as newest
    FROM signal_log
""").fetchone()
if time_range and time_range["oldest"]:
    oldest = time.time() - time_range["oldest"]
    newest = time.time() - time_range["newest"]
    print(f"  Data range: {oldest/3600:.1f}h ago → {newest/3600:.1f}h ago")

conn.close()
print()
print("=" * 78)
print(" Report complete.")
print("=" * 78)
