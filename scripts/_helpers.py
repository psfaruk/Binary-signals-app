"""
Shared helpers for scripts/ — test runner + candle builder.

FIX (DEEP-AUDIT-2026-07-26 / F-19-27): add this module so future scripts can
import a single canonical `test()` / `check()` / `build_candles()` instead
of re-defining them in every script (A-10 PROBLEM 77 — 5 scripts had
near-duplicate helpers).

Existing scripts (`backtest_fixes.py`, `backtest_remaining.py`,
`backtest_weak_neutral.py`, `verify_no_sim_mode.py`, `verify_live_audit_fixes.py`)
keep their own copies for backward compat; new scripts SHOULD import from here.

Usage:
    from scripts._helpers import build_candles, run_test, PASS, FAIL
"""
from __future__ import annotations

import sys
from typing import Callable, List, Tuple

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# Windows-safe ANSI fallback (A-10 PROBLEM 96, 108, 109).
_IS_WINDOWS = sys.platform.startswith("win")
if _IS_WINDOWS:
    try:
        import colorama  # type: ignore
        colorama.init()
        _ANSI_OK = True
    except ImportError:
        _ANSI_OK = False
else:
    _ANSI_OK = True


def build_candles(
    patterns: List[Tuple[float, float, float, float]],
    base_price: float = 1.0,
    base_t: int = 1_700_000_000,
) -> List[dict]:
    """Build a candle list from a list of (o, h, l, c) offset tuples.

    Each offset is added to `base_price` so tests can use small relative
    moves without committing to an absolute price level.
    """
    out = []
    for i, (o, h, l, c) in enumerate(patterns):
        out.append({
            "time": base_t + i * 60,
            "open": base_price + o,
            "high": base_price + h,
            "low": base_price + l,
            "close": base_price + c,
        })
    return out


def run_test(
    results: List[Tuple[str, str, str]],
    name: str,
    condition: bool,
    detail: str = "",
) -> bool:
    """Append (name, PASS/FAIL, detail) to `results`, print a line, return condition.

    `results` is the caller's accumulator list (each script maintains its own
    so the summary printer can count PASS/FAIL/SKIP at the end).
    """
    status = PASS if condition else FAIL
    results.append((name, status, detail))
    print(f"  [{status}] {name}")
    if detail:
        print(f"         {detail}")
    return condition


def print_summary(results: List[Tuple[str, str, str]]) -> int:
    """Print a pass/fail summary; return exit code (0 = pass, 1 = fail)."""
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    print("=" * 70)
    skip_str = f", {n_skip} SKIP" if n_skip else ""
    print(f"SUMMARY: {n_pass} PASS, {n_fail} FAIL{skip_str} "
          f"out of {len(results)} tests")
    print("=" * 70)
    if n_fail > 0:
        print("\nFAILED TESTS:")
        for name, status, detail in results:
            if status == FAIL:
                print(f"  [FAIL] {name}")
                if detail:
                    print(f"      {detail}")
        return 1
    print("\nALL TESTS PASSED — safe to push to GitHub.")
    return 0
