"""
core/signal_verifier.py — REAL-TIME SIGNAL VERIFICATION AGENT (PROD-2026-08-06)

PURPOSE
=======
The existing engine produces CALL/PUT predictions based on co-occurrence
statistics (module votes). But "this module's CALL won 55% historically"
does NOT mean THIS specific signal will win — it's a probability, not a
guarantee, and the probability is barely above coin-flip.

This agent sits BETWEEN the engine's prediction and the broadcast to UI.
It performs REAL-TIME, multi-layer analysis on the actual candle + tick
data that produced the signal, then issues one of three verdicts:

  CONFIRM  — signal is strong, broadcast as-is
  WEAKEN   — signal is questionable, broadcast with reduced confidence
  VETO     — signal disagrees with current price action, downgrade to NEUTRAL

This is NOT statistics — it's candle-by-candle verification.

LAYERS
======
Layer 1: Price Action Confluence
  - Compare last 3 candle bodies vs signal direction
  - If signal=CALL but last 2 candles are bearish → VETO (counter-trend)
  - If signal=CALL and last 3 candles bullish → CONFIRM
  - Body sizes must be > ATR*0.15 (significant candles only)

Layer 2: Wick / Rejection Analysis
  - Long upper wick + signal=CALL → WEAKEN (rejection from above)
  - Long lower wick + signal=PUT  → WEAKEN (rejection from below)
  - Wick ≥ 60% of candle range = significant rejection

Layer 3: Tick Momentum (last 30 ticks)
  - Tick slope must align with signal direction
  - If CALL but ticks decelerating downward → VETO
  - If PUT but ticks accelerating upward → VETO
  - Tick velocity = (last_5_avg - first_5_avg) / total_range

Layer 4: Key Level Confluence
  - Is the current price near a recent swing high/low?
  - If CALL near swing_high resistance → WEAKEN
  - If PUT near swing_low support → WEAKEN

Layer 5: Historical Failure Pattern
  - Lookup past N signals with similar setup (same asset, direction, hour)
  - If historical win rate for THIS specific combo < 50% → WEAKEN

USAGE
=====
    from core.signal_verifier import verify_signal
    verified = await verify_signal(prediction, candles, ticks, asset)

    if verified['verdict'] == 'VETO':
        # Don't broadcast — convert to NEUTRAL
        prediction['signal'] = 'NEUTRAL'
        prediction['strength'] = 'NEUTRAL'
        prediction['confidence'] = 0
    elif verified['verdict'] == 'WEAKEN':
        prediction['confidence'] = int(prediction.get('confidence', 0) * 0.6)
    # CONFIRM: leave prediction unchanged

INTEGRATION
===========
Called from feed.py::_run_eoc() right after engine.predict() returns,
before stream.prediction is set. Failures are caught and non-fatal —
on any error, the original prediction passes through unchanged.
"""
import os
import math
import sqlite3
import time
import threading
from typing import Optional, Dict, Any, List, Tuple
from collections import defaultdict


# ── Configuration ────────────────────────────────────────────────────────────

# Layer 1: Price Action
MIN_CANDLE_BODY_ATR_RATIO = 0.15  # candle body must be ≥15% of ATR to count
TREND_AGREE_MIN_CANDLES   = 2     # min candles in signal direction to CONFIRM
TREND_DISAGREE_MIN_CANDLES = 2    # min opposite candles to VETO

# Layer 2: Wick rejection
WICK_REJECTION_THRESHOLD = 0.60   # wick ≥60% of range = significant

# Layer 3: Tick momentum
MIN_TICKS_FOR_MOMENTUM = 20
TICK_MOMENTUM_LOOKBACK = 30
TICK_VELOCITY_VETO_THRESHOLD = -0.2  # negative = against signal

# Layer 4: Key level proximity
KEY_LEVEL_PROXIMITY_ATR = 0.25  # within 25% of ATR = "near" a level
MIN_SWING_LOOKBACK = 10          # candles to look back for swing detection

# Layer 5: Historical lookup
HISTORICAL_LOOKUP_MIN_SAMPLES = 20
HISTORICAL_LOOKUP_HOURS = 168    # 7 days
HISTORICAL_FAIL_THRESHOLD = 0.50  # <50% historical = WEAKEN

# Cache for Layer 5 (DB lookups are expensive)
_HIST_CACHE: Dict[Tuple, Dict] = {}
_HIST_CACHE_TS = 0
_HIST_CACHE_TTL = 300  # 5 minutes
_HIST_LOCK = threading.Lock()


# ── State tracking (for visibility endpoints) ───────────────────────────────
# In-memory store of last N verdicts + cumulative counters.
# Exposed via /api/verifier/status and /api/verifier/recent for UI visibility.

_VERIFIER_STATE_LOCK = threading.Lock()
_VERIFIER_RECENT_MAX = 200  # keep last 200 verdicts in memory
_verifier_state = {
    "total_verified": 0,
    "total_veto": 0,
    "total_weaken": 0,
    "total_confirm": 0,
    "total_pass": 0,
    "total_killed": 0,   # signals converted to NEUTRAL by VETO
    "total_weakened": 0,  # signals that had confidence reduced
    "started_at": time.time(),
    "recent": [],        # list of recent verdict dicts (newest first)
}


def _record_verdict(asset: str, signal: str, hour_utc: int, verdict: str,
                    conf_mult: float, layers: Dict, reason: str,
                    original_conf: float, final_conf: float,
                    final_signal: str) -> None:
    """Record a verdict in the in-memory state for UI visibility."""
    with _VERIFIER_STATE_LOCK:
        _verifier_state["total_verified"] += 1
        if verdict == "VETO":
            _verifier_state["total_veto"] += 1
            if final_signal == "NEUTRAL":
                _verifier_state["total_killed"] += 1
        elif verdict == "WEAKEN":
            _verifier_state["total_weaken"] += 1
            if final_conf < original_conf:
                _verifier_state["total_weakened"] += 1
        elif verdict == "CONFIRM":
            _verifier_state["total_confirm"] += 1
        else:
            _verifier_state["total_pass"] += 1

        entry = {
            "ts": time.time(),
            "asset": asset,
            "signal": signal,
            "hour_utc": hour_utc,
            "verdict": verdict,
            "conf_mult": round(conf_mult, 2),
            "original_conf": original_conf,
            "final_conf": final_conf,
            "final_signal": final_signal,
            "killed": (verdict == "VETO" and final_signal == "NEUTRAL"),
            "layers": {k: v["verdict"] for k, v in layers.items()},
            "reason": reason[:300],
        }
        _verifier_state["recent"].insert(0, entry)
        if len(_verifier_state["recent"]) > _VERIFIER_RECENT_MAX:
            _verifier_state["recent"] = _verifier_state["recent"][:_VERIFIER_RECENT_MAX]


def get_verifier_status() -> Dict[str, Any]:
    """Return current verifier state for /api/verifier/status endpoint."""
    with _VERIFIER_STATE_LOCK:
        s = _verifier_state
        uptime = time.time() - s["started_at"]
        total = s["total_verified"]
        return {
            "enabled": os.environ.get("QX_SIGNAL_VERIFIER", "0") == "1",
            "uptime_seconds": round(uptime, 0),
            "uptime_human": _human_duration(uptime),
            "total_verified": total,
            "total_veto": s["total_veto"],
            "total_weaken": s["total_weaken"],
            "total_confirm": s["total_confirm"],
            "total_pass": s["total_pass"],
            "total_killed": s["total_killed"],
            "total_weakened": s["total_weakened"],
            "veto_rate": round(100 * s["total_veto"] / total, 1) if total > 0 else 0,
            "weaken_rate": round(100 * s["total_weaken"] / total, 1) if total > 0 else 0,
            "confirm_rate": round(100 * s["total_confirm"] / total, 1) if total > 0 else 0,
            "pass_rate": round(100 * s["total_pass"] / total, 1) if total > 0 else 0,
            "recent_count": len(s["recent"]),
        }


def get_verifier_recent(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent verdicts for /api/verifier/recent endpoint."""
    with _VERIFIER_STATE_LOCK:
        return list(_verifier_state["recent"][:limit])


def _human_duration(seconds: float) -> str:
    """Convert seconds to human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    if seconds < 86400:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"





# ── Helper functions ─────────────────────────────────────────────────────────

def _body(c: Dict) -> float:
    return c.get("close", 0) - c.get("open", 0)


def _abs_body(c: Dict) -> float:
    return abs(_body(c))


def _range(c: Dict) -> float:
    return max(c.get("high", 0) - c.get("low", 0), 1e-9)


def _atr(candles: List[Dict], n: int = 20) -> float:
    if len(candles) < 2:
        return 0.0001
    recent = candles[-n:] if len(candles) >= n else candles
    trs = []
    for i in range(1, len(recent)):
        c, prev = recent[i], recent[i-1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0001


def _upper_wick_pct(c: Dict) -> float:
    r = _range(c)
    if r <= 0: return 0.0
    return (c["high"] - max(c["open"], c["close"])) / r * 100


def _lower_wick_pct(c: Dict) -> float:
    r = _range(c)
    if r <= 0: return 0.0
    return (min(c["open"], c["close"]) - c["low"]) / r * 100


def _swing_highs_lows(candles: List[Dict], lookback: int = 10) -> Tuple[List[float], List[float]]:
    """Find recent swing highs and lows."""
    if len(candles) < 5:
        return [], []
    recent = candles[-lookback:]
    highs, lows = [], []
    for i in range(2, len(recent) - 2):
        # swing high: this candle's high > 2 neighbors on each side
        if (recent[i]["high"] > recent[i-1]["high"] and
            recent[i]["high"] > recent[i-2]["high"] and
            recent[i]["high"] > recent[i+1]["high"] and
            recent[i]["high"] > recent[i+2]["high"]):
            highs.append(recent[i]["high"])
        if (recent[i]["low"] < recent[i-1]["low"] and
            recent[i]["low"] < recent[i-2]["low"] and
            recent[i]["low"] < recent[i+1]["low"] and
            recent[i]["low"] < recent[i+2]["low"]):
            lows.append(recent[i]["low"])
    return highs, lows


def _tick_velocity(ticks: List[float], lookback: int = 30) -> Optional[float]:
    """Compute tick velocity over last N ticks.

    Returns a value in [-1, 1]:
      Positive = upward momentum
      Negative = downward momentum
      Zero = flat
    """
    if not ticks or len(ticks) < MIN_TICKS_FOR_MOMENTUM:
        return None
    recent = ticks[-lookback:] if len(ticks) >= lookback else ticks
    if len(recent) < 10:
        return None
    first_5 = sum(recent[:5]) / 5
    last_5 = sum(recent[-5:]) / 5
    total_range = max(recent) - min(recent)
    if total_range <= 0:
        return 0.0
    return (last_5 - first_5) / total_range


# ── Layer implementations ───────────────────────────────────────────────────

def _layer1_price_action(signal: str, candles: List[Dict], atr: float) -> Dict:
    """Layer 1: price action confluence check."""
    if len(candles) < 4 or atr <= 0:
        return {"verdict": "PASS", "reason": "insufficient data"}

    # Look at last 3 closed candles
    last3 = candles[-3:]
    sig_dir = 1 if signal == "CALL" else -1

    agree = 0
    disagree = 0
    for c in last3:
        body = _body(c)
        if abs(body) < atr * MIN_CANDLE_BODY_ATR_RATIO:
            continue  # too small to count
        if (body > 0 and sig_dir == 1) or (body < 0 and sig_dir == -1):
            agree += 1
        else:
            disagree += 1

    if disagree >= TREND_DISAGREE_MIN_CANDLES and agree == 0:
        return {"verdict": "VETO",
                "reason": f"last 3 candles strongly counter-trend ({disagree} against, 0 agree)"}
    if agree >= TREND_AGREE_MIN_CANDLES:
        return {"verdict": "CONFIRM",
                "reason": f"last 3 candles trend-aligned ({agree} agree)"}
    return {"verdict": "PASS", "reason": f"mixed: {agree} agree, {disagree} against"}


def _layer2_wick_rejection(signal: str, candles: List[Dict]) -> Dict:
    """Layer 2: wick rejection analysis."""
    if not candles:
        return {"verdict": "PASS", "reason": "no candles"}
    last = candles[-1]
    uw = _upper_wick_pct(last)
    lw = _lower_wick_pct(last)

    # CALL but big upper wick = rejection from above
    if signal == "CALL" and uw >= WICK_REJECTION_THRESHOLD * 100:
        return {"verdict": "WEAKEN",
                "reason": f"upper wick {uw:.0f}% — rejection from above"}
    # PUT but big lower wick = rejection from below
    if signal == "PUT" and lw >= WICK_REJECTION_THRESHOLD * 100:
        return {"verdict": "WEAKEN",
                "reason": f"lower wick {lw:.0f}% — rejection from below"}
    return {"verdict": "PASS", "reason": f"uw={uw:.0f}% lw={lw:.0f}%"}


def _layer3_tick_momentum(signal: str, ticks: List[float]) -> Dict:
    """Layer 3: tick-level momentum analysis."""
    vel = _tick_velocity(ticks, TICK_MOMENTUM_LOOKBACK)
    if vel is None:
        return {"verdict": "PASS", "reason": "insufficient ticks"}

    # CALL needs positive tick velocity
    if signal == "CALL" and vel < TICK_VELOCITY_VETO_THRESHOLD:
        return {"verdict": "VETO",
                "reason": f"ticks decelerating down (vel={vel:.2f})"}
    # PUT needs negative tick velocity
    if signal == "PUT" and vel > -TICK_VELOCITY_VETO_THRESHOLD:
        return {"verdict": "VETO",
                "reason": f"ticks accelerating up (vel={vel:.2f})"}

    if (signal == "CALL" and vel > 0.2) or (signal == "PUT" and vel < -0.2):
        return {"verdict": "CONFIRM",
                "reason": f"tick momentum aligned (vel={vel:.2f})"}
    return {"verdict": "PASS", "reason": f"neutral momentum (vel={vel:.2f})"}


def _layer4_key_level(signal: str, candles: List[Dict], atr: float) -> Dict:
    """Layer 4: key level confluence check."""
    if len(candles) < MIN_SWING_LOOKBACK or atr <= 0:
        return {"verdict": "PASS", "reason": "insufficient history"}

    highs, lows = _swing_highs_lows(candles, MIN_SWING_LOOKBACK)
    if not highs and not lows:
        return {"verdict": "PASS", "reason": "no swing levels"}

    last_close = candles[-1]["close"]
    proximity = atr * KEY_LEVEL_PROXIMITY_ATR

    # CALL near swing high = resistance, weaken
    if signal == "CALL":
        for h in highs:
            if abs(last_close - h) < proximity and last_close <= h:
                return {"verdict": "WEAKEN",
                        "reason": f"near swing-high resistance {h:.5f}"}
    # PUT near swing low = support, weaken
    if signal == "PUT":
        for l in lows:
            if abs(last_close - l) < proximity and last_close >= l:
                return {"verdict": "WEAKEN",
                        "reason": f"near swing-low support {l:.5f}"}
    return {"verdict": "PASS", "reason": "no nearby key level"}


def _layer5_historical_pattern(signal: str, asset: str, hour: int) -> Dict:
    """Layer 5: historical failure pattern lookup from DB."""
    global _HIST_CACHE, _HIST_CACHE_TS

    now = time.time()
    with _HIST_LOCK:
        if now - _HIST_CACHE_TS > _HIST_CACHE_TTL:
            _HIST_CACHE = {}
            _HIST_CACHE_TS = now

    cache_key = (asset, signal, hour)
    if cache_key in _HIST_CACHE:
        cached = _HIST_CACHE[cache_key]
        return {"verdict": cached['verdict'], "reason": cached['reason']}

    # Try DB lookup
    try:
        db_path = os.environ.get("QX_DB_PATH", "/app/data/signals.db")
        if not os.path.exists(db_path):
            return {"verdict": "PASS", "reason": "no DB"}

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # Last 7 days, same asset, same signal direction, same UTC hour
        cutoff = now - HISTORICAL_LOOKUP_HOURS * 3600
        cur.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS c
            FROM signal_log
            WHERE asset = ? AND signal = ?
              AND accuracy IN ('correct','wrong')
              AND ts >= ?
              AND CAST(strftime('%H', datetime(ctime, 'unixepoch')) AS INT) = ?
        """, (asset, signal, cutoff, hour))
        r = cur.fetchone()
        conn.close()

        n, c = r[0], r[1] or 0
        if n < HISTORICAL_LOOKUP_MIN_SAMPLES:
            verdict = "PASS"
            reason = f"only {n} historical samples"
        else:
            win_rate = c / n
            if win_rate < HISTORICAL_FAIL_THRESHOLD:
                verdict = "WEAKEN"
                reason = f"historical {win_rate*100:.1f}% win (n={n})"
            elif win_rate >= 0.60:
                verdict = "CONFIRM"
                reason = f"historical {win_rate*100:.1f}% win (n={n})"
            else:
                verdict = "PASS"
                reason = f"historical {win_rate*100:.1f}% win (n={n})"

        _HIST_CACHE[cache_key] = {"verdict": verdict, "reason": reason}
        return {"verdict": verdict, "reason": reason}
    except Exception as e:
        return {"verdict": "PASS", "reason": f"DB lookup failed: {e}"}


# ── Main verification function ──────────────────────────────────────────────

def verify_signal(prediction: Dict[str, Any],
                  candles: List[Dict],
                  ticks: List[float],
                  asset: str,
                  hour_utc: int) -> Dict[str, Any]:
    """Run all 5 verification layers on a prediction.

    Args:
        prediction: engine prediction dict (signal, confidence, strength, etc.)
        candles: list of closed candle dicts (last 20+ recommended)
        ticks: list of tick prices for current candle
        asset: pair name (e.g. "EURUSD_otc")
        hour_utc: current UTC hour (0-23)

    Returns:
        Dict with:
          verdict: "CONFIRM" | "WEAKEN" | "VETO" | "PASS"
          confidence_adjustment: float multiplier (1.0, 0.6, 0.0)
          layers: dict of per-layer results
          reason: human-readable summary
    """
    signal = prediction.get("signal")
    if signal not in ("CALL", "PUT"):
        return {"verdict": "PASS", "confidence_adjustment": 1.0,
                "layers": {}, "reason": "not a CALL/PUT signal"}

    atr = _atr(candles) if candles else 0.0001
    layers = {}
    veto_count = 0
    weaken_count = 0
    confirm_count = 0

    # Run all 5 layers
    layers["L1_price_action"] = _layer1_price_action(signal, candles, atr)
    layers["L2_wick_rejection"] = _layer2_wick_rejection(signal, candles)
    layers["L3_tick_momentum"] = _layer3_tick_momentum(signal, ticks or [])
    layers["L4_key_level"] = _layer4_key_level(signal, candles, atr)
    layers["L5_historical"] = _layer5_historical_pattern(signal, asset, hour_utc)

    for lname, result in layers.items():
        v = result["verdict"]
        if v == "VETO":
            veto_count += 1
        elif v == "WEAKEN":
            weaken_count += 1
        elif v == "CONFIRM":
            confirm_count += 1

    # Aggregate verdict logic:
    # - 2+ VETO → VETO (multiple layers strongly disagree)
    # - 1 VETO + 0 CONFIRM → VETO
    # - 1 VETO + 1+ CONFIRM → WEAKEN (conflict)
    # - 0 VETO, 2+ WEAKEN → WEAKEN
    # - 0 VETO, 1 WEAKEN, 1+ CONFIRM → PASS (slight concern but confirmed)
    # - 0 VETO, 0 WEAKEN, 2+ CONFIRM → CONFIRM
    # - else → PASS
    if veto_count >= 2:
        verdict = "VETO"
        conf_mult = 0.0
    elif veto_count == 1 and confirm_count == 0:
        verdict = "VETO"
        conf_mult = 0.0
    elif veto_count == 1 and confirm_count >= 1:
        verdict = "WEAKEN"
        conf_mult = 0.5
    elif weaken_count >= 2:
        verdict = "WEAKEN"
        conf_mult = 0.5
    elif weaken_count == 1 and confirm_count >= 1:
        verdict = "PASS"
        conf_mult = 0.8
    elif confirm_count >= 2 and weaken_count == 0 and veto_count == 0:
        verdict = "CONFIRM"
        conf_mult = 1.1  # slight boost
    else:
        verdict = "PASS"
        conf_mult = 1.0

    # Build reason string
    reason_parts = [f"{lname}: {result['reason']}" for lname, result in layers.items()]
    reason = f"{verdict} (veto={veto_count} weaken={weaken_count} confirm={confirm_count}) | " + " | ".join(reason_parts)

    # Visible console log so Railway logs show every verdict in real time
    _verdict_emoji = {"VETO": "🚫", "WEAKEN": "⚠️", "CONFIRM": "✅", "PASS": "➖"}.get(verdict, "?")
    print(f"[verifier] {_verdict_emoji} {verdict} {asset} {signal} h={hour_utc} "
          f"veto={veto_count} weaken={weaken_count} confirm={confirm_count} "
          f"| L1={layers['L1_price_action']['verdict'][:1]} "
          f"L2={layers['L2_wick_rejection']['verdict'][:1]} "
          f"L3={layers['L3_tick_momentum']['verdict'][:1]} "
          f"L4={layers['L4_key_level']['verdict'][:1]} "
          f"L5={layers['L5_historical']['verdict'][:1]}")

    return {
        "verdict": verdict,
        "confidence_adjustment": conf_mult,
        "layers": layers,
        "reason": reason,
    }


def refresh_historical_cache():
    """Force a refresh of the historical pattern cache."""
    global _HIST_CACHE_TS
    with _HIST_LOCK:
        _HIST_CACHE_TS = 0
