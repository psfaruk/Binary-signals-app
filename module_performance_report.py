#!/usr/bin/env python3
"""Per-module performance report from signals.db."""
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent
sys.path.append(str(APP_DIR))

from db import DB_PATH  # noqa: E402

MIN_SAMPLES_FOR_GRADED = 5


def main():
    """Run the per-module performance report."""
    if not Path(DB_PATH).exists():
        print(f"[ERROR] signals.db not found at: {DB_PATH}\n\n"
              "This means the app hasn't run on this machine yet, or DB_PATH\n"
              "is set to a different location. To find it:\n"
              "  - Check if signals.db exists: ls signals.db\n"
              "  - Or set DB_PATH env var: export DB_PATH=/path/to/signals.db")
        sys.exit(1)

    # Shared stats computer — single source of truth for module names.
    from core.stats import compute_module_stats

    try:
        stats = compute_module_stats(DB_PATH)
    except Exception as e:
        print(f"[ERROR] could not compute stats from {DB_PATH}: {e}\n\n"
              "This usually means the DB exists but signal_log table is\n"
              "missing. Run `python -c 'import db; db.init()'` to create it.")
        sys.exit(1)

    # `--json` flag for CLI piping.
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

    # Print formatted report — use .get() for safe dict access.
    print("=" * 80 + "\n  PER-MODULE PERFORMANCE REPORT\n" + "=" * 80 + "\n")
    print(f"Total signals logged:  {stats.get('total_signals', 0)}")
    print(f"Total graded signals:  {stats.get('total_graded', 0)}")
    print(f"Overall win rate:      {stats.get('overall_win_pct', 0.0)}%")
    print(f"  Correct:             {stats.get('total_correct', 0)}")
    print(f"  Wrong:               {stats.get('total_wrong', 0)}")
    print()

    print("─" * 80 + "\n" +
          f"{'Module':<20} {'Total':>6} {'Correct':>8} {'Wrong':>6} {'Win%':>7} "
          f"{'CALL Win%':>10} {'PUT Win%':>10}\n" +
          "─" * 80)

    for m in stats.get("modules", []):
        print(f"{m.get('display_name', '?'):<20} {m.get('total', 0):>6} "
              f"{m.get('correct', 0):>8} {m.get('wrong', 0):>6} "
              f"{(m.get('win_pct') or 0.0):>6.1f}% "
              f"{(m.get('call_win_pct') or 0.0):>9.1f}% "
              f"{(m.get('put_win_pct') or 0.0):>9.1f}%")

    print("─" * 80 + "\n")

    # Best and worst modules (min samples threshold applied).
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
        print("=" * 80 + "\n  PER-PAIR MODULE PERFORMANCE\n" + "=" * 80)
        for asset in sorted(pairs.keys()):
            pair_data = pairs[asset]
            print(f"\n  {asset}:")
            for module_key, m in pair_data.items():
                display = m.get("display_name", module_key)
                print(f"    {display:<20} {(m.get('total') or 0):>4} signals  "
                      f"{(m.get('win_pct') or 0.0):>5.1f}% win  "
                      f"({m.get('correct', 0)}c/{m.get('wrong', 0)}w)")

    print("\n" + "=" * 80 +
          "\n  Done. Use /api/stats on the running app for the JSON version.\n"
          "  Or run: python module_performance_report.py --json\n" +
          "=" * 80)


if __name__ == "__main__":
    main()
