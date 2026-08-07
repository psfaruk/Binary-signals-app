"""
core/backtest.py — Simple walk-forward backtest using signal_log data.

FIX (DEEP-FIX-2026-08-07): the codebase had no automated way to verify
whether changes to the engine/agent/system actually improved win rate.
This module processes signal_log in chronological order and computes:

  1. Overall win rate + Wilson confidence interval
  2. Per-pair win rate breakdown
  3. Per-hour win rate breakdown
  4. Per-signal-quality tier win rate
  5. Consecutive win/loss streaks
  6. Profit factor (assuming fixed stake)
  7. Sharpe-like ratio

Used by /api/backtest/recent endpoint for on-demand validation.
"""

import os
import time
import math
import sqlite3
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone

SECONDS_PER_DAY = 86400


def _get_db_path() -> str:
    candidates = [
        os.environ.get("DB_PATH", ""),
        "/app/data/signals.db",
        os.path.join(os.path.dirname(__file__), "..", "signals.db"),
        "signals.db",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(__file__), "..", "signals.db")


def _wilson_bounds(correct: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score interval, returned as (lo, hi) in [0, 1]."""
    if total <= 0:
        return 0.0, 0.0
    p = correct / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    lo = (centre - margin) / denom
    hi = (centre + margin) / denom
    return max(0.0, lo), min(1.0, hi)


def _breakeven_for_payout(payout: int) -> float:
    """Return breakeven win rate as a fraction (e.g. 0.5405 for 85% payout)."""
    if payout <= 0:
        payout = 85
    return 100.0 / (100.0 + payout)


def run_backtest(days: int = 7, period: int = 60, payout: int = 85) -> dict:
    """Run walk-forward backtest on recent signal_log data.

    Args:
        days: how many days of data to include
        period: candle period in seconds (default 60)
        payout: payout percentage for profit calculation

    Returns:
        Backtest report dict
    """
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return {"error": f"DB not found at {db_path}"}

    cutoff = time.time() - days * SECONDS_PER_DAY
    breakeven = _breakeven_for_payout(payout)

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT asset, ctime, signal, confidence, strength, accuracy,
                   regime, zone, signal_quality, agree, postmortem
            FROM signal_log
            WHERE period = ? AND signal IN ('CALL', 'PUT')
              AND accuracy IN ('correct', 'wrong')
              AND ctime > ?
            ORDER BY ctime, asset
        """, (period, cutoff)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        return {"error": f"DB query failed: {e}"}

    if not rows:
        return {"error": f"no graded signals in last {days} days", "days": days}

    # ── Aggregate ─────────────────────────────────────────────────────────

    total = 0
    correct = 0
    wrong = 0
    per_pair: Dict[str, dict] = defaultdict(lambda: {"correct": 0, "wrong": 0})
    per_hour: Dict[int, dict] = defaultdict(lambda: {"correct": 0, "wrong": 0})
    per_quality: Dict[str, dict] = defaultdict(lambda: {"correct": 0, "wrong": 0})
    per_strength: Dict[str, dict] = defaultdict(lambda: {"correct": 0, "wrong": 0})
    per_regime: Dict[str, dict] = defaultdict(lambda: {"correct": 0, "wrong": 0})

    # Streak tracking
    current_streak = 0
    current_streak_type = None  # "win" or "loss"
    max_win_streak = 0
    max_loss_streak = 0
    win_streaks: List[int] = []
    loss_streaks: List[int] = []

    # P&L tracking
    cumulative_pnl = 0.0
    stake = 1.0  # $1 per trade
    pnl_series: List[float] = []

    for r in rows:
        is_correct = r["accuracy"] == "correct"
        total += 1
        if is_correct:
            correct += 1
            cumulative_pnl += stake * (payout / 100.0)
        else:
            wrong += 1
            cumulative_pnl -= stake
        pnl_series.append(cumulative_pnl)

        asset = r["asset"]
        per_pair[asset]["correct"] += 1 if is_correct else 0
        per_pair[asset]["wrong"] += 0 if is_correct else 1

        try:
            hour = datetime.fromtimestamp(r["ctime"], tz=timezone.utc).hour
            per_hour[hour]["correct"] += 1 if is_correct else 0
            per_hour[hour]["wrong"] += 0 if is_correct else 1
        except Exception:
            pass

        quality = r["signal_quality"] or "UNKNOWN"
        per_quality[quality]["correct"] += 1 if is_correct else 0
        per_quality[quality]["wrong"] += 0 if is_correct else 1

        strength = r["strength"] or "UNKNOWN"
        per_strength[strength]["correct"] += 1 if is_correct else 0
        per_strength[strength]["wrong"] += 0 if is_correct else 1

        regime = r["regime"] or "UNKNOWN"
        per_regime[regime]["correct"] += 1 if is_correct else 0
        per_regime[regime]["wrong"] += 0 if is_correct else 1

        # Streak tracking
        if is_correct:
            if current_streak_type == "win":
                current_streak += 1
            else:
                if current_streak_type == "loss":
                    loss_streaks.append(current_streak)
                    max_loss_streak = max(max_loss_streak, current_streak)
                current_streak = 1
                current_streak_type = "win"
        else:
            if current_streak_type == "loss":
                current_streak += 1
            else:
                if current_streak_type == "win":
                    win_streaks.append(current_streak)
                    max_win_streak = max(max_win_streak, current_streak)
                current_streak = 1
                current_streak_type = "loss"

    # Capture final streak
    if current_streak_type == "win":
        win_streaks.append(current_streak)
        max_win_streak = max(max_win_streak, current_streak)
    elif current_streak_type == "loss":
        loss_streaks.append(current_streak)
        max_loss_streak = max(max_loss_streak, current_streak)

    # ── Compute stats ─────────────────────────────────────────────────────

    win_rate = round(100.0 * correct / total, 2) if total > 0 else 0.0
    lo, hi = _wilson_bounds(correct, total)
    profit_factor = (correct * (payout / 100.0)) / max(1, wrong) if wrong > 0 else float("inf")

    # Sharpe-like: mean(pnl_change) / std(pnl_change)
    if len(pnl_series) >= 2:
        changes = [pnl_series[i] - pnl_series[i-1] for i in range(1, len(pnl_series))]
        mean_change = sum(changes) / len(changes)
        variance = sum((c - mean_change) ** 2 for c in changes) / len(changes)
        std_change = math.sqrt(max(1e-9, variance))
        sharpe_like = round(mean_change / std_change * math.sqrt(52560), 2)  # annualized
    else:
        sharpe_like = 0.0

    # ── Per-pair breakdown ────────────────────────────────────────────────

    pair_breakdown = []
    for asset, counts in sorted(per_pair.items()):
        t = counts["correct"] + counts["wrong"]
        wr = round(100.0 * counts["correct"] / t, 1) if t > 0 else 0.0
        lo_p, hi_p = _wilson_bounds(counts["correct"], t)
        pair_breakdown.append({
            "asset": asset,
            "total": t,
            "correct": counts["correct"],
            "wrong": counts["wrong"],
            "win_rate": wr,
            "wilson_lo": round(lo_p * 100, 1),
            "wilson_hi": round(hi_p * 100, 1),
            "beats_breakeven": lo_p > breakeven,
        })

    # ── Per-hour breakdown ────────────────────────────────────────────────

    hour_breakdown = []
    for hour in range(24):
        counts = per_hour.get(hour, {"correct": 0, "wrong": 0})
        t = counts["correct"] + counts["wrong"]
        wr = round(100.0 * counts["correct"] / t, 1) if t > 0 else 0.0
        hour_breakdown.append({
            "hour_utc": hour,
            "total": t,
            "win_rate": wr,
        })

    # ── Per-quality tier ──────────────────────────────────────────────────

    quality_breakdown = []
    for tier, counts in sorted(per_quality.items()):
        t = counts["correct"] + counts["wrong"]
        wr = round(100.0 * counts["correct"] / t, 1) if t > 0 else 0.0
        quality_breakdown.append({
            "tier": tier,
            "total": t,
            "correct": counts["correct"],
            "wrong": counts["wrong"],
            "win_rate": wr,
        })

    # ── Per-strength ──────────────────────────────────────────────────────

    strength_breakdown = []
    for s, counts in sorted(per_strength.items()):
        t = counts["correct"] + counts["wrong"]
        wr = round(100.0 * counts["correct"] / t, 1) if t > 0 else 0.0
        strength_breakdown.append({
            "strength": s,
            "total": t,
            "win_rate": wr,
        })

    return {
        "backtest": {
            "days": days,
            "period_sec": period,
            "payout_pct": payout,
            "breakeven_pct": round(breakeven * 100, 2),
            "total_signals": total,
            "correct": correct,
            "wrong": wrong,
            "win_rate_pct": win_rate,
            "wilson_lo_pct": round(lo * 100, 1),
            "wilson_hi_pct": round(hi * 100, 1),
            "beats_breakeven": lo > breakeven,
            "profit_factor": round(profit_factor, 2),
            "cumulative_pnl": round(cumulative_pnl, 2),
            "sharpe_like": sharpe_like,
        },
        "streaks": {
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "avg_win_streak": round(sum(win_streaks) / len(win_streaks), 1) if win_streaks else 0,
            "avg_loss_streak": round(sum(loss_streaks) / len(loss_streaks), 1) if loss_streaks else 0,
        },
        "per_pair": pair_breakdown,
        "per_hour": hour_breakdown,
        "per_quality": quality_breakdown,
        "per_strength": strength_breakdown,
        "summary": (
            f"Win rate: {win_rate}% ({correct}/{total}) over {days}d. "
            f"95% CI: [{round(lo*100,1)}%-{round(hi*100,1)}%]. "
            f"Breakeven: {round(breakeven*100,1)}%. "
            f"{'✅ PROFITABLE' if lo > breakeven else '❌ UNPROFITABLE'}"
        ),
    }
