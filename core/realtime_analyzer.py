"""
core/realtime_analyzer.py — TRUE REAL-TIME MILLISECOND ANALYZER (PROD-2026-08-06)

PROBLEM WITH PREVIOUS VERIFIER
==============================
The previous signal_verifier.py used:
  - CLOSED candles (1-minute-old data) for "price action"
  - Tick prices without timestamps (couldn't measure velocity per second)
  - Historical lookup (lookahead bias — comparing to future-graded signals)
  - No awareness of the CURRENT open candle's first ticks

This meant the agent was verifying "what happened 1 minute ago" instead of
"what is happening right now, milliseconds into the new candle".

WHAT THIS ANALYZER DOES DIFFERENTLY
====================================
Uses REAL-TIME tick data with millisecond timestamps:

  1. Pre-candle momentum (last 5 seconds of PREVIOUS candle)
     - Did price accelerate into the close? (strong momentum = continuation)
     - Did price stall at the close? (exhaustion = reversal)

  2. Current-candle opening behavior (first 1-3 seconds of NEW candle)
     - Where did the first tick land vs last close?
     - Immediate direction = market's true intent
     - Gap analysis: big gap up = bullish pressure, big gap down = bearish

  3. Tick arrival rate (broker behavior)
     - Normal: 5-10 ticks/sec from Quotex
     - Slowdown: <2 ticks/sec = broker throttling (suspicious)
     - Burst: >20 ticks/sec in 200ms = news/spike

  4. Micro-volatility (per-second price change)
     - Calm: <0.3 ATR/sec
     - Active: 0.3-1.0 ATR/sec
     - Spike: >1.0 ATR/sec (risk: slippage, broker trap)

  5. Order-flow proxy (tick rule)
     - Uptick = buyers in control
     - Downtick = sellers in control
     - Buy/sell ratio over last 30 ticks

  6. Spread proxy (if bid/ask available — Quotex usually doesn't expose)
     - Skipped if tick has only 'price' field

  7. Real-time HTF alignment
     - Is current 1-min candle aligned with 5-min trend?
     - Counter-trend signals get penalized

INTEGRATION
===========
Called from feed.py AFTER engine.predict() returns, with:
  - The prediction (CALL/PUT)
  - The last 20+ closed candles
  - The full tick deque for the CURRENT (just-opened) candle
  - The full tick deque for the PREVIOUS (just-closed) candle

Returns the same verdict shape as the old verifier (CONFIRM/WEAKEN/VETO/PASS)
so it can be a drop-in replacement when QX_REALTIME_ANALYZER=1.
"""
import os
import time
import math
import threading
from typing import Optional, Dict, Any, List, Tuple
from collections import deque, defaultdict


# ── Configuration ────────────────────────────────────────────────────────────

# Pre-candle momentum window (last N seconds of previous candle)
PRE_CANDLE_WINDOW_SEC = 5.0

# Current-candle opening window (first N seconds of new candle)
OPENING_WINDOW_SEC = 3.0

# Tick arrival rate thresholds
TICK_RATE_NORMAL_MIN = 2.0   # ticks/sec
TICK_RATE_BURST_MAX = 20.0   # ticks/sec
TICK_RATE_WINDOW_SEC = 5.0   # measure over last N seconds

# Micro-volatility thresholds (fraction of ATR per second)
MICRO_VOL_CALM = 0.3
MICRO_VOL_SPIKE = 1.0

# Order-flow window
ORDER_FLOW_TICKS = 30

# Gap analysis (relative to ATR)
GAP_SMALL = 0.10  # < 10% of ATR = small gap
GAP_LARGE = 0.50  # > 50% of ATR = large gap

# Confidence adjustments
CONF_VETO_MULT = 0.0
CONF_WEAKEN_MULT = 0.5
CONF_PASS_MULT = 1.0
CONF_CONFIRM_MULT = 1.1
CONF_STRONG_CONFIRM_MULT = 1.2  # multiple confirmations

# State tracking (visible via /api/verifier/status)
_STATE_LOCK = threading.Lock()
_state = {
    "total_analyzed": 0,
    "total_veto": 0,
    "total_weaken": 0,
    "total_confirm": 0,
    "total_pass": 0,
    "total_killed": 0,
    "total_weakened": 0,
    "started_at": time.time(),
    "recent": [],
}
_RECENT_MAX = 200


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _now_ms() -> float:
    """Current time in milliseconds (float)."""
    return time.time() * 1000.0


def _extract_tick_tuples(ticks: List) -> List[Tuple[float, float]]:
    """Convert tick list to [(timestamp_ms, price), ...] sorted by time.

    Accepts:
      - List of floats (prices only — timestamps will be synthesized)
      - List of dicts with 'time' and 'price'
      - List of tuples (time, price)
    """
    out = []
    if not ticks:
        return out
    # Detect format
    first = ticks[0]
    if isinstance(first, (int, float)):
        # Prices only — synthesize timestamps assuming uniform arrival
        # at 5 ticks/sec (Quotex average). This is a fallback.
        now = _now_ms()
        step_ms = 200.0  # 5 ticks/sec
        for i, p in enumerate(ticks):
            out.append((now - (len(ticks) - i - 1) * step_ms, float(p)))
    elif isinstance(first, dict):
        for t in ticks:
            ts = t.get("time", 0)
            # Quotex ts is in seconds (float). Convert to ms.
            if ts < 1e12:
                ts = ts * 1000.0
            out.append((float(ts), float(t.get("price", 0))))
    elif isinstance(first, (list, tuple)) and len(first) >= 2:
        for t in ticks:
            ts = float(t[0])
            if ts < 1e12:
                ts = ts * 1000.0
            out.append((ts, float(t[1])))
    return out


# ── Real-time analysis layers ───────────────────────────────────────────────

def _layer_pre_candle_momentum(prev_ticks: List[Tuple[float, float]],
                                atr: float, signal: str) -> Dict:
    """Layer 1: Pre-candle momentum (last 5s of previous candle).

    Did price accelerate into the close? Strong momentum → continuation.
    Did price stall? → exhaustion → reversal.
    """
    if not prev_ticks or atr <= 0:
        return {"verdict": "PASS", "reason": "no prev ticks", "data": {}}

    now = prev_ticks[-1][0]
    cutoff = now - PRE_CANDLE_WINDOW_SEC * 1000
    recent = [(t, p) for t, p in prev_ticks if t >= cutoff]
    if len(recent) < 5:
        return {"verdict": "PASS", "reason": f"only {len(recent)} prev ticks in 5s",
                "data": {}}

    # Velocity = (last_price - first_price) / time_seconds / atr
    first_t, first_p = recent[0]
    last_t, last_p = recent[-1]
    dt_sec = (last_t - first_t) / 1000.0
    if dt_sec <= 0:
        return {"verdict": "PASS", "reason": "zero time delta", "data": {}}
    velocity = (last_p - first_p) / dt_sec / atr  # ATR per second
    sig_dir = 1 if signal == "CALL" else -1

    # Acceleration: compare first half velocity to second half
    mid = len(recent) // 2
    if mid > 0:
        first_half = (recent[mid][1] - recent[0][1]) / max(0.001, (recent[mid][0] - recent[0][0]) / 1000.0) / atr
        second_half = (recent[-1][1] - recent[mid][1]) / max(0.001, (recent[-1][0] - recent[mid][0]) / 1000.0) / atr
        accel = second_half - first_half
    else:
        accel = 0.0

    data = {"velocity_atr_per_sec": round(velocity, 3),
            "acceleration": round(accel, 3),
            "ticks_in_window": len(recent)}

    # Signal-aligned strong momentum → CONFIRM
    if velocity * sig_dir > 0.3 and accel * sig_dir > 0:
        return {"verdict": "CONFIRM",
                "reason": f"strong {signal}-aligned momentum into close (v={velocity:.2f} ATR/s, accel={accel:.2f})",
                "data": data}
    # Strong counter-momentum → VETO
    if velocity * sig_dir < -0.3:
        return {"verdict": "VETO",
                "reason": f"strong counter-momentum into close (v={velocity:.2f} ATR/s against {signal})",
                "data": data}
    # Stall / exhaustion → WEAKEN for continuation signals
    if abs(velocity) < 0.05 and abs(accel) < 0.05:
        return {"verdict": "WEAKEN",
                "reason": f"stalled at close (v={velocity:.3f} ATR/s) — exhaustion risk",
                "data": data}
    return {"verdict": "PASS", "reason": f"normal momentum (v={velocity:.2f} ATR/s)",
            "data": data}


def _layer_opening_behavior(curr_ticks: List[Tuple[float, float]],
                             prev_close: float, atr: float,
                             signal: str) -> Dict:
    """Layer 2: Current-candle opening behavior (first 1-3 seconds).

    Where did the first ticks land vs the previous close?
    Immediate direction = market's true intent.
    """
    if not curr_ticks or prev_close <= 0 or atr <= 0:
        return {"verdict": "PASS", "reason": "no opening ticks", "data": {}}

    now = curr_ticks[-1][0]
    cutoff = curr_ticks[0][0] + OPENING_WINDOW_SEC * 1000
    opening = [(t, p) for t, p in curr_ticks if t <= cutoff]
    if len(opening) < 3:
        return {"verdict": "PASS", "reason": f"only {len(opening)} opening ticks",
                "data": {}}

    first_p = opening[0][1]
    last_p = opening[-1][1]
    gap = (first_p - prev_close) / atr  # gap as fraction of ATR
    move = (last_p - first_p) / atr      # subsequent move as fraction of ATR
    sig_dir = 1 if signal == "CALL" else -1

    data = {"gap_atr": round(gap, 3),
            "opening_move_atr": round(move, 3),
            "opening_ticks": len(opening)}

    # Gap aligned with signal + move continues → strong CONFIRM
    if gap * sig_dir > 0.1 and move * sig_dir > 0.05:
        return {"verdict": "CONFIRM",
                "reason": f"gap {gap:+.2f} ATR + continuation {move:+.2f} ATR — aligned with {signal}",
                "data": data}
    # Gap against signal + move continues against → VETO
    if gap * sig_dir < -0.1 and move * sig_dir < -0.05:
        return {"verdict": "VETO",
                "reason": f"gap {gap:+.2f} ATR + move {move:+.2f} ATR — against {signal}",
                "data": data}
    # Big gap but reversal move → WEAKEN (indecisive)
    if abs(gap) > 0.3 and move * gap < 0:
        return {"verdict": "WEAKEN",
                "reason": f"gap {gap:+.2f} ATR but reversed {move:+.2f} ATR — indecisive",
                "data": data}
    return {"verdict": "PASS", "reason": f"normal opening (gap={gap:+.2f}, move={move:+.2f})",
            "data": data}


def _layer_tick_arrival_rate(all_ticks: List[Tuple[float, float]]) -> Dict:
    """Layer 3: Tick arrival rate (broker behavior).

    Detects:
      - Slowdown: <2 ticks/sec (broker throttling, suspicious)
      - Burst: >20 ticks/sec (news/spike, slippage risk)
      - Normal: 5-10 ticks/sec
    """
    if len(all_ticks) < 10:
        return {"verdict": "PASS", "reason": "insufficient ticks", "data": {}}

    now = all_ticks[-1][0]
    cutoff = now - TICK_RATE_WINDOW_SEC * 1000
    recent = [t for t, _ in all_ticks if t >= cutoff]
    if len(recent) < 5:
        return {"verdict": "PASS", "reason": "too few recent ticks", "data": {}}

    rate = len(recent) / TICK_RATE_WINDOW_SEC
    data = {"ticks_per_sec": round(rate, 1), "window_sec": TICK_RATE_WINDOW_SEC}

    if rate < TICK_RATE_NORMAL_MIN:
        return {"verdict": "WEAKEN",
                "reason": f"slow tick rate {rate:.1f}/sec (broker throttling suspected)",
                "data": data}
    if rate > TICK_RATE_BURST_MAX:
        return {"verdict": "WEAKEN",
                "reason": f"tick burst {rate:.1f}/sec (spike/slippage risk)",
                "data": data}
    return {"verdict": "PASS", "reason": f"normal tick rate {rate:.1f}/sec",
            "data": data}


def _layer_micro_volatility(all_ticks: List[Tuple[float, float]],
                              atr: float) -> Dict:
    """Layer 4: Micro-volatility (per-second price change).

    Calm: <0.3 ATR/sec
    Active: 0.3-1.0 ATR/sec
    Spike: >1.0 ATR/sec (risk: slippage, broker trap)
    """
    if len(all_ticks) < 10 or atr <= 0:
        return {"verdict": "PASS", "reason": "insufficient data", "data": {}}

    # Look at last 3 seconds
    now = all_ticks[-1][0]
    cutoff = now - 3000  # 3 sec
    recent = [(t, p) for t, p in all_ticks if t >= cutoff]
    if len(recent) < 5:
        return {"verdict": "PASS", "reason": "too few recent ticks", "data": {}}

    prices = [p for _, p in recent]
    price_range = max(prices) - min(prices)
    vol_per_sec = price_range / 3.0 / atr  # ATR per second
    data = {"micro_vol_atr_per_sec": round(vol_per_sec, 3)}

    if vol_per_sec > MICRO_VOL_SPIKE:
        return {"verdict": "WEAKEN",
                "reason": f"micro-volatility spike {vol_per_sec:.2f} ATR/s (slippage risk)",
                "data": data}
    return {"verdict": "PASS", "reason": f"normal micro-volatility {vol_per_sec:.2f} ATR/s",
            "data": data}


def _layer_order_flow(all_ticks: List[Tuple[float, float]],
                       signal: str) -> Dict:
    """Layer 5: Order-flow proxy (tick rule).

    Uptick = buyers, Downtick = sellers.
    Buy/sell ratio over last 30 ticks.
    """
    if len(all_ticks) < ORDER_FLOW_TICKS:
        return {"verdict": "PASS", "reason": "insufficient ticks", "data": {}}

    recent = all_ticks[-ORDER_FLOW_TICKS:]
    up = 0
    down = 0
    for i in range(1, len(recent)):
        if recent[i][1] > recent[i-1][1]:
            up += 1
        elif recent[i][1] < recent[i-1][1]:
            down += 1
    total = up + down
    if total == 0:
        return {"verdict": "PASS", "reason": "no price changes", "data": {}}

    buy_ratio = up / total
    sig_dir = 1 if signal == "CALL" else -1
    # For CALL: want buy_ratio > 0.55
    # For PUT: want buy_ratio < 0.45
    data = {"buy_ratio": round(buy_ratio, 2), "up_ticks": up, "down_ticks": down}

    if sig_dir == 1 and buy_ratio >= 0.65:
        return {"verdict": "CONFIRM",
                "reason": f"strong buying pressure ({buy_ratio*100:.0f}% upticks) — supports CALL",
                "data": data}
    if sig_dir == -1 and buy_ratio <= 0.35:
        return {"verdict": "CONFIRM",
                "reason": f"strong selling pressure ({(1-buy_ratio)*100:.0f}% downticks) — supports PUT",
                "data": data}
    if sig_dir == 1 and buy_ratio <= 0.35:
        return {"verdict": "VETO",
                "reason": f"sellers dominating ({(1-buy_ratio)*100:.0f}% downticks) — contradicts CALL",
                "data": data}
    if sig_dir == -1 and buy_ratio >= 0.65:
        return {"verdict": "VETO",
                "reason": f"buyers dominating ({buy_ratio*100:.0f}% upticks) — contradicts PUT",
                "data": data}
    return {"verdict": "PASS", "reason": f"balanced order flow ({buy_ratio*100:.0f}% up)",
            "data": data}


def _layer_htf_alignment(candles: List[Dict], signal: str) -> Dict:
    """Layer 6: Higher-timeframe alignment.

    Uses 5-min EMA proxy: average of last 5 1-min closes vs last 20.
    If price is above → HTF uptrend → CALL favored.
    If price is below → HTF downtrend → PUT favored.
    """
    if len(candles) < 20:
        return {"verdict": "PASS", "reason": "insufficient history", "data": {}}

    last5_avg = sum(c["close"] for c in candles[-5:]) / 5
    last20_avg = sum(c["close"] for c in candles[-20:]) / 20
    htf_dir = 1 if last5_avg > last20_avg else -1
    sig_dir = 1 if signal == "CALL" else -1
    sep = (last5_avg - last20_avg) / max(1e-9, last20_avg) * 100  # percent

    data = {"htf_direction": "UP" if htf_dir == 1 else "DOWN",
            "ema_separation_pct": round(sep, 4)}

    if htf_dir == sig_dir and abs(sep) > 0.05:
        return {"verdict": "CONFIRM",
                "reason": f"HTF {'up' if htf_dir==1 else 'down'}trend aligned with {signal} (sep={sep:+.3f}%)",
                "data": data}
    if htf_dir != sig_dir and abs(sep) > 0.10:
        return {"verdict": "WEAKEN",
                "reason": f"HTF {'up' if htf_dir==1 else 'down'}trend opposes {signal} (sep={sep:+.3f}%)",
                "data": data}
    return {"verdict": "PASS", "reason": f"HTF neutral (sep={sep:+.3f}%)",
            "data": data}


# ── Main analysis function ──────────────────────────────────────────────────

def analyze_realtime(prediction: Dict[str, Any],
                     closed_candles: List[Dict],
                     current_candle_ticks: List,
                     previous_candle_ticks: List,
                     asset: str) -> Dict[str, Any]:
    """Run all 6 real-time analysis layers.

    Args:
        prediction: engine prediction dict (signal, confidence, etc.)
        closed_candles: list of closed candle dicts (OHLC) — last 20+
        current_candle_ticks: ticks of the CURRENT (just-opened) candle
                              Can be list of floats, dicts, or tuples
        previous_candle_ticks: ticks of the PREVIOUS (just-closed) candle
        asset: pair name (e.g. "EURUSD_otc")

    Returns:
        Dict with:
          verdict: "CONFIRM" | "WEAKEN" | "VETO" | "PASS"
          confidence_adjustment: float multiplier (1.0, 0.5, 0.0, 1.1, 1.2)
          layers: dict of per-layer results
          reason: human-readable summary
    """
    signal = prediction.get("signal")
    if signal not in ("CALL", "PUT"):
        return {"verdict": "PASS", "confidence_adjustment": 1.0,
                "layers": {}, "reason": "not a CALL/PUT signal"}

    atr = _atr(closed_candles) if closed_candles else 0.0001
    prev_close = closed_candles[-1]["close"] if closed_candles else 0.0

    # Convert ticks to (timestamp_ms, price) tuples
    curr_tuples = _extract_tick_tuples(current_candle_ticks)
    prev_tuples = _extract_tick_tuples(previous_candle_ticks)
    # Combined recent ticks for arrival rate / micro-vol / order flow
    all_recent = (prev_tuples + curr_tuples)[-200:]

    layers = {}
    veto_count = 0
    weaken_count = 0
    confirm_count = 0

    # Run all 6 layers
    layers["L1_pre_candle_momentum"] = _layer_pre_candle_momentum(
        prev_tuples, atr, signal)
    layers["L2_opening_behavior"] = _layer_opening_behavior(
        curr_tuples, prev_close, atr, signal)
    layers["L3_tick_arrival_rate"] = _layer_tick_arrival_rate(all_recent)
    layers["L4_micro_volatility"] = _layer_micro_volatility(all_recent, atr)
    layers["L5_order_flow"] = _layer_order_flow(all_recent, signal)
    layers["L6_htf_alignment"] = _layer_htf_alignment(closed_candles, signal)

    for lname, result in layers.items():
        v = result["verdict"]
        if v == "VETO":
            veto_count += 1
        elif v == "WEAKEN":
            weaken_count += 1
        elif v == "CONFIRM":
            confirm_count += 1

    # Aggregate verdict
    if veto_count >= 2:
        verdict = "VETO"
        conf_mult = CONF_VETO_MULT
    elif veto_count == 1 and confirm_count == 0:
        verdict = "VETO"
        conf_mult = CONF_VETO_MULT
    elif veto_count == 1 and confirm_count >= 1:
        verdict = "WEAKEN"
        conf_mult = CONF_WEAKEN_MULT
    elif weaken_count >= 2:
        verdict = "WEAKEN"
        conf_mult = CONF_WEAKEN_MULT
    elif confirm_count >= 3 and weaken_count == 0 and veto_count == 0:
        verdict = "CONFIRM"
        conf_mult = CONF_STRONG_CONFIRM_MULT  # multiple confirmations = strong
    elif confirm_count >= 2 and weaken_count == 0 and veto_count == 0:
        verdict = "CONFIRM"
        conf_mult = CONF_CONFIRM_MULT
    elif weaken_count == 1 and confirm_count >= 1:
        verdict = "PASS"
        conf_mult = CONF_PASS_MULT
    else:
        verdict = "PASS"
        conf_mult = CONF_PASS_MULT

    # Build reason string
    reason_parts = [f"{lname}: {result['reason']}" for lname, result in layers.items()]
    reason = f"{verdict} (veto={veto_count} weaken={weaken_count} confirm={confirm_count}) | " + " | ".join(reason_parts)

    # Visible console log
    emoji = {"VETO": "🚫", "WEAKEN": "⚠️", "CONFIRM": "✅", "PASS": "➖"}.get(verdict, "?")
    print(f"[realtime] {emoji} {verdict} {asset} {signal} "
          f"veto={veto_count} weaken={weaken_count} confirm={confirm_count} "
          f"| L1={layers['L1_pre_candle_momentum']['verdict'][:1]} "
          f"L2={layers['L2_opening_behavior']['verdict'][:1]} "
          f"L3={layers['L3_tick_arrival_rate']['verdict'][:1]} "
          f"L4={layers['L4_micro_volatility']['verdict'][:1]} "
          f"L5={layers['L5_order_flow']['verdict'][:1]} "
          f"L6={layers['L6_htf_alignment']['verdict'][:1]}")

    return {
        "verdict": verdict,
        "confidence_adjustment": conf_mult,
        "layers": layers,
        "reason": reason,
    }


# ── State tracking (compatible with old verifier's API) ─────────────────────

def _record_verdict(asset: str, signal: str, hour_utc: int, verdict: str,
                    conf_mult: float, layers: Dict, reason: str,
                    original_conf: float, final_conf: float,
                    final_signal: str) -> None:
    """Record a verdict in the in-memory state for UI visibility."""
    with _STATE_LOCK:
        _state["total_analyzed"] += 1
        if verdict == "VETO":
            _state["total_veto"] += 1
            if final_signal == "NEUTRAL":
                _state["total_killed"] += 1
        elif verdict == "WEAKEN":
            _state["total_weaken"] += 1
            if final_conf < original_conf:
                _state["total_weakened"] += 1
        elif verdict == "CONFIRM":
            _state["total_confirm"] += 1
        else:
            _state["total_pass"] += 1

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
        _state["recent"].insert(0, entry)
        if len(_state["recent"]) > _RECENT_MAX:
            _state["recent"] = _state["recent"][:_RECENT_MAX]


def get_verifier_status() -> Dict[str, Any]:
    """Compatible with old verifier's status endpoint."""
    with _STATE_LOCK:
        s = _state
        uptime = time.time() - s["started_at"]
        total = s["total_analyzed"]
        return {
            "enabled": os.environ.get("QX_REALTIME_ANALYZER", "0") == "1",
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
            "analyzer_type": "realtime_v2",
        }


def get_verifier_recent(limit: int = 50) -> List[Dict[str, Any]]:
    """Compatible with old verifier's recent endpoint."""
    with _STATE_LOCK:
        return list(_state["recent"][:limit])


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    if seconds < 86400:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"
