#!/usr/bin/env python3
"""
Per-module performance report from signals.db.

Run on the machine where the app actually runs (where signals.db exists):
    python module_performance_report.py
    python module_performance_report.py --json   # JSON output for piping

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

FIX (DEEP-AUDIT-2026-07-26 / F-14-33..F-14-38): refactored per audit
report A-07 problems 94-100. Added `if __name__ == "__main__":` guard,
`sys.path.append` instead of `insert(0, ...)`, imported DB_PATH from db,
used `.get()` for dict access, named the `>= 5` magic-number threshold,
and added `--json` output flag.
"""
import sys
from pathlib import Path

# FIX (DEEP-AUDIT-2026-07-26 / F-14-33): use `append` instead of
# `insert(0, ...)` so the app dir doesn't shadow installed packages of
# the same name (e.g. a pip-installed `core` package elsewhere).
APP_DIR = Path(__file__).parent
sys.path.append(str(APP_DIR))

# FIX (DEEP-AUDIT-2026-07-26 / F-14-34): import DB_PATH from db instead
# of duplicating the env-var fallback logic. Drift risk if db.py
# changes its default path.
from db import DB_PATH  # noqa: E402

# FIX (DEEP-AUDIT-2026-07-26 / F-14-35): magic number for "graded module"
# minimum sample threshold — was a bare `>= 5` literal at L80. Now a named
# constant at module scope so it's discoverable + adjustable.
MIN_SAMPLES_FOR_GRADED = 5


def main():
    """Run the per-module performance report.

    FIX (DEEP-AUDIT-2026-07-26 / F-14-36): wrapped L44-105 in main() +
    `if __name__ == "__main__":` guard. Previously the script executed at
    import time — importing this module (e.g. for testing) ran the
    report and exited the process via sys.exit(1). Also moved the
    `from core.stats import compute_module_stats` import inside main()
    so an ImportError doesn't shadow the helpful DB_PATH error message.
    """
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

    # FIX (DEEP-AUDIT-2026-07-26 / F-14-40): if the DB exists but
    # signal_log table is missing (e.g., points to a different app's
    # signals.db, or db.init() hasn't run yet), compute_module_stats
    # crashes with sqlite3.OperationalError. Catch and print a helpful
    # message instead of dumping a traceback.
    try:
        stats = compute_module_stats(DB_PATH)
    except Exception as e:
        print(f"[ERROR] could not compute stats from {DB_PATH}: {e}")
        print()
        print("This usually means the DB exists but signal_log table is")
        print("missing. Run `python -c 'import db; db.init()'` to create it.")
        sys.exit(1)

    # FIX (DEEP-AUDIT-2026-07-26 / F-14-37): `--json` flag for CLI piping.
    # Previously only human-readable text output was available, despite
    # the comment at the bottom suggesting `/api/stats` for JSON.
    if "--json" in sys.argv:
        import json
        print(json.dumps(stats, indent=2, default=str))
        return

    if stats.get("error"):
        print(f"[ERROR] {stats['error']}")
        sys.exit(1)

    if stats.get("message"):
        print(stats["message"])
        sys.exit(0)

    # ─── Print formatted report ──────────────────────────────────────────────
    # FIX (DEEP-AUDIT-2026-07-26 / F-14-38): use `.get(key, default)` for all
    # dict accesses — previously direct `stats['total_signals']` raised
    # KeyError if compute_module_stats returned a dict missing that key
    # (e.g. on DB error).
    print("=" * 80)
    print("  PER-MODULE PERFORMANCE REPORT")
    print("=" * 80)
    print()
    print(f"Total signals logged:  {stats.get('total_signals', 0)}")
    print(f"Total graded signals:  {stats.get('total_graded', 0)}")
    print(f"Overall win rate:      {stats.get('overall_win_pct', 0.0)}%")
    print(f"  Correct:             {stats.get('total_correct', 0)}")
    print(f"  Wrong:               {stats.get('total_wrong', 0)}")
    print()

    print("─" * 80)
    print(f"{'Module':<20} {'Total':>6} {'Correct':>8} {'Wrong':>6} {'Win%':>7} "
          f"{'CALL Win%':>10} {'PUT Win%':>10}")
    print("─" * 80)

    for m in stats.get("modules", []):
        # FIX (DEEP-AUDIT-2026-07-26 / F-14-39): the original used direct
        # `m['win_pct']` access which crashed with TypeError if the key
        # was None (no graded signals for this module). `.get(key, default)`
        # only falls back when the key is MISSING — None values still
        # propagate. Use `or 0.0` to coerce both None and missing to 0.0.
        print(f"{m.get('display_name', '?'):<20} {m.get('total', 0):>6} "
              f"{m.get('correct', 0):>8} {m.get('wrong', 0):>6} "
              f"{(m.get('win_pct') or 0.0):>6.1f}% "
              f"{(m.get('call_win_pct') or 0.0):>9.1f}% "
              f"{(m.get('put_win_pct') or 0.0):>9.1f}%")

    print("─" * 80)
    print()

    # Best and worst modules (by win%, min samples).
    # FIX (F-14-35): replaced bare `>= 5` magic number with named constant.
    # FIX (F-14-39b): use `or 0.0` in max/min key to handle None win_pct.
    graded = [m for m in stats.get("modules", [])
              if (m.get("total") or 0) >= MIN_SAMPLES_FOR_GRADED]
    if graded:
        best = max(graded, key=lambda m: m.get("win_pct") or 0.0)
        worst = min(graded, key=lambda m: m.get("win_pct") or 0.0)
        print(f"Best module:  {best.get('display_name', '?')}  "
              f"({(best.get('win_pct') or 0.0):.1f}% win, n={best.get('total', 0)})")
        print(f"Worst module: {worst.get('display_name', '?')}  "
              f"({(worst.get('win_pct') or 0.0):.1f}% win, n={worst.get('total', 0)})")
        print()

    # Per-pair breakdown
    pairs = stats.get("pairs") or {}
    if pairs:
        print("=" * 80)
        print("  PER-PAIR MODULE PERFORMANCE")
        print("=" * 80)
        for asset in sorted(pairs.keys()):
            pair_data = pairs[asset]
            print(f"\n  {asset}:")
            for module_key, m in pair_data.items():
                display = m.get("display_name", module_key)
                # FIX (F-14-39c): `or 0.0` for None win_pct safety.
                print(f"    {display:<20} {(m.get('total') or 0):>4} signals  "
                      f"{(m.get('win_pct') or 0.0):>5.1f}% win  "
                      f"({m.get('correct', 0)}c/{m.get('wrong', 0)}w)")

    print()
    print("=" * 80)
    print("  Done. Use /api/stats on the running app for the JSON version.")
    print("  Or run: python module_performance_report.py --json")
    print("=" * 80)


if __name__ == "__main__":
    main()
