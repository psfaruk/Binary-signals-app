"""
core/breakeven.py — Payout-to-breakeven calculator + per-pair profitability gate.

FIX (DEEP-FIX-2026-08-07): previously the engine traded every pair regardless
of whether its win rate beat the breakeven threshold. Several pairs were
chronically losing (BRLUSD_otc 44.3%, USDCOP_otc 44.8%, USDBDT_otc 45.4%)
because the breakeven at 85% OTC payout is 54.05% — those pairs were 8-10pp
below it. No mechanism existed to auto-skip unprofitable pairs.

This module provides:
  1. breakeven_for_payout(payout) → minimum win rate % required
  2. is_pair_profitable(asset, period, payout) → True/False
  3. pair_breakeven_report() → full report of all pairs vs breakeven
"""

import os
import time
import sqlite3
import threading
from typing import Optional, Tuple, Dict

# ── Constants ─────────────────────────────────────────────────────────────────

# Default payouts when not available from the broker.
DEFAULT_OTC_PAYOUT = int(os.environ.get("QX_PAYOUT_FLOOR_OTC",
                                         os.environ.get("QX_PAYOUT_FLOOR", "85")))
DEFAULT_REAL_PAYOUT = int(os.environ.get("QX_PAYOUT_FLOOR_REAL", "70"))

# How many recent graded signals to use for win-rate calculation.
BREAKEVEN_LOOKBACK_N = int(os.environ.get("QX_BREAKEVEN_LOOKBACK_N", "200"))

# Minimum samples before a pair can be judged.
BREAKEVEN_MIN_SAMPLES = int(os.environ.get("QX_BREAKEVEN_MIN_SAMPLES", "50"))

# Safety margin: a pair's win rate must clear breakeven by this margin
# (in percentage points) to avoid being disabled by noise.
BREAKEVEN_SAFETY_MARGIN_PP = float(os.environ.get("QX_BREAKEVEN_SAFETY_MARGIN", "2.0"))

# Cache TTL for per-pair profitability checks.
_CACHE_TTL = float(os.environ.get("QX_BREAKEVEN_CACHE_TTL", "120"))
_cache: Dict[Tuple[str, int, int], Tuple[bool, float, float, float]] = {}
_cache_lock = threading.Lock()

SECONDS_PER_DAY = 86400


# ── Breakeven Calculator ──────────────────────────────────────────────────────

def breakeven_for_payout(payout: int | float | None) -> float:
    """Calculate the minimum win rate (%) required to break even.

    Binary options payout formula:
        EV = (win_rate × payout%) - ((1 - win_rate) × 100%)
        Set EV = 0 → breakeven_win_rate = 100 / (100 + payout)

    Examples:
        payout 85% → breakeven = 100 / 185 = 54.05%
        payout 80% → breakeven = 100 / 180 = 55.56%
        payout 70% → breakeven = 100 / 170 = 58.82%
        payout 90% → breakeven = 100 / 190 = 52.63%
    """
    if payout is None or payout <= 0:
        payout = DEFAULT_OTC_PAYOUT
    return round(10000.0 / (100.0 + float(payout)), 2)


def _get_db_path() -> str:
    """Resolve the signals.db path consistently."""
    db_path = os.environ.get("DB_PATH", "")
    candidates = [
        db_path,
        "/app/data/signals.db",
        os.path.join(os.path.dirname(__file__), "..", "signals.db"),
        "signals.db",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(__file__), "..", "signals.db")


def _category_for_asset(asset: str) -> str:
    return "otc" if asset.endswith("_otc") else "real"


def _default_payout_for(asset: str) -> int:
    return DEFAULT_OTC_PAYOUT if asset.endswith("_otc") else DEFAULT_REAL_PAYOUT


def get_pair_recent_win_rate(asset: str, period: int = 60,
                              n: int = None) -> Optional[Tuple[float, int]]:
    """Return (win_rate_pct, sample_count) for recent signals, or None.

    Only considers signals graded 'correct' or 'wrong' in the last 7 days.
    """
    if n is None:
        n = BREAKEVEN_LOOKBACK_N
    db_path = _get_db_path()
    cutoff = time.time() - 7 * SECONDS_PER_DAY
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT accuracy FROM signal_log
            WHERE asset = ? AND period = ?
              AND signal IN ('CALL', 'PUT')
              AND accuracy IN ('correct', 'wrong')
              AND ctime > ?
            ORDER BY ctime DESC, id DESC
            LIMIT ?
        """, (asset, period, cutoff, n)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"[breakeven] DB error for {asset}: {e}")
        return None

    if not rows:
        return None
    correct = sum(1 for r in rows if r["accuracy"] == "correct")
    total = len(rows)
    win_rate = round(100.0 * correct / total, 2) if total > 0 else 0.0
    return win_rate, total


def is_pair_profitable(asset: str, period: int = 60,
                        payout: int = None) -> Tuple[bool, str, float, float, int]:
    """Check if a pair's recent win rate clears the breakeven threshold.

    Returns:
        (is_profitable, reason, win_rate, breakeven, sample_count)

    A pair is profitable if:
        recent_win_rate >= (breakeven - safety_margin)
    The safety margin prevents disabling pairs due to statistical noise.
    """
    if payout is None:
        payout = _default_payout_for(asset)
    breakeven = breakeven_for_payout(payout)
    effective_threshold = breakeven - BREAKEVEN_SAFETY_MARGIN_PP

    cache_key = (asset, period, payout)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and (now - cached[0]) < _CACHE_TTL:
            _, is_prof, reason, wr, n = cached
            return is_prof, reason, wr, breakeven, n

    result = get_pair_recent_win_rate(asset, period)
    if result is None:
        reason = f"{asset}: insufficient data for breakeven check"
        with _cache_lock:
            _cache[cache_key] = (now, True, reason, 0.0, 0)
        return True, reason, 0.0, breakeven, 0

    win_rate, sample_count = result

    if sample_count < BREAKEVEN_MIN_SAMPLES:
        reason = (f"{asset}: only {sample_count} graded signals "
                  f"(need {BREAKEVEN_MIN_SAMPLES}) — not enough data to judge")
        with _cache_lock:
            _cache[cache_key] = (now, True, reason, win_rate, sample_count)
        return True, reason, win_rate, breakeven, sample_count

    if win_rate >= effective_threshold:
        reason = (f"{asset}: {win_rate:.1f}% WR ≥ {effective_threshold:.1f}% "
                  f"(breakeven={breakeven:.1f}% @ {payout}% payout)")
        is_prof = True
    else:
        loss_pp = round(effective_threshold - win_rate, 1)
        reason = (f"[AUTO-DISABLE] {asset}: {win_rate:.1f}% WR < "
                  f"{effective_threshold:.1f}% threshold "
                  f"(breakeven={breakeven:.1f}% @ {payout}% payout, "
                  f"deficit={loss_pp}pp, n={sample_count})")
        is_prof = False

    with _cache_lock:
        _cache[cache_key] = (now, is_prof, reason, win_rate, sample_count)
    return is_prof, reason, win_rate, breakeven, sample_count


def pair_breakeven_report() -> Dict:
    """Generate a full report of all known pairs vs their breakeven."""
    from core.constants import ALLOWED_PAIRS_OTC, ALLOWED_PAIRS_REAL

    report = {"timestamp": time.time(), "pairs": [], "summary": {}}
    disabled = 0
    profitable = 0
    unknown = 0

    for asset in sorted(ALLOWED_PAIRS_OTC + ALLOWED_PAIRS_REAL):
        is_prof, reason, wr, be, n = is_pair_profitable(asset)
        status = "profitable" if is_prof else "DISABLED"
        if is_prof:
            if n >= BREAKEVEN_MIN_SAMPLES:
                profitable += 1
            else:
                unknown += 1
        else:
            disabled += 1
        report["pairs"].append({
            "asset": asset,
            "status": status,
            "win_rate_pct": wr,
            "breakeven_pct": be,
            "margin_pp": round(wr - be, 1) if wr > 0 else None,
            "sample_count": n,
            "reason": reason,
        })

    report["summary"] = {
        "total": len(report["pairs"]),
        "profitable": profitable,
        "disabled": disabled,
        "unknown": unknown,
        "breakeven_otc": breakeven_for_payout(DEFAULT_OTC_PAYOUT),
        "breakeven_real": breakeven_for_payout(DEFAULT_REAL_PAYOUT),
    }
    return report


def invalidate_cache():
    """Clear the profitability check cache (call after new signals are graded)."""
    with _cache_lock:
        _cache.clear()
