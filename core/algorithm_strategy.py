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
# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-17): enable PEP 604/PEP 585
# syntax on Python 3.8/3.9 (PEP 563 deferred evaluation). Without this, the
# `dict | None` and `dict[str, dict]` type hints raise TypeError at import
# time on Python 3.8/3.9, crashing the entire app.
from __future__ import annotations
import os
import sqlite3
import threading
import time
# FIX (DEEP-AUDIT-2026-07-26 / F-07-13): removed dead imports `json`,
# `deque`, `defaultdict`, `datetime`, `timezone` — none referenced in file.

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

# FIX (DEEP-AUDIT-2026-07-26 / F-07-91): moved algorithm_monitor imports to
# module level (was per-call local import inside `_get_algo_state`, called
# ~5-6×/candle/asset). algorithm_monitor does not import algorithm_strategy,
# so there's no circular-dependency risk.
# FIX (DEEP-AUDIT-2026-07-26 / F-07-17): log import failures so operators can
# diagnose (was silent `except Exception: return unknown`).
try:
    from core.algorithm_monitor import (
        _WINDOWS, _LAST_PAYOUT, _LAST_ALGO_GUESS, _LAST_TICK_DENSITY,
    )
except Exception as _algo_mon_import_err:
    print(f"[algo_strategy] WARN: algorithm_monitor import failed: "
          f"{_algo_mon_import_err!r} — every strategy will be 'unknown'")
    _WINDOWS = {}
    _LAST_PAYOUT = {}
    _LAST_ALGO_GUESS = {}
    _LAST_TICK_DENSITY = {}

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
#
# FIX (DEEP-AUDIT-2026-07-26 / F-07-14): added `_lock` for thread-safe access
# to `_ASSET_STRATEGY`. Multiple blender/API threads read+write this dict
# concurrently without a lock — partial-dict reads were possible.
# FIX (DEEP-AUDIT-2026-07-26 / F-07-90): env-configurable cooldown/reset
# durations (was hardcoded 5 / 3 candles).
# FIX (DEEP-AUDIT-2026-07-26 / F-07-21): env-configurable minimum-sample
# threshold for strategy determination (was hardcoded `samples < 15`).
# FIX (DEEP-AUDIT-2026-07-26 / F-07-16): TTL cache for `_check_recent_change`
# results — previously each `determine_strategy` call opened a fresh SQLite
# connection (~5-6 calls/candle/asset).
_ASSET_STRATEGY: dict[str, dict] = {}  # asset → {strategy, reason, until, cooldown_candles}
_COOLDOWN_DURATION = int(os.environ.get("STRATEGY_COOLDOWN_CANDLES", "5"))
_RESET_DURATION = int(os.environ.get("STRATEGY_RESET_CANDLES", "3"))
_MIN_SAMPLES = int(os.environ.get("STRATEGY_MIN_SAMPLES", "15"))
_RECENT_CHANGE_TTL = float(os.environ.get("STRATEGY_RECENT_CHANGE_TTL", "5"))
_RECENT_CHANGE_CACHE: dict[str, tuple[float, "dict | None"]] = {}
_lock = threading.Lock()


def _get_algo_state(asset: str, period: int = 60) -> dict:
    """Query the algorithm monitor's current state for an asset.

    FIX (DEEP-AUDIT-2026-07-26 / A-04-CRIT-1): added `period` parameter so
    the recent-change window uses the asset's actual candle size.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-15): aligned the early-return threshold
      with the strategy step's threshold (was `< 10` here but `< 15` in step 3,
      leaving 10-14-sample windows to compute an algorithm_guess the strategy
      step then ignored — wasted compute).
    FIX (DEEP-AUDIT-2026-07-26 / F-07-91): imports now at module level.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-17): log exceptions instead of silently
      returning 'unknown'.
    """
    try:
        window = _WINDOWS.get(asset)
        if not window or len(window) < _MIN_SAMPLES:
            return {"algorithm": "unknown", "samples": 0}

        algo = _LAST_ALGO_GUESS.get(asset, "unknown")
        payout = _LAST_PAYOUT.get(asset, 0)
        tick_density = _LAST_TICK_DENSITY.get(asset, 0)

        # Also check recent algorithm changes from DB to detect
        # recent payout spikes or tick density shifts
        recent_change = _check_recent_change(asset, period=period)

        return {
            "algorithm": algo,
            "samples": len(window),
            "payout": payout,
            "tick_density": tick_density,
            "recent_change": recent_change,
        }
    except Exception as e:
        # FIX (F-07-17): log instead of silently returning 'unknown'.
        print(f"[algo_strategy] _get_algo_state error for {asset!r}: {e!r}")
        return {"algorithm": "unknown", "samples": 0}


def _check_recent_change(asset: str, period: int = 60) -> dict | None:
    """Check if there was a recent algorithm change (within last cooldown window).

    FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-15 + AUDIT-LIVE-1-08):
      1. Cutoff is now `_COOLDOWN_DURATION * 60` (5 min) instead of hardcoded
         360s (6 min). The old 6-min window caused the cooldown to re-trigger
         after expiry (effective cooldown = 10 min instead of 5 min).
      2. Return ALL changes in the window (was `LIMIT 1` — most recent only).
         The old behavior masked payout spikes when a regime_shift happened
         afterward: the query returned the regime_shift, which didn't match
         the payout_spike/payout_drop branch, so `cautious` was never set.
         Callers iterate the list and check each against its applicable branch.

    FIX (DEEP-AUDIT-2026-07-26 / A-04-CRIT-1):
      The cutoff was hardcoded `_COOLDOWN_DURATION * 60` (assumes 60s candles).
      For 15s periods the cooldown was 20 candles (too long); for 300s periods
      the cooldown was 1 candle (too short). Now uses the asset's actual period.

    FIX (DEEP-AUDIT-2026-07-26 / F-07-16): added a TTL cache
      (`_RECENT_CHANGE_TTL` seconds, default 5) so callers don't open a fresh
      SQLite connection on every `determine_strategy` call (~5-6×/candle).
      Both hits and misses are cached (None cached too so empty-DB doesn't
      hammer the DB).
    """
    # FIX (F-07-16): TTL cache lookup.
    now = time.time()
    cached_entry = _RECENT_CHANGE_CACHE.get(asset)
    if cached_entry is not None and (now - cached_entry[0]) < _RECENT_CHANGE_TTL:
        return cached_entry[1]

    conn = sqlite3.connect(DB_PATH, timeout=5)
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-16): wrap connection lifecycle
    # in try/finally so the connection is closed even on exception. Previously
    # an exception between `sqlite3.connect()` and `conn.close()` leaked the
    # connection — under load this could exhaust file descriptors.
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # FIX (AUDIT-2-15): cutoff = cooldown duration * period (period-aware).
        # FIX (A-04-CRIT-1): use the asset's actual period, not hardcoded 60.
        safe_period = max(1, int(period))
        cutoff = time.time() - (_COOLDOWN_DURATION * safe_period)
        # FIX (AUDIT-LIVE-1-08): fetch ALL changes in the window (no LIMIT 1)
        # so callers can check each against its applicable branch.
        rows = cur.execute("""SELECT * FROM algorithm_changes
                             WHERE asset=? AND ts >= ?
                             ORDER BY ts DESC""",
                          (asset, cutoff)).fetchall()
        if not rows:
            _RECENT_CHANGE_CACHE[asset] = (now, None)
            return None
        # Return the most-recent row for backward compat (callers that only
        # need one change), but also expose the full list for callers that
        # need to scan all changes in the window.
        first = rows[0]
        result = {
            "type": first["change_type"],
            "ts": first["ts"],
            "old_payout": first["old_payout"],
            "new_payout": first["new_payout"],
            "all_changes": [
                {"type": r["change_type"], "ts": r["ts"],
                 "old_payout": r["old_payout"], "new_payout": r["new_payout"]}
                for r in rows
            ],
        }
        _RECENT_CHANGE_CACHE[asset] = (now, result)
        return result
    except Exception as e:
        # FIX (F-07-17): log instead of silently returning None.
        print(f"[algo_strategy] _check_recent_change error for {asset!r}: {e!r}")
        return None
    finally:
        conn.close()


def determine_strategy(asset: str, period: int = 60) -> dict:
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

    FIX (DEEP-AUDIT-2026-07-26 / A-04-CRIT-1): `period` parameter added.
      Previously all cooldown math assumed 60s candles. For 15s/30s/120s/300s/
      1800s/3600s periods the cooldown was wrong by the same factor. Now the
      cooldown duration is `candles * period` seconds, matching candle size.

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
    state = _get_algo_state(asset, period=period)
    algo = state.get("algorithm", "unknown")
    samples = state.get("samples", 0)
    payout = state.get("payout", 0)
    recent = state.get("recent_change")

    # FIX (A-04-CRIT-1): period-aware cooldown. Default 60 preserves backward
    # compat for any caller that didn't pass period.
    safe_period = max(1, int(period))

    # Check cached strategy (cooldown tracking)
    # FIX (DEEP-AUDIT-2026-07-26 / F-07-14): acquire `_lock` for the cache read
    #   so concurrent threads can't see a partial-dict write.
    # FIX (DEEP-AUDIT-2026-07-26 / F-07-02): dropped the redundant
    #   `cached_candles > 0` counter gate. The counter was a parallel source
    #   of truth alongside the timestamp; if it ever hit 0 while `until` was
    #   still in the future, the cooldown was bypassed. Now we rely solely on
    #   the time-based `until` timestamp.
    # FIX (DEEP-AUDIT-2026-07-26 / F-07-94): simplified cooldown check to a
    #   single time comparison (was `cached_candles > 0 AND time < until`).
    # FIX (DEEP-AUDIT-2026-07-26 / F-07-18): dropped the redundant cache
    #   rewrite on every cooldown call. The `until` timestamp doesn't change
    #   during cooldown, so the previous rewrite was a no-op behaviorally
    #   (pure write amplification).
    with _lock:
        cached = _ASSET_STRATEGY.get(asset, {})
    cached_until = cached.get("until", 0)
    if cached_until > time.time():
        # Cooldown still active. Compute remaining candles from the time delta
        # (no per-call counter decrement — see A-04 audit history).
        # FIX (A-04-CRIT-1): use asset's actual period (was hardcoded 60s).
        remaining_sec = max(0, cached_until - time.time())
        remaining_candles = max(0, int(round(remaining_sec / float(safe_period))))
        strategy_key = cached.get("strategy", "neutral")
        # FIX (F-07-92): log when the STRATEGIES fallback fires — the defensive
        # `.get(key, neutral)` was masking stale cache entries from older
        # code versions (e.g., renamed strategy keys).
        strat = STRATEGIES.get(strategy_key)
        if strat is None:
            print(f"[algo_strategy] WARN: stale cache for {asset!r}, "
                  f"key={strategy_key!r}; falling back to neutral")
            strat = STRATEGIES["neutral"]
            strategy_key = "neutral"
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
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-08): scan ALL recent changes
    # in the cooldown window (not just the most recent). The old code only
    # checked the most-recent change, so a payout_spike followed by a
    # regime_shift was masked — the cautious cooldown never fired.
    # FIX (F-07-19): the `or [recent]` fallback was unreachable —
    #   `_check_recent_change` always sets `all_changes` to a non-empty list
    #   when `recent` is non-None. Simplified to a plain conditional.
    all_changes = recent["all_changes"] if recent else []
    has_payout_change = any(c["type"] in ("payout_spike", "payout_drop") for c in all_changes)
    if has_payout_change:
        # Find the most-recent payout change for the reason string.
        # FIX (F-07-20): if `has_payout_change` is True but no payout change
        #   is found (impossible given construction, but defensive), log and
        #   synthesize a placeholder — the previous fallback to `recent`
        #   would have used a different change_type's payout values
        #   (e.g., a regime_shift's old_payout/new_payout).
        pc = next((c for c in all_changes
                   if c["type"] in ("payout_spike", "payout_drop")), None)
        if pc is None:
            print(f"[algo_strategy] WARN: has_payout_change=True but no "
                  f"payout change found for {asset!r}")
            pc = {"old_payout": "?", "new_payout": "?", "type": "payout_spike"}
        # FIX (F-07-93): single reason format string (was duplicated with
        #   different wording in the cache entry vs. the return value).
        reason = f"payout {pc['old_payout']}→{pc['new_payout']} ({pc['type']})"
        with _lock:
            _ASSET_STRATEGY[asset] = {
                "strategy": "cautious",
                # FIX (A-04-CRIT-1): cooldown duration = candles × period (sec).
                "until": time.time() + (_COOLDOWN_DURATION * safe_period),
                "cooldown_candles": _COOLDOWN_DURATION,
                "reason": reason,
            }
        strat = STRATEGIES["cautious"]
        return {
            "strategy": "cautious",
            "name": strat["name"],
            "icon": strat["icon"],
            "reason": f"{reason} — algorithm just changed, conservative",
            "multipliers": strat,
            "algorithm": algo,
            "payout": payout,
        }

    # ── Step 2: Check for recent tick density shift → RESET ────────────
    has_tick_shift = any(c["type"] == "tick_density_shift" for c in all_changes)
    if has_tick_shift:
        with _lock:
            _ASSET_STRATEGY[asset] = {
                "strategy": "reset",
                # FIX (A-04-CRIT-1): reset duration = candles × period (sec).
                "until": time.time() + (_RESET_DURATION * safe_period),
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
    # FIX (F-07-21): env-configurable min samples (was hardcoded 15).
    if samples < _MIN_SAMPLES:
        strat = STRATEGIES["unknown"]
        return {
            "strategy": "unknown",
            "name": strat["name"],
            "icon": strat["icon"],
            "reason": f"only {samples} samples — need {_MIN_SAMPLES}+ for strategy",
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

    # FIX (F-07-24): defensive `.get` with neutral fallback (was bare
    # `STRATEGIES[strategy_key]`, which would KeyError on any invalid key).
    strat = STRATEGIES.get(strategy_key, STRATEGIES["neutral"])

    # Cache for this candle (no cooldown, just remember)
    with _lock:
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


def get_strategy_for_blender(asset: str, period: int = 60) -> dict:
    """Convenience method for the blender — returns just the multipliers + reason.

    FIX (DEEP-AUDIT-2026-07-26 / A-04-CRIT-1): added `period` parameter so
    cooldown duration matches the asset's actual candle size.

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
    result = determine_strategy(asset, period=period)
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
    """Return current strategy for one or all assets (for /api/current-strategy).

    FIX (DEEP-AUDIT-2026-07-26 / F-07-23): documented side effect — calling
      this summary function invokes `determine_strategy`, which writes to
      the `_ASSET_STRATEGY` cache (and can set a cooldown). True read-only
      separation would require duplicating the strategy-determination logic;
      deferred as a refactor TODO. With the F-07-18 fix, calls to assets
      already in cooldown no longer rewrite the cache (no-op), so the
      side effect is now limited to non-cooldown assets.
    FIX (DEEP-AUDIT-2026-07-26 / F-07-91): use the module-level `_WINDOWS`
      import (was a per-call local import).
    TODO (F-07-22): when `asset=None`, this still iterates all assets and
      calls `determine_strategy(a)` for each. Each call previously triggered
      a `_check_recent_change` DB query; with the F-07-16 TTL cache this is
      now amortized to one DB query per asset per 5s — acceptable, but a
      true batch query (single SELECT for all assets in the cooldown window)
      would be better. Tracked as a follow-up.
    """
    if asset:
        return determine_strategy(asset)
    # All all-time OTC pairs + any with cached strategy
    assets = list(_WINDOWS.keys())
    # Add cached assets (acquire lock for thread-safe iteration).
    with _lock:
        cached_assets = list(_ASSET_STRATEGY.keys())
    assets.extend([a for a in cached_assets if a not in assets])
    return {a: determine_strategy(a) for a in assets}
