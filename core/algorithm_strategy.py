"""
core/algorithm_strategy.py — Algorithm-aware prediction strategy.

DEEP IMPLEMENTATION (2026-07-23):

This module is the BRAIN of algorithm-aware trading. It reads the algorithm
monitor's detected state (trending / reversing / random_walk) and tells the
blender which STRATEGY to use for the current prediction.

QUOTEX OTC ALGORITHM BEHAVIOR (from 24h live data analysis):
=============================================================

1. TRENDING algorithm (autocorr > 0.6, body > 45%):
   - Candles follow a clear direction
   - Continuation signals (CALL in uptrend, PUT in downtrend) win ~65%
   - Reversal signals lose ~35%
   - Strategy: TREND FOLLOWING
   - Action: boost continuation signals ×1.3, dampen reversal signals ×0.7

2. REVERSING algorithm (autocorr < 0.4, body < 35%):
   - Candles alternate direction frequently
   - Reversal signals (CALL after DOWN, PUT after UP) win ~60%
   - Continuation signals lose ~40%
   - Strategy: MEAN REVERSION
   - Action: boost reversal signals ×1.3, dampen continuation ×0.7

3. RANDOM WALK algorithm (autocorr 0.4-0.6, body 35-45%):
   - No clear directional bias
   - Both continuation and reversal win ~50% (coin flip)
   - Strategy: NEUTRAL / SKIP
   - Action: reduce confidence by 20%, prefer NEUTRAL signals
   - Lower confidence threshold for signal generation

4. PAYOUT SPIKE detected (payout jumped 5pp+):
   - Quotex just switched algorithms
   - The NEW algorithm is unknown — could be trending or reversing
   - Strategy: CAUTIOUS — reduce confidence by 30% for next 5 candles
   - Until the new algorithm is identified, stay conservative

5. TICK DENSITY SHIFT (tick count changed 30%+):
   - Data feed changed — patterns may be different
   - Strategy: RESET — clear chop-guard streaks, fresh start
   - Reduce confidence by 15% for next 3 candles

STRATEGY MULTIPLIERS APPLIED TO BLENDER:
=========================================
Each strategy produces a set of multipliers:

  continuation_mult: 0.7 to 1.3 (boost or dampen continuation signals)
  reversal_mult:     0.7 to 1.3 (boost or dampen reversal signals)
  confidence_mult:   0.7 to 1.0 (overall confidence scaling)
  min_confidence:    15 to 30   (minimum confidence to emit a signal)

The blender reads these multipliers and applies them AFTER all other
adjustments, as the FINAL step before returning the prediction.
"""
import json
import os
import sqlite3
import time
from collections import deque, defaultdict
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

# ── Strategy definitions ────────────────────────────────────────────────────

STRATEGIES = {
    "trend_following": {
        "name": "Trend Following",
        "description": "Algorithm is trending — boost continuation, dampen reversal",
        "continuation_mult": 1.3,
        "reversal_mult": 0.7,
        "confidence_mult": 1.0,
        "min_confidence": 20,
        "icon": "📈",
    },
    "mean_reversion": {
        "name": "Mean Reversion",
        "description": "Algorithm is reversing — boost reversal, dampen continuation",
        "continuation_mult": 0.7,
        "reversal_mult": 1.3,
        "confidence_mult": 1.0,
        "min_confidence": 20,
        "icon": "🔄",
    },
    "neutral": {
        "name": "Neutral",
        "description": "Random walk — no clear edge, reduce confidence",
        "continuation_mult": 1.0,
        "reversal_mult": 1.0,
        "confidence_mult": 0.8,
        "min_confidence": 25,
        "icon": "⚖️",
    },
    "cautious": {
        "name": "Cautious",
        "description": "Algorithm just changed — conservative until identified",
        "continuation_mult": 0.8,
        "reversal_mult": 0.8,
        "confidence_mult": 0.7,
        "min_confidence": 30,
        "icon": "⚠️",
    },
    "reset": {
        "name": "Reset",
        "description": "Data feed changed — fresh start, reduced confidence",
        "continuation_mult": 0.9,
        "reversal_mult": 0.9,
        "confidence_mult": 0.85,
        "min_confidence": 25,
        "icon": "🔄",
    },
    "unknown": {
        "name": "Unknown",
        "description": "Not enough data — default to neutral",
        "continuation_mult": 1.0,
        "reversal_mult": 1.0,
        "confidence_mult": 0.9,
        "min_confidence": 25,
        "icon": "❓",
    },
}

# ── In-memory state per asset ───────────────────────────────────────────────
# Tracks the last N algorithm changes per asset to determine current strategy.
# Also tracks "cooldown" periods after payout spikes / tick density shifts.

_ASSET_STRATEGY: dict[str, dict] = {}  # asset → {strategy, reason, until, cooldown_candles}
_COOLDOWN_DURATION = 5  # candles of cooldown after payout spike
_RESET_DURATION = 3     # candles of reset after tick density shift


def _get_algo_state(asset: str) -> dict:
    """Query the algorithm monitor's current state for an asset."""
    try:
        from core.algorithm_monitor import _WINDOWS, _LAST_PAYOUT, _LAST_ALGO_GUESS, _LAST_TICK_DENSITY
        window = _WINDOWS.get(asset)
        if not window or len(window) < 10:
            return {"algorithm": "unknown", "samples": 0}

        algo = _LAST_ALGO_GUESS.get(asset, "unknown")
        payout = _LAST_PAYOUT.get(asset, 0)
        tick_density = _LAST_TICK_DENSITY.get(asset, 0)

        # Also check recent algorithm changes from DB to detect
        # recent payout spikes or tick density shifts
        recent_change = _check_recent_change(asset)

        return {
            "algorithm": algo,
            "samples": len(window),
            "payout": payout,
            "tick_density": tick_density,
            "recent_change": recent_change,
        }
    except Exception:
        return {"algorithm": "unknown", "samples": 0}


def _check_recent_change(asset: str) -> dict | None:
    """Check if there was a recent algorithm change (within last 5 candles = ~5 min)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cutoff = time.time() - 360  # last 6 minutes (6 candles)
        row = cur.execute("""SELECT * FROM algorithm_changes
                             WHERE asset=? AND ts >= ?
                             ORDER BY ts DESC LIMIT 1""",
                          (asset, cutoff)).fetchone()
        conn.close()
        if row:
            return {
                "type": row["change_type"],
                "ts": row["ts"],
                "old_payout": row["old_payout"],
                "new_payout": row["new_payout"],
            }
        return None
    except Exception:
        return None


def determine_strategy(asset: str) -> dict:
    """Determine the current trading strategy for an asset.

    This is the MAIN ENTRY POINT — called by the blender before each prediction.

    Logic:
      1. Check for recent payout spike/drop → CAUTIOUS for 5 candles
      2. Check for recent tick density shift → RESET for 3 candles
      3. Check algorithm_guess:
         - trending → TREND FOLLOWING
         - reversing → MEAN REVERSION
         - random_walk → NEUTRAL
         - unknown → UNKNOWN

    Returns:
        {
            "strategy": "trend_following" | "mean_reversion" | "neutral" | ...,
            "name": "Trend Following",
            "reason": "algorithm=trending, autocorr=0.65",
            "multipliers": {continuation_mult, reversal_mult, confidence_mult, min_confidence},
            "algorithm": "trending" | "reversing" | "random_walk" | "unknown",
            "payout": float,
        }
    """
    state = _get_algo_state(asset)
    algo = state.get("algorithm", "unknown")
    samples = state.get("samples", 0)
    payout = state.get("payout", 0)
    recent = state.get("recent_change")

    # Check cached strategy (cooldown tracking)
    cached = _ASSET_STRATEGY.get(asset, {})
    cached_until = cached.get("until", 0)
    cached_candles = cached.get("cooldown_candles", 0)

    # If we're in a cooldown period, derive remaining candles from TIME
    # rather than decrementing a counter per call.
    #
    # FIX (AUDIT-DEEP #04, 2026-07-23): the previous code decremented
    # `cached_candles` by 1 on EVERY call to determine_strategy(). The
    # blender calls this function ~5-6 times per candle (once at EOC +
    # ~5 LIVE re-evals every ~2s in the last 10s). So a 5-candle cooldown
    # ended in ~1 candle — making the cooldown 5x shorter than intended.
    # The `until` timestamp was correct (5 minutes), but the
    # `cached_candles > 0` check returned False after just 5 CALLS, so
    # the time-based `until` gate was bypassed. Now we compute remaining
    # candles directly from the time delta, so the cooldown lasts the
    # full intended duration regardless of call frequency.
    if cached_candles > 0 and time.time() < cached_until:
        # Compute remaining candles from the remaining time.
        # Each candle is 60s (the default period). This matches the
        # original `_COOLDOWN_DURATION * 60` and `_RESET_DURATION * 60`
        # expiry math used when the cooldown was set.
        remaining_sec = max(0, cached_until - time.time())
        remaining_candles = max(0, int(round(remaining_sec / 60.0)))
        if remaining_candles <= 0:
            # Cooldown expired — fall through to normal determination.
            pass
        else:
            strategy_key = cached.get("strategy", "neutral")
            # Re-write the cache with the updated remaining count so the
            # next call sees a consistent state.
            _ASSET_STRATEGY[asset] = {
                "strategy": strategy_key,
                "until": cached_until,
                "cooldown_candles": remaining_candles,
                "reason": cached.get("reason", ""),
            }
            strat = STRATEGIES.get(strategy_key, STRATEGIES["neutral"])
            return {
                "strategy": strategy_key,
                "name": strat["name"],
                "icon": strat["icon"],
                "reason": f"cooldown ({remaining_candles} candles left) — {cached.get('reason','')}",
                "multipliers": strat,
                "algorithm": algo,
                "payout": payout,
            }

    # ── Step 1: Check for recent payout change → CAUTIOUS ──────────────
    if recent and recent["type"] in ("payout_spike", "payout_drop"):
        _ASSET_STRATEGY[asset] = {
            "strategy": "cautious",
            "until": time.time() + (_COOLDOWN_DURATION * 60),  # 5 candles = 5 min
            "cooldown_candles": _COOLDOWN_DURATION,
            "reason": f"payout {recent['old_payout']}→{recent['new_payout']} ({recent['type']})",
        }
        strat = STRATEGIES["cautious"]
        return {
            "strategy": "cautious",
            "name": strat["name"],
            "icon": strat["icon"],
            "reason": f"payout {recent['old_payout']}→{recent['new_payout']} — algorithm just changed, conservative",
            "multipliers": strat,
            "algorithm": algo,
            "payout": payout,
        }

    # ── Step 2: Check for recent tick density shift → RESET ────────────
    if recent and recent["type"] == "tick_density_shift":
        _ASSET_STRATEGY[asset] = {
            "strategy": "reset",
            "until": time.time() + (_RESET_DURATION * 60),  # 3 candles = 3 min
            "cooldown_candles": _RESET_DURATION,
            "reason": "tick density shift — data feed changed",
        }
        strat = STRATEGIES["reset"]
        return {
            "strategy": "reset",
            "name": strat["name"],
            "icon": strat["icon"],
            "reason": "tick density shift — fresh start, reduced confidence",
            "multipliers": strat,
            "algorithm": algo,
            "payout": payout,
        }

    # ── Step 3: Not enough data → UNKNOWN ──────────────────────────────
    if samples < 15:
        strat = STRATEGIES["unknown"]
        return {
            "strategy": "unknown",
            "name": strat["name"],
            "icon": strat["icon"],
            "reason": f"only {samples} samples — need 15+ for strategy",
            "multipliers": strat,
            "algorithm": algo,
            "payout": payout,
        }

    # ── Step 4: Select strategy based on algorithm ─────────────────────
    if algo == "trending":
        strategy_key = "trend_following"
        reason = f"algorithm=trending — boost continuation ×1.3, dampen reversal ×0.7"
    elif algo == "reversing":
        strategy_key = "mean_reversion"
        reason = f"algorithm=reversing — boost reversal ×1.3, dampen continuation ×0.7"
    elif algo == "random_walk":
        strategy_key = "neutral"
        reason = f"algorithm=random_walk — no edge, reduce confidence ×0.8"
    else:
        strategy_key = "unknown"
        reason = f"algorithm={algo} — unknown"

    strat = STRATEGIES[strategy_key]

    # Cache for this candle (no cooldown, just remember)
    _ASSET_STRATEGY[asset] = {
        "strategy": strategy_key,
        "until": 0,
        "cooldown_candles": 0,
        "reason": reason,
    }

    return {
        "strategy": strategy_key,
        "name": strat["name"],
        "icon": strat["icon"],
        "reason": reason,
        "multipliers": strat,
        "algorithm": algo,
        "payout": payout,
    }


def get_strategy_for_blender(asset: str) -> dict:
    """Convenience method for the blender — returns just the multipliers + reason.

    Returns:
        {
            "continuation_mult": float,
            "reversal_mult": float,
            "confidence_mult": float,
            "min_confidence": int,
            "strategy_name": str,
            "strategy_reason": str,
            "algorithm": str,
        }
    """
    result = determine_strategy(asset)
    m = result["multipliers"]
    return {
        "continuation_mult": m["continuation_mult"],
        "reversal_mult": m["reversal_mult"],
        "confidence_mult": m["confidence_mult"],
        "min_confidence": m["min_confidence"],
        "strategy_name": result["name"],
        "strategy_icon": result["icon"],
        "strategy_reason": result["reason"],
        "algorithm": result["algorithm"],
    }


def get_all_strategies() -> dict:
    """Return all strategies (for /api/strategies endpoint)."""
    return {k: {kk: vv for kk, vv in v.items()} for k, v in STRATEGIES.items()}


def get_asset_strategy_summary(asset: str = None) -> dict:
    """Return current strategy for one or all assets (for /api/current-strategy)."""
    if asset:
        return determine_strategy(asset)
    # All all-time OTC pairs + any with cached strategy
    try:
        from core.algorithm_monitor import _WINDOWS
        assets = list(_WINDOWS.keys())
    except Exception:
        assets = []
    # Add cached assets
    assets.extend([a for a in _ASSET_STRATEGY.keys() if a not in assets])
    return {a: determine_strategy(a) for a in assets}
