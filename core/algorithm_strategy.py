"""core/algorithm_strategy.py — Algorithm-aware prediction strategy."""
from __future__ import annotations
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "signals.db"))

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

_ASSET_STRATEGY: dict[str, dict] = {}  # asset → {strategy, reason, until, cooldown_candles}
_COOLDOWN_DURATION = int(os.environ.get("STRATEGY_COOLDOWN_CANDLES", "5"))
_RESET_DURATION = int(os.environ.get("STRATEGY_RESET_CANDLES", "3"))
_MIN_SAMPLES = int(os.environ.get("STRATEGY_MIN_SAMPLES", "30"))
_RECENT_CHANGE_TTL = float(os.environ.get("STRATEGY_RECENT_CHANGE_TTL", "5"))
_RECENT_CHANGE_CACHE: dict[str, tuple[float, "dict | None"]] = {}
_lock = threading.Lock()

def _get_algo_state(asset: str, period: int = 60) -> dict:
    """Query the algorithm monitor's current state for an asset."""
    try:
        window = _WINDOWS.get(asset)
        if not window or len(window) < _MIN_SAMPLES:
            return {"algorithm": "unknown", "samples": 0}
        algo = _LAST_ALGO_GUESS.get(asset, "unknown")
        payout = _LAST_PAYOUT.get(asset, 0)
        tick_density = _LAST_TICK_DENSITY.get(asset, 0)
        recent_change = _check_recent_change(asset, period=period)
        return {
            "algorithm": algo,
            "samples": len(window),
            "payout": payout,
            "tick_density": tick_density,
            "recent_change": recent_change,
        }
    except Exception as e:
        print(f"[algo_strategy] _get_algo_state error for {asset!r}: {e!r}")
        return {"algorithm": "unknown", "samples": 0}
def _check_recent_change(asset: str, period: int = 60) -> dict | None:
    """Check if there was a recent algorithm change (within last cooldown window)."""
    now = time.time()
    cached_entry = _RECENT_CHANGE_CACHE.get(asset)
    if cached_entry is not None and (now - cached_entry[0]) < _RECENT_CHANGE_TTL:
        return cached_entry[1]
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        safe_period = max(1, int(period))
        cutoff = time.time() - (_COOLDOWN_DURATION * safe_period)
        rows = cur.execute("""SELECT * FROM algorithm_changes
                             WHERE asset=? AND ts >= ?
                             ORDER BY ts DESC""",
                          (asset, cutoff)).fetchall()
        if not rows:
            _RECENT_CHANGE_CACHE[asset] = (now, None)
            return None
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
        print(f"[algo_strategy] _check_recent_change error for {asset!r}: {e!r}")
        return None
    finally:
        conn.close()
def determine_strategy(asset: str, period: int = 60) -> dict:
    """Determine the current trading strategy for an asset."""
    state = _get_algo_state(asset, period=period)
    algo = state.get("algorithm", "unknown")
    samples = state.get("samples", 0)
    payout = state.get("payout", 0)
    recent = state.get("recent_change")
    safe_period = max(1, int(period))
    with _lock:
        cached = _ASSET_STRATEGY.get(asset, {})
    cached_until = cached.get("until", 0)
    if cached_until > time.time():
        remaining_sec = max(0, cached_until - time.time())
        remaining_candles = max(0, int(round(remaining_sec / float(safe_period))))
        strategy_key = cached.get("strategy", "neutral")
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
    all_changes = recent["all_changes"] if recent else []
    has_payout_change = any(c["type"] in ("payout_spike", "payout_drop") for c in all_changes)
    if has_payout_change:
        pc = next((c for c in all_changes
                   if c["type"] in ("payout_spike", "payout_drop")), None)
        if pc is None:
            print(f"[algo_strategy] WARN: has_payout_change=True but no "
                  f"payout change found for {asset!r}")
            pc = {"old_payout": "?", "new_payout": "?", "type": "payout_spike"}
        reason = f"payout {pc['old_payout']}→{pc['new_payout']} ({pc['type']})"
        with _lock:
            _ASSET_STRATEGY[asset] = {
                "strategy": "cautious",
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
    has_tick_shift = any(c["type"] == "tick_density_shift" for c in all_changes)
    if has_tick_shift:
        with _lock:
            _ASSET_STRATEGY[asset] = {
                "strategy": "reset",
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
    strat = STRATEGIES.get(strategy_key, STRATEGIES["neutral"])
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
    """Convenience method for the blender — returns just the multipliers + reason."""
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
    """Return current strategy for one or all assets (for /api/current-strategy)."""
    if asset:
        return determine_strategy(asset)
    assets = list(_WINDOWS.keys())
    with _lock:
        cached_assets = list(_ASSET_STRATEGY.keys())
    assets.extend([a for a in cached_assets if a not in assets])
    return {a: determine_strategy(a) for a in assets}
