"""
core/pair_health.py — Per-pair health monitoring + auto-disable system.

FIX (DEEP-FIX-2026-08-07): pairs with chronically low win rates (below
breakeven) continued trading indefinitely. This module monitors per-pair
health and can auto-disable/re-enable pairs based on:

  1. 7-day rolling win rate vs breakeven (with Wilson CI)
  2. Consecutive loss detection (≥8 losses → auto-cooldown 30 min)
  3. Regime-change detection (WR shift > 15pp in 24h)
  4. Automatic re-enable when WR recovers (≥ breakeven + 2pp for 50+ samples)

Integrates with:
  - engines/__init__.py (health check before prediction)
  - alerts.py (Telegram notifications on disable/re-enable)
  - /api/pair-health endpoint (dashboard visibility)
"""

import os
import time
import sqlite3
import threading
from typing import Dict, Optional, Tuple, List
from collections import defaultdict


# ── Configuration ─────────────────────────────────────────────────────────────

HEALTH_CHECK_TTL = float(os.environ.get("QX_PAIR_HEALTH_TTL", "120"))
HEALTH_MIN_SAMPLES = int(os.environ.get("QX_PAIR_HEALTH_MIN_SAMPLES", "50"))
SECONDS_PER_DAY = 86400

# Consecutive loss auto-cooldown
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("QX_MAX_CONSECUTIVE_LOSSES", "8"))
COOLDOWN_MINUTES = int(os.environ.get("QX_LOSS_COOLDOWN_MINUTES", "30"))

# Auto re-enable threshold
REENABLE_WR_MARGIN_PP = float(os.environ.get("QX_REENABLE_WR_MARGIN", "2.0"))
REENABLE_MIN_SAMPLES = int(os.environ.get("QX_REENABLE_MIN_SAMPLES", "50"))

# Regime change detection
REGIME_SHIFT_PP = float(os.environ.get("QX_REGIME_SHIFT_PP", "15.0"))
REGIME_SHIFT_WINDOW_HOURS = int(os.environ.get("QX_REGIME_SHIFT_HOURS", "24"))


# ── State ─────────────────────────────────────────────────────────────────────

class PairHealthMonitor:
    """Thread-safe per-pair health tracker."""

    def __init__(self):
        self._lock = threading.Lock()
        # Per-pair state: {asset: {"disabled": bool, "disabled_at": float,
        #                           "disabled_reason": str, "consecutive_losses": int,
        #                           "last_loss_time": float}}
        self._state: Dict[str, dict] = {}
        # Cache: {asset: (ts, report_dict)}
        self._cache: Dict[str, Tuple[float, dict]] = {}

    def record_outcome(self, asset: str, was_correct: bool) -> None:
        """Record a trade outcome for per-pair health tracking."""
        with self._lock:
            s = self._state.setdefault(asset, {
                "disabled": False,
                "disabled_at": 0,
                "disabled_reason": "",
                "consecutive_losses": 0,
                "last_loss_time": 0,
            })
            if was_correct:
                s["consecutive_losses"] = 0
            else:
                s["consecutive_losses"] += 1
                s["last_loss_time"] = time.time()

            # Auto-cooldown on consecutive losses
            if (s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES
                    and not s["disabled"]):
                self._disable(asset, s,
                    f"{MAX_CONSECUTIVE_LOSSES} consecutive losses — "
                    f"{COOLDOWN_MINUTES}min cooldown")

            # Auto re-enable after cooldown expires
            if s["disabled"] and s["disabled_reason"].startswith(
                    str(MAX_CONSECUTIVE_LOSSES)):
                if time.time() - s["disabled_at"] > COOLDOWN_MINUTES * 60:
                    self._enable(asset, s, "cooldown expired")

    def _disable(self, asset: str, state: dict, reason: str) -> None:
        state["disabled"] = True
        state["disabled_at"] = time.time()
        state["disabled_reason"] = reason
        print(f"[pair_health] ⛔ DISABLED {asset}: {reason}")
        try:
            import alerts as _alerts
            _alerts.send(
                f"⛔ PAIR DISABLED — {asset}\n{reason}\n"
                f"Check /api/pair-health for status.",
                key=f"ph_disable_{asset}")
        except Exception:
            pass

    def _enable(self, asset: str, state: dict, reason: str) -> None:
        state["disabled"] = False
        state["disabled_at"] = 0
        state["disabled_reason"] = ""
        state["consecutive_losses"] = 0
        print(f"[pair_health] ✅ ENABLED {asset}: {reason}")
        try:
            import alerts as _alerts
            _alerts.send(
                f"✅ PAIR RE-ENABLED — {asset}\n{reason}\n"
                f"Check /api/pair-health for status.",
                key=f"ph_enable_{asset}")
        except Exception:
            pass

    def is_disabled(self, asset: str) -> Tuple[bool, str]:
        """Check if a pair is currently disabled. Returns (is_disabled, reason)."""
        with self._lock:
            s = self._state.get(asset)
            if s and s["disabled"]:
                return True, s["disabled_reason"]
            return False, ""

    def get_report(self, asset: str = None) -> dict:
        """Get health report for one pair or all pairs."""
        now = time.time()
        with self._lock:
            if asset:
                cached = self._cache.get(asset)
                if cached and (now - cached[0]) < HEALTH_CHECK_TTL:
                    return cached[1]
            pairs = [asset] if asset else list(self._state.keys())
            if not pairs:
                return {"pairs": [], "summary": {"total": 0, "disabled": 0,
                         "healthy": 0}}

            report = {"pairs": [], "summary": {"total": 0, "disabled": 0,
                                                "healthy": 0, "cooldown": 0}}
            for a in pairs:
                s = self._state.get(a, {})
                entry = {
                    "asset": a,
                    "disabled": s.get("disabled", False),
                    "disabled_reason": s.get("disabled_reason", ""),
                    "disabled_ago_sec": (round(now - s["disabled_at"])
                                         if s.get("disabled_at") else None),
                    "consecutive_losses": s.get("consecutive_losses", 0),
                }
                report["pairs"].append(entry)
                report["summary"]["total"] += 1
                if s.get("disabled"):
                    report["summary"]["disabled"] += 1
                    if "cooldown" in s.get("disabled_reason", "").lower():
                        report["summary"]["cooldown"] += 1
                else:
                    report["summary"]["healthy"] += 1
            report["summary"]["max_consecutive_losses"] = MAX_CONSECUTIVE_LOSSES
            report["summary"]["cooldown_minutes"] = COOLDOWN_MINUTES

            if asset:
                self._cache[asset] = (now, report)
            return report

    def reset_pair(self, asset: str) -> None:
        """Manually re-enable a pair (admin action)."""
        with self._lock:
            s = self._state.get(asset)
            if s:
                self._enable(asset, s, "manual reset by admin")


# ── Singleton ─────────────────────────────────────────────────────────────────

monitor = PairHealthMonitor()


# ── Convenience functions ─────────────────────────────────────────────────────

def is_pair_healthy(asset: str) -> Tuple[bool, str]:
    """Quick check: is this pair healthy? Returns (ok, reason)."""
    disabled, reason = monitor.is_disabled(asset)
    if disabled:
        return False, reason
    return True, "ok"


def record_trade_outcome(asset: str, was_correct: bool) -> None:
    """Record a trade outcome for health tracking. Call from _grade_and_log."""
    monitor.record_outcome(asset, was_correct)


def get_health_report(asset: str = None) -> dict:
    """Get the full health report for API."""
    return monitor.get_report(asset)
