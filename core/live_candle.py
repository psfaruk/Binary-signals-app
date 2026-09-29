"""
core/live_candle.py — LIVE RUNNING-CANDLE PREDICTION (LIVE-CANDLE 2026-09-29).

USER REQUIREMENT (verbatim):
  "তারা রানিং ক্যান্ডেল টি দেখে বুঝতে পারে, যে এই রানিং ক্যান্ডেল টি কোন দিকে
   যাবে, তারা এটা ধরতে পারে, যে একটি ক্যান্ডেল এ বায়ার আছে নাকি সেলার আছে…
   এমন টা কি আমার অ্যাপ এ এপ্লাই করা যাবে, যে একটি ক্যান্ডেল কি ঘটছে, মিলি
   সেকেন্ড এ আপডেট হবে। আমি চাই আমার প্রেডিকশন ক্যান্ডেল টি কোন দিকে যাবে।"

WHAT THIS ENGINE ANSWERS — on EVERY tick (millisecond-fresh, ~200µs):

  1. ক্যান্ডেল কোথায় ক্লোজ হবে?  → p_close_green  = P(close > open)
     The direction the RUNNING candle is heading (vs its own open).
  2. এখন ঢুকলে জেতার সম্ভাবনা?  → p_up_from_here = P(final > current)
     The TRADEABLE probability: a binary entered NOW settles strike→expiry,
     strike ≈ current price. This is the honest number a trader needs.
  3. বায়ার নাকি সেলার?         → unified buyer/seller pressure
     ONE definition everywhere (fixes the 3-definitions drift, audit C4):
     50% tick-count flow + 50% volume-delta flow of the whole running
     candle, graded into BALANCED / BUYERS / SELLERS / DOMINANT_*.

HOW (leak-free by construction — every input is the tick sequence up to
"now", exactly like a trader watching the candle form):

  σ-model  — expected REMAINING move of the candle:
             σ_remaining = σ_full × sqrt(τ)   (Brownian time-scaling)
             σ_full blends the median range of recent CLOSED candles with
             the running candle's own range scaled by elapsed fraction.
  momentum — last-segment tick velocity extrapolated over seconds_left
             × PERSISTENCE (calibrated walk-forward in the backtest; the
             honest coefficient is small — momentum mostly decays).
  reversion — when the candle is deeply extended with a rejected wick,
             a pull-to-body term fights blind extrapolation.
  anatomy  — qualitative factors from core.tick_eye (velocity, close
             position, late-flip + spike-noise detection, tick burst,
             wick rejection) reused verbatim — one eye, one math layer.

DESIGN CONSTRAINTS (repo lessons, see core/constants.py):
  * PURE function, zero I/O, O(n) over ≤ 400 ticks — safe to run on every
    tick of every stream (same budget contract as tick_eye/coordination).
  * NO look-ahead: never touches future data; `now` is a parameter.
  * Honest abstention: below MIN_TICKS the engine says "not ready".
  * Honest probabilities: clamped to [P_FLOOR, 1-P_FLOOR]; when almost no
    time is left the candle is mechanically "locked" and the probability
    converges to 0/1 — the engine reports lock_fraction so the UI can
    separate "locked-in" from "genuinely predicted".
  * The published EOC signal is NOT overwritten (CONFLUENCE-V1 contract).
    This engine is a first-class LIVE output, broadcast per tick and
    graded at candle close (rolling accuracy exposed via API + panel).

Public API:
  predict_running_candle(ticks, open_price, period, candle_open_time,
                         now, recent_candles=None) -> dict
  pressure_from_anatomy(ticks) -> (buyer_pct, seller_pct, state)
"""

from __future__ import annotations

import math
import os
import time

try:
    from core.tick_eye import analyze_candle_ticks as _anatomy_of
except Exception:                                    # pragma: no cover
    _anatomy_of = None

__all__ = [
    "predict_running_candle",
    "pressure_from_anatomy",
    "PERSISTENCE",
]

# ── Tunables (env-overridable, repo convention) ─────────────────────────────
# Momentum persistence: fraction of the estimated drift rate that is
# assumed to continue over the remaining seconds. The backtest
# (scripts/backtest_live_candle.py) fits this walk-forward; the default is
# deliberately small — OTC momentum decays fast and over-extrapolation was
# one of the audit's "wrong prediction" causes.
PERSISTENCE = float(os.environ.get("QX_LIVE_CANDLE_PERSISTENCE", "0.25"))
# Drift-estimator blend: weight of the STABLE whole-candle drift rate
# (net/elapsed — the regime estimate) vs the REACTIVE last-segment
# velocity (catches late control-transfer, noisier).
# BT-2026-09-29 finding: segment-only velocity bets against trends and
# measured 31% WR; the blend keeps trend continuation AND reaction.
DRIFT_STABLE_W = float(os.environ.get("QX_LIVE_CANDLE_DRIFT_STABLE_W", "0.55"))
# Minimum ticks before the engine speaks.
MIN_TICKS = int(os.environ.get("QX_LIVE_CANDLE_MIN_TICKS", "8"))
# Probability clamp — the engine never claims certainty while time remains.
P_FLOOR = float(os.environ.get("QX_LIVE_CANDLE_P_FLOOR", "0.03"))
# How many recent closed candles feed the σ estimate.
SIGMA_LOOKBACK = int(os.environ.get("QX_LIVE_CANDLE_SIGMA_LOOKBACK", "20"))
# Confidence never exceeds this (repo lesson: inflated confidence is worse
# than none).
MAX_CONFIDENCE = 95
# Max ticks scanned per call (bounded, same contract as tick_eye).
MAX_SCAN_TICKS = 400

# ── Unified buyer/seller pressure bands (ONE definition — fixes audit C4) ──
PRESSURE_DOMINANT = 72      # ≥72% → DOMINANT_*
PRESSURE_LEAN = 60          # ≥60% → lean BUYERS/SELLERS

# Phase bands (same convention as tick_eye._phase_of).
_PHASE_LAST10 = 10


def _phase_of(seconds_left, period) -> str:
    if seconds_left is None:
        return "UNKNOWN"
    frac = seconds_left / period if period > 0 else 1.0
    if seconds_left <= _PHASE_LAST10:
        return "LAST10"
    if frac <= 1 / 3:
        return "LATE"
    if frac <= 2 / 3:
        return "MID"
    return "EARLY"


def _phi(x: float) -> float:
    """Standard normal CDF (Abramowitz-Stegun 7.1.26 — no scipy needed)."""
    s = -1.0 if x < 0 else 1.0
    x = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t)
                 + 1.421413741) * t - 0.284496736) * t + 0.254829592) \
        * t * math.exp(-x * x)
    return 0.5 * (1.0 + s * y)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def pressure_from_anatomy(ticks) -> tuple[int, int, str]:
    """UNIFIED buyer/seller pressure of a tick sequence (prices, oldest→newest).

    Blend of:
      * count flow — % of up-moves among all moves (what the eye counts)
      * volume flow — Σ positive deltas / Σ |deltas| (who MOVES price)
    Returns (buyer_pct, seller_pct, state) where state ∈
    {DOMINANT_BUYERS, BUYERS, BALANCED, SELLERS, DOMINANT_SELLERS}.
    This is the ONE definition the live panel, the API and the roadmap
    all consume — the old code had three (62% count / 55% volume /
    70-80 count bands) that contradicted each other on the same candle.
    """
    ticks = list(ticks)
    n = len(ticks)
    if n < 2:
        return 50, 50, "BALANCED"
    up = dn = 0
    vol_up = vol_dn = 0.0
    for i in range(1, n):
        d = ticks[i] - ticks[i - 1]
        if d > 0:
            up += 1
            vol_up += d
        elif d < 0:
            dn += 1
            vol_dn -= d
    moves = up + dn
    count_flow = (up / moves) if moves else 0.5
    vol_tot = vol_up + vol_dn
    vol_flow = (vol_up / vol_tot) if vol_tot > 0 else 0.5
    blend = 0.5 * count_flow + 0.5 * vol_flow
    buyer_pct = int(round(blend * 100))
    seller_pct = 100 - buyer_pct
    if buyer_pct >= PRESSURE_DOMINANT:
        state = "DOMINANT_BUYERS"
    elif seller_pct >= PRESSURE_DOMINANT:
        state = "DOMINANT_SELLERS"
    elif buyer_pct >= PRESSURE_LEAN:
        state = "BUYERS"
    elif seller_pct >= PRESSURE_LEAN:
        state = "SELLERS"
    else:
        state = "BALANCED"
    return buyer_pct, seller_pct, state


_PRESSURE_BN = {
    "DOMINANT_BUYERS":  "প্রবল বায়ার",
    "BUYERS":           "বায়ার ভারী",
    "BALANCED":         "ব্যালান্সড",
    "SELLERS":          "সেলার ভারী",
    "DOMINANT_SELLERS": "প্রবল সেলার",
}


def _median(values) -> float:
    vs = sorted(values)
    m = len(vs)
    if m == 0:
        return 0.0
    if m % 2:
        return vs[m // 2]
    return 0.5 * (vs[m // 2 - 1] + vs[m // 2])


def _sigma_full(ticks, elapsed_frac: float, recent_candles) -> float:
    """Expected FULL-candle move magnitude (σ of the open→close walk).

    Blend of (a) the median high-low range of recent CLOSED candles and
    (b) the running candle's own range unscaled by elapsed fraction —
    (b) alone is biased low early / high late, (a) alone ignores the
    current candle's personality; the blend is stable at every phase.
    """
    rngs = []
    if recent_candles:
        for c in recent_candles[-SIGMA_LOOKBACK:]:
            try:
                r = float(c.get("high", 0.0)) - float(c.get("low", 0.0))
                if r > 0:
                    rngs.append(r)
            except Exception:
                continue
    hist_sigma = _median(rngs) if rngs else 0.0
    own_rng = (max(ticks) - min(ticks)) if ticks else 0.0
    own_sigma = own_rng / max(elapsed_frac, 0.20)
    if hist_sigma <= 0:
        return own_sigma
    if own_sigma <= 0:
        return hist_sigma
    # Weight shifts toward the candle's own behavior as it fills up.
    w_own = _clamp(elapsed_frac, 0.15, 0.60)
    return (1.0 - w_own) * hist_sigma + w_own * own_sigma


def predict_running_candle(ticks, open_price: float, period: int,
                           candle_open_time: float, now: float | None = None,
                           recent_candles=None) -> dict | None:
    """Predict where the RUNNING candle is heading — every-tick, leak-free.

    Parameters
      ticks           — the running candle's prices so far (oldest → newest)
      open_price      — the candle's open price
      period          — candle period in seconds
      candle_open_time— candle open epoch-seconds (for seconds_left)
      now             — current epoch-seconds (defaults to time.time());
                        pass a SERVER-ALIGNED clock when available
      recent_candles  — last ~20 closed candle dicts ({high, low, ...})
                        for the σ estimate (optional but recommended)
    """
    t0 = time.perf_counter()
    if now is None:
        now = time.time()
    if ticks is None or open_price is None or period is None:
        return None
    ticks = list(ticks)
    n = len(ticks)
    seconds_left = max(0, int(round(candle_open_time + period - now))) \
        if candle_open_time and candle_open_time > 0 else None
    if n < MIN_TICKS or open_price <= 0:
        return {
            "ready": False, "tick_count": n,
            "seconds_left": seconds_left,
            "phase": _phase_of(seconds_left, period),
            "direction": "NEUTRAL", "confidence": 0,
            "note_bn": f"টিক {n} — ইঞ্জিনের জন্য যথেষ্ট ডেটা নেই",
        }
    if n > MAX_SCAN_TICKS:
        ticks = ticks[-MAX_SCAN_TICKS:]
        n = len(ticks)

    op = float(open_price)
    cur = ticks[-1]
    hi = max(ticks)
    lo = min(ticks)
    rng = hi - lo
    net = cur - op

    elapsed = _clamp(now - candle_open_time, 0.5, float(period)) \
        if candle_open_time and candle_open_time > 0 else 0.5 * period
    elapsed_frac = _clamp(elapsed / float(period), 0.02, 0.98)
    tau = 1.0 - elapsed_frac                       # remaining time fraction
    secs_left_f = max(0.0, tau * float(period))

    # ── σ model ───────────────────────────────────────────────────────────
    sig_full = _sigma_full(ticks, elapsed_frac, recent_candles)
    sig_remaining = sig_full * math.sqrt(tau)
    lock_fraction = 1.0 - _clamp(
        (sig_remaining / sig_full) if sig_full > 0 else 0.0, 0.0, 1.0)

    # ── Anatomy (qualitative eye — reused, not duplicated) ────────────────
    anatomy = None
    if _anatomy_of is not None:
        try:
            anatomy = _anatomy_of(ticks, op, period)
        except Exception:
            anatomy = None

    velocity = (anatomy or {}).get("velocity", 0.0)          # -1..1
    close_pos = (anatomy or {}).get("close_position", 0.5)   # 0..1
    late_flip = (anatomy or {}).get("late_flip") or {}
    late_wick = (anatomy or {}).get("late_wick") or {}
    tick_burst = (anatomy or {}).get("tick_burst") or {}

    # ── Momentum extrapolation ────────────────────────────────────────────
    # TWO drift estimators, blended (BT-2026-09-29 finding):
    #   stable   — whole-candle net/elapsed: the regime's drift rate; the
    #              single best predictor of the remaining move on a
    #              trending candle (segment-only velocity measured 31% WR
    #              — it bets AGAINST trends).
    #   reactive — last-segment velocity (tick_eye convention ≈ last 1/6
    #              of ticks): catches late control-transfer, noisier.
    # Both × PERSISTENCE (fitted walk-forward) over the remaining time.
    seg_n = max(5, int(round(n * 0.167)))
    seg = ticks[-seg_n:]
    seg_net = cur - seg[0]
    elapsed_secs = max(elapsed, 1.0)
    ticks_per_sec = max(n / elapsed_secs, 0.1)
    stable_rate = net / elapsed_secs
    reactive_rate = (seg_net / max(seg_n - 1, 1)) * ticks_per_sec \
        if seg_n > 1 else 0.0
    drift_px_per_sec = (DRIFT_STABLE_W * stable_rate
                        + (1.0 - DRIFT_STABLE_W) * reactive_rate)
    drift_remaining = drift_px_per_sec * PERSISTENCE * secs_left_f

    # A late-flip SPIKE (1-2 tick giant print that flipped the color) is a
    # bad print, not momentum — the eye already detected it; withhold the
    # drift in the spike's direction.
    if late_flip.get("is_spike_noise"):
        if late_flip.get("to_color") == "GREEN" and drift_remaining > 0:
            drift_remaining = 0.0
        elif late_flip.get("to_color") == "RED" and drift_remaining < 0:
            drift_remaining = 0.0

    # ── Probabilities (Gaussian tail model) ──────────────────────────────
    # NOTE (BT-2026-09-29): the earlier mean-reversion pull (extended
    # candle + rejected wick → bet a pull-back) was REMOVED — on trend
    # regimes it systematically bets against the drift and measured 31%
    # WR on entries. The spike-noise withholding above is the only
    # anti-momentum guard; the honest σ-model already shrinks late-candle
    # drift as seconds_left → 0.
    exp_close = cur + drift_remaining
    if sig_remaining > 1e-12:
        z_close = (exp_close - op) / sig_remaining
        z_here = (exp_close - cur) / sig_remaining
        p_close_green = _phi(z_close)
        p_up_from_here = _phi(z_here)
    else:
        # No time / no volatility left — candle is mechanically locked.
        p_close_green = 1.0 if exp_close >= op else 0.0
        p_up_from_here = 0.5
    p_close_green = _clamp(p_close_green, P_FLOOR, 1.0 - P_FLOOR)
    p_up_from_here = _clamp(p_up_from_here, P_FLOOR, 1.0 - P_FLOOR)

    # Direction + confidence (of the "where will this candle close" call).
    direction = "CALL" if p_close_green > 0.5 else "PUT"
    conf_raw = abs(p_close_green - 0.5) * 200.0
    # Data sufficiency: few ticks → discount; late phase → the lock itself
    # is real information (a locked candle SHOULD read confident).
    sufficiency = min(1.0, n / 24.0)
    confidence = int(round(_clamp(conf_raw * (0.45 + 0.55 * sufficiency),
                                  0, MAX_CONFIDENCE)))

    # ── Unified pressure ─────────────────────────────────────────────────
    buyer_pct, seller_pct, pressure_state = pressure_from_anatomy(ticks)

    # ── Entry-quality hint (the trader's actual decision) ────────────────
    # |p_up_from_here − 0.5| ≥ ENTRY_EDGE and enough data → the moment a
    # video-trader would click. Honest: early-candle hints are rare by
    # construction (σ_remaining is large), LAST10 hints dominate.
    entry_hint = None
    if n >= 16:
        edge = p_up_from_here - 0.5
        if edge >= 0.15:
            entry_hint = "CALL"
        elif edge <= -0.15:
            entry_hint = "PUT"

    # ── Factors (Bengali — surface in the UI panel) ──────────────────────
    factors = []
    if abs(velocity) >= 0.15:
        factors.append({
            "name": "velocity", "dir": "CALL" if velocity > 0 else "PUT",
            "note_bn": (f"শেষ সেগমেন্টের ভেলোসিটি "
                        f"{'উপরে' if velocity > 0 else 'নিচে'} "
                        f"({velocity:+.0%} রেঞ্জ)")})
    if close_pos >= 0.75:
        factors.append({"name": "close_pos", "dir": "CALL",
                        "note_bn": f"প্রাইস হাই-এর কাছে ({close_pos:.0%})"})
    elif close_pos <= 0.25:
        factors.append({"name": "close_pos", "dir": "PUT",
                        "note_bn": f"প্রাইস লো-এর কাছে ({close_pos:.0%})"})
    if late_flip.get("detected"):
        if late_flip.get("is_real"):
            factors.append({
                "name": "late_flip",
                "dir": "CALL" if late_flip.get("to_color") == "GREEN" else "PUT",
                "note_bn": (f"রিয়েল লেট ফ্লিপ "
                            f"{late_flip.get('from_color')}→"
                            f"{late_flip.get('to_color')} "
                            f"({late_flip.get('flip_ticks')} টিক)")})
        elif late_flip.get("is_spike_noise"):
            factors.append({
                "name": "flip_spike",
                "dir": "PUT" if late_flip.get("to_color") == "GREEN" else "CALL",
                "note_bn": "স্পাইক-নয়েজ ফ্লিপ — নকল, ফেরার সম্ভাবনা"})
    if late_wick.get("rejected"):
        factors.append({
            "name": "wick_reject",
            "dir": "PUT" if late_wick.get("side") == "UPPER" else "CALL",
            "note_bn": (f"{'উপরের' if late_wick.get('side') == 'UPPER' else 'নিচের'} "
                        f"উইক রিজেক্ট হয়েছে")})
    if tick_burst.get("is_burst"):
        factors.append({
            "name": "tick_burst",
            "dir": "CALL" if velocity > 0 else "PUT" if velocity < 0 else None,
            "note_bn": f"টিক বার্সট ×{tick_burst.get('ratio')}"})
    if abs(net) > 0.9 * sig_full and sig_full > 0:
        factors.append({
            "name": "locked",
            "dir": "CALL" if net > 0 else "PUT",
            "note_bn": (f"ক্যান্ডেল প্রায় লকড "
                        f"({'গ্রিন' if net > 0 else 'রেড'}, "
                        f"{lock_fraction:.0%} নিশ্চিত)")})

    # ── Reasons (top-line Bengali verdict) ────────────────────────────────
    reasons_bn = []
    reasons_bn.append(
        f"ক্যান্ডেল "
        f"{'গ্রিন' if net > 0 else 'রেড' if net < 0 else 'ফ্ল্যাট'} "
        f"({net:+.5f}), আর {seconds_left if seconds_left is not None else '?'}s বাকি")
    reasons_bn.append(
        f"বায়ার {buyer_pct%100 if False else buyer_pct}% · "
        f"সেলার {seller_pct}% — {_PRESSURE_BN.get(pressure_state, pressure_state)}")

    compute_us = int((time.perf_counter() - t0) * 1_000_000)

    return {
        "ready": True,
        "engine": "live_candle_v1",
        "tick_count": n,
        "seconds_left": seconds_left,
        "phase": _phase_of(seconds_left, period),
        # The prediction the user asked for: where this candle is heading.
        "direction": direction,
        "confidence": confidence,
        "p_close_green": round(p_close_green, 4),
        "p_up_from_here": round(p_up_from_here, 4),
        # Unified pressure (ONE definition — audit C4 fix).
        "buyer_pct": buyer_pct,
        "seller_pct": seller_pct,
        "pressure_state": pressure_state,
        "pressure_bn": _PRESSURE_BN.get(pressure_state, pressure_state),
        # Anatomy snapshot.
        "net": round(net, 6),
        "range": round(rng, 6),
        "velocity": velocity,
        "close_position": close_pos,
        "lock_fraction": round(lock_fraction, 3),
        # The trader's actionable moment (or null).
        "entry_hint": entry_hint,
        "factors": factors,
        "reasons_bn": reasons_bn,
        "server_ms": int(time.time() * 1000),
        "compute_us": compute_us,
    }
