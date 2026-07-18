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

FIX (BUG-3, 2026-07-18): previously had a local MODULE_NAMES dict that
was missing `trend_follow` (the Real engine's 6th module), silently
undercounting Real-engine signals. Now uses the shared
`core.stats.compute_module_stats()` which sources module names from
`core.constants.MODULE_NAMES` — the single source of truth.
"""
import sys
import os
from pathlib import Path

# Make the app dir importable so we can use core.stats
APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

DB_PATH = os.environ.get("DB_PATH", str(APP_DIR / "signals.db"))

if not Path(DB_PATH).exists():
    print(f"[ERROR] signals.db not found at: {DB_PATH}")
    print()
    print("This means the app hasn't run on this machine yet, or DB_PATH")
    print("is set to a different location. To find it:")
    print("  - Check if signals.db exists: ls signals.db")
    print("  - Or set DB_PATH env var: export DB_PATH=/path/to/signals.db")
    sys.exit(1)

# Use the shared stats computer — single source of truth for module names.
from core.stats import compute_module_stats
from core.constants import MODULE_DISPLAY_NAMES

stats = compute_module_stats(DB_PATH)

if stats.get("error"):
    print(f"[ERROR] {stats['error']}")
    sys.exit(1)

if stats.get("message"):
    print(stats["message"])
    sys.exit(0)

# ─── Print formatted report ──────────────────────────────────────────────
print("=" * 80)
print("  PER-MODULE PERFORMANCE REPORT")
print("=" * 80)
print()
print(f"Total signals logged:  {stats['total_signals']}")
print(f"Total graded signals:  {stats['total_graded']}")
print(f"Overall win rate:      {stats['overall_win_pct']}%")
print(f"  Correct:             {stats['total_correct']}")
print(f"  Wrong:               {stats['total_wrong']}")
print()

print("─" * 80)
print(f"{'Module':<20} {'Total':>6} {'Correct':>8} {'Wrong':>6} {'Win%':>7} "
      f"{'CALL Win%':>10} {'PUT Win%':>10}")
print("─" * 80)

for m in stats["modules"]:
    print(f"{m['display_name']:<20} {m['total']:>6} {m['correct']:>8} "
          f"{m['wrong']:>6} {m['win_pct']:>6.1f}% "
          f"{m['call_win_pct']:>9.1f}% {m['put_win_pct']:>9.1f}%")

print("─" * 80)
print()

# Best and worst modules (by win%, min 5 samples)
graded = [m for m in stats["modules"] if m["total"] >= 5]
if graded:
    best = max(graded, key=lambda m: m["win_pct"])
    worst = min(graded, key=lambda m: m["win_pct"])
    print(f"Best module:  {best['display_name']}  ({best['win_pct']:.1f}% win, n={best['total']})")
    print(f"Worst module: {worst['display_name']}  ({worst['win_pct']:.1f}% win, n={worst['total']})")
    print()

# Per-pair breakdown
if stats["pairs"]:
    print("=" * 80)
    print("  PER-PAIR MODULE PERFORMANCE")
    print("=" * 80)
    for asset in sorted(stats["pairs"].keys()):
        pair_data = stats["pairs"][asset]
        print(f"\n  {asset}:")
        for module_key, m in pair_data.items():
            display = m.get("display_name", module_key)
            print(f"    {display:<20} {m['total']:>4} signals  "
                  f"{m['win_pct']:>5.1f}% win  "
                  f"({m['correct']}c/{m['wrong']}w)")

print()
print("=" * 80)
print("  Done. Use /api/stats on the running app for the JSON version.")
print("=" * 80)
