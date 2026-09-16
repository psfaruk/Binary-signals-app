"""
core/tick_eye.py — TICK-EYE: the "human eye" tick-level candle reader
(TICK-EYE 2026-09-16).

USER REQUIREMENT (verbatim):
  "আমরা মানুষ চোক দিয়ে ক্যান্ডেল দেখতে পারি, বুঝতেও পারি ক্যান্ডেল এ কি
   হচ্ছে মিলি সেকেন্ড এর কম সময়ে ও, এমন কোন সিস্টেম করা যাবে না আছে,
   একটি ওয়েব সাইট বানিয়ে টিক মানুষের মতোই কাজে লাগানো যাবে?"

WHAT A HUMAN EYE ACTUALLY WATCHES ON A RUNNING CANDLE (implemented here,
one function per skill):

  1. ENDING VELOCITY   — the last ~10 seconds of ticks: is price MOVING
                        into the close or drifting? (net move of the final
                        tick-segment, normalised by the candle's own range)
  2. ENDING FLOW       — buy-tick vs sell-tick pressure in the final
                        segment (order-flow imbalance the eye perceives as
                        "ভারী কেনা হচ্ছে / বেচা হচ্ছে")
  3. LATE FLIP         — the classic 1-minute trap: candle was RED at
                        57-58s, then flipped GREEN in the last 2s (or vice
                        versa). We detect WHERE control transferred, how
                        many ticks the flip took, and how far it traveled.
  4. CLOSE POSITION    — where the (current/last) price sits inside the
                        candle's high-low range. Close near high = bullish
                        conviction; near low = bearish conviction.
  5. TICK BURST        — abnormal tick-rate acceleration in the final
                        segment (activity spike — someone big just showed
                        up). Tick count in the final segment vs the
                        per-segment average.
  6. WICK REJECT SPEED — a fresh wick (new extreme) that got REJECTED
                        fast in the late phase = stop-hunt / failed
                        breakout, the eye sees "উপরে গিয়ে ব্যর্থ হয়ে
                        নেমে এসেছে".

DESIGN CONSTRAINTS (from the repo's hard-won lessons — see
core/constants.py "READ THIS BEFORE RUNNING ANOTHER THEORY-PRUNE ROUND"):
  * NO look-ahead: every input is the tick sequence up to "now".
  * Windows are TICK-COUNT based (the repo's established convention —
    stream.ticks carries prices only, and tickrun/_lateflip already use
    70/30 count splits). For a 60s candle the final 1/6 of ticks ≈ the
    last ~10 seconds.
  * Output is PURE DATA + an honest "verdict" lean. It is NOT a signal by
    itself; the prediction module (engines/base/modules/tick_eye.py) turns
    STRONG evidence into a single module vote, and the strict confluence
    engine decides the final signal.
  * Cheap: O(n) single pass over at most the last 400 ticks — designed
    to run on EVERY broadcast tick without blocking the event loop.

Public API:
  analyze_candle_ticks(ticks, open_price, period) -> dict | None
      Full anatomy of a tick sequence (running or closed candle).
  live_eye(ticks, open_price, period, candle_open_time, now) -> dict | None
      Same + time-aware extras (seconds_left, phase, last10) for the UI.
  eye_verdict(anatomy) -> (direction, strength_pct, reasons)
      Net human-eye lean ("এখন চোখে যা দেখা যাচ্ছে").
"""

from __future__ import annotations

import math
import os

# ── Tunables (env-overridable, per repo convention) ─────────────────────────
# Final-segment size as a fraction of the tick count. 1/6 ≈ last 10s of a
# 60s candle (uniform tick-rate approximation; matches tickrun's 70/30 split
# style and needs no timestamp plumbing).
FINAL_SEGMENT_FRAC = float(os.environ.get("QX_TICK_EYE_FINAL_SEG", "0.167"))
# Minimum ticks before the eye trusts anything.
MIN_TICKS = int(os.environ.get("QX_TICK_EYE_MIN_TICKS", "12"))
# Final segment must have at least this many ticks to judge velocity/flow.
MIN_FINAL_TICKS = int(os.environ.get("QX_TICK_EYE_MIN_FINAL_TICKS", "5"))
# How far (as fraction of range) the price must travel in the final segment
# for ending velocity to be called decisive.
VELOCITY_DECISIVE = float(os.environ.get("QX_TICK_EYE_VEL_DECISIVE", "0.35"))
# Close-position bands: within X of an extreme = conviction.
CLOSE_POSITION_EXTREME = float(os.environ.get("QX_TICK_EYE_CLOSE_EXTREME", "0.25"))
# Late-flip: flip must cover at least this fraction of range AND take at
# least this many ticks to count as a REAL flip (1-2 tick "print spikes"
# are noise — the user explicitly asked for this distinction).
FLIP_MIN_TRAVEL = float(os.environ.get("QX_TICK_EYE_FLIP_TRAVEL", "0.30"))
FLIP_MIN_TICKS = int(os.environ.get("QX_TICK_EYE_FLIP_TICKS", "3"))
# Tick-burst: final segment must contain >= this multiple of the average
# per-segment tick count.
BURST_MULTIPLE = float(os.environ.get("QX_TICK_EYE_BURST_MULT", "1.8"))
# How far the late wick must be rejected (retrace fraction of the wick).
WICK_REJECT_FRAC = float(os.environ.get("QX_TICK_EYE_WICK_REJECT", "0.60"))
# Max ticks scanned per call (keep O(n) bounded on 2000-tick buffers).
MAX_SCAN_TICKS = int(os.environ.get("QX_TICK_EYE_MAX_SCAN", "400"))

__all__ = [
    "analyze_candle_ticks", "live_eye", "eye_verdict",
    "MIN_TICKS", "FINAL_SEGMENT_FRAC",
]


def _fmt(p: float, digits: int = 6) -> str:
    """Trim a price for human-readable reasons."""
    try:
        s = f"{p:.{digits}f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return str(p)


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def analyze_candle_ticks(ticks, open_price: float, period: int = 60) -> dict | None:
    """Compute the full tick-anatomy of one candle's tick sequence.

    `ticks` may be the RUNNING candle's ticks (live eye) or a CLOSED
    candle's ticks (EOC confirmation). Prices only, oldest → newest.
    Returns None below MIN_TICKS (honest "eye can't see yet").
    """
    if ticks is None:
        return None
    ticks = list(ticks)
    n = len(ticks)
    if n < MIN_TICKS or open_price is None:
        return None
    if n > MAX_SCAN_TICKS:
        ticks = ticks[-MAX_SCAN_TICKS:]
        n = len(ticks)

    op = float(open_price)
    cur = ticks[-1]
    hi = max(ticks)
    lo = min(ticks)
    rng = hi - lo

    # Whole-candle flow (buy vs sell tick pressure)
    up_all = sum(1 for i in range(1, n) if ticks[i] > ticks[i - 1])
    dn_all = sum(1 for i in range(1, n) if ticks[i] < ticks[i - 1])
    moves_all = up_all + dn_all
    flow_all = (up_all / moves_all) if moves_all else 0.5

    # ── Final segment (≈ last 10s on a 60s candle) ──────────────────────────
    seg_n = max(MIN_FINAL_TICKS, int(round(n * FINAL_SEGMENT_FRAC)))
    seg = ticks[-seg_n:]
    seg_start = ticks[-seg_n]
    seg_net = cur - seg_start
    up_seg = sum(1 for i in range(1, len(seg)) if seg[i] > seg[i - 1])
    dn_seg = sum(1 for i in range(1, len(seg)) if seg[i] < seg[i - 1])
    moves_seg = up_seg + dn_seg
    flow_seg = (up_seg / moves_seg) if moves_seg else 0.5
    # Velocity: final-segment net move, normalised by the candle's range.
    velocity = (seg_net / rng) if rng > 0 else 0.0
    if velocity > 1.0:
        velocity = 1.0
    elif velocity < -1.0:
        velocity = -1.0

    # ── Close position inside the candle range ─────────────────────────────
    close_pos = ((cur - lo) / rng) if rng > 0 else 0.5
    if close_pos > 1.0:
        close_pos = 1.0
    elif close_pos < 0.0:
        close_pos = 0.0

    # ── Late flip detection (57-58s red → 59-60s green trap) ───────────────
    # The eye's definition of a LATE FLIP: the candle carried the OPPOSITE
    # color inside the late window, and the final color was set by a late
    # PUSH. Two questions decide real-vs-noise:
    #   push_ticks — how many consecutive ticks have been advancing (or
    #                declining) into the close. 1-2 ticks = a print spike
    #                (the "1 tick green at 59s" fake). >=3 ticks = a real
    #                controlled push (buyers genuinely took over).
    #   travel      — how much of the candle range the push covered.
    final_color = "GREEN" if cur > op else ("RED" if cur < op else "FLAT")
    late_flip = None
    if final_color in ("GREEN", "RED"):
        # Was the candle the OPPOSITE color earlier in the late window?
        window_start = max(0, n - 2 * seg_n)
        early_color = None
        for i in range(window_start, n - 1):
            c = "GREEN" if ticks[i] > op else ("RED" if ticks[i] < op else "FLAT")
            if c != "FLAT" and c != final_color:
                early_color = c
                break
        if early_color is not None:
            # Walk back from the end counting the consecutive push ticks.
            # Also record the LARGEST single step inside the push — a real
            # controlled push has no single step dominating the move, while
            # a print spike is one giant step ≈ the whole travel.
            if final_color == "GREEN":
                push_ticks = 1
                max_step = 0.0
                for i in range(n - 1, 0, -1):
                    if ticks[i] > ticks[i - 1]:
                        push_ticks += 1
                        max_step = max(max_step, ticks[i] - ticks[i - 1])
                    else:
                        break
                anchor = min(ticks[max(0, n - push_ticks - 2):])
                travel_abs = cur - anchor
                travel = (travel_abs / rng) if rng > 0 else 0.0
            else:
                push_ticks = 1
                max_step = 0.0
                for i in range(n - 1, 0, -1):
                    if ticks[i] < ticks[i - 1]:
                        push_ticks += 1
                        max_step = max(max_step, ticks[i - 1] - ticks[i])
                    else:
                        break
                anchor = max(ticks[max(0, n - push_ticks - 2):])
                travel_abs = anchor - cur
                travel = (travel_abs / rng) if rng > 0 else 0.0
            # Spike test: one step covering >50% of the push distance is a
            # single bad print, not a controlled transfer of control.
            is_spike = bool(travel_abs > 0 and max_step > 0.50 * travel_abs)
            is_real = bool(travel >= FLIP_MIN_TRAVEL
                          and push_ticks >= FLIP_MIN_TICKS
                          and not is_spike)
            late_flip = {
                "detected": True,
                "from_color": early_color,       # e.g. RED (57-58s)
                "to_color": final_color,         # e.g. GREEN (last 2s)
                "flip_ticks": push_ticks,        # consecutive ticks of the push
                "travel_frac": round(max(0.0, min(1.0, travel)), 3),
                "max_step_frac": round(max(0.0, min(1.0,
                    (max_step / travel_abs) if travel_abs > 0 else 0.0)), 3),
                "is_real": is_real,
                "is_spike_noise": bool(not is_real),
            }

    # ── Tick burst (activity acceleration in the final segment) ───────────
    # Average ticks per same-sized segment across the candle, then compare.
    avg_per_seg = n / max(1.0, 1.0 / FINAL_SEGMENT_FRAC)
    burst_ratio = (seg_n / avg_per_seg) if avg_per_seg > 0 else 1.0
    tick_burst = {
        "ratio": round(burst_ratio, 2),
        "is_burst": bool(burst_ratio >= BURST_MULTIPLE),
    }

    # ── Late wick rejection (new extreme rejected fast) ────────────────────
    # Did the final segment TOUCH a new candle extreme and pull back?
    late_wick = None
    if rng > 0:
        seg_hi = max(seg)
        seg_lo = min(seg)
        made_new_hi = seg_hi >= hi
        made_new_lo = seg_lo <= lo
        if made_new_hi and not made_new_lo:
            wick_size = seg_hi - max(cur, seg[0])
            retrace = (seg_hi - cur) / (seg_hi - seg_lo) if seg_hi > seg_lo else 0.0
            late_wick = {
                "side": "UPPER", "new_extreme": True,
                "rejected": bool(cur < seg_hi - 0.5 * (seg_hi - seg_lo)),
                "retrace_frac": round(max(0.0, min(1.0, retrace)), 3),
            } if wick_size > 0 else None
        elif made_new_lo and not made_new_hi:
            wick_size = min(cur, seg[0]) - seg_lo
            retrace = (cur - seg_lo) / (seg_hi - seg_lo) if seg_hi > seg_lo else 0.0
            late_wick = {
                "side": "LOWER", "new_extreme": True,
                "rejected": bool(cur > seg_lo + 0.5 * (seg_hi - seg_lo)),
                "retrace_frac": round(max(0.0, min(1.0, retrace)), 3),
            } if wick_size > 0 else None

    anatomy = {
        "tick_count": n,
        "open": op,
        "high": hi,
        "low": lo,
        "last": cur,
        "range": rng,
        "net": cur - op,
        "color": final_color,
        # whole-candle order flow
        "flow_all": round(flow_all, 3),
        "buy_pct": round(flow_all * 100),
        "sell_pct": round((1.0 - flow_all) * 100),
        # final segment (≈ last 10s)
        "final_ticks": seg_n,
        "flow_final": round(flow_seg, 3),
        "buy_pct_final": round(flow_seg * 100),
        "sell_pct_final": round((1.0 - flow_seg) * 100),
        "velocity": round(velocity, 3),          # -1..1, + = pushing up
        "velocity_decisive": bool(abs(velocity) >= VELOCITY_DECISIVE),
        "close_position": round(close_pos, 3),   # 0..1
        "late_flip": late_flip,
        "tick_burst": tick_burst,
        "late_wick": late_wick,
    }
    direction, strength, reasons = eye_verdict(anatomy)
    anatomy["eye_direction"] = direction
    anatomy["eye_strength"] = strength
    anatomy["eye_reasons"] = reasons
    return anatomy


def live_eye(ticks, open_price: float, period: int, candle_open_time: float,
             now: float | None = None) -> dict | None:
    """Running-candle eye: anatomy + time-aware fields for the UI panel.

    `now` defaults to time.time(). seconds_left drives the live countdown;
    phase is EARLY/MID/LATE/LAST10 so the frontend can highlight the
    critical window (user: "লাস্ট 10 সেকেন্ড এ একটি ক্যান্ডেল এ কি ঘটে").
    """
    import time as _time
    if now is None:
        now = _time.time()
    anatomy = analyze_candle_ticks(ticks, open_price, period)
    if anatomy is None:
        # Still return a minimal shell so the UI panel can render the
        # countdown even before enough ticks exist.
        n = len(ticks) if ticks else 0
        seconds_left = max(
            0, int(round(candle_open_time + period - now))) if candle_open_time > 0 else None
        return {
            "tick_count": n, "ready": False,
            "seconds_left": seconds_left,
            "phase": _phase_of(seconds_left, period),
            "eye_direction": "NEUTRAL", "eye_strength": 0,
            "eye_reasons": [f"টিক কম ({n}) — চোখের জন্য যথেষ্ট ডেটা নেই"],
        }
    seconds_left = max(
        0, int(round(candle_open_time + period - now))) if candle_open_time > 0 else None
    anatomy["ready"] = True
    anatomy["seconds_left"] = seconds_left
    anatomy["phase"] = _phase_of(seconds_left, period)
    return anatomy


def _phase_of(seconds_left: int | None, period: int) -> str:
    if seconds_left is None:
        return "UNKNOWN"
    frac = seconds_left / period if period > 0 else 1.0
    if seconds_left <= 10:
        return "LAST10"
    if frac <= 1 / 3:
        return "LATE"
    if frac <= 2 / 3:
        return "MID"
    return "EARLY"


def eye_verdict(anatomy: dict) -> tuple[str, int, list[str]]:
    """Net human-eye lean from a full anatomy dict.

    Returns (direction, strength_pct 0-100, reasons[...]) — the reasons are
    written in the user's language because they surface directly in the UI
    panel ("চোখে যা দেখা যাচ্ছে").
    """
    reasons: list[str] = []
    call_pts = 0.0
    put_pts = 0.0

    # Spike guard: if the ending move is a 1-2 tick print spike that
    # flipped the candle's color, its "velocity" is a single bad print —
    # not real momentum. Detect it first so velocity points can be
    # withheld in that direction.
    _lf_early = anatomy.get("late_flip") or {}
    _spike_dir = None
    if _lf_early.get("is_spike_noise"):
        _spike_dir = "CALL" if _lf_early.get("to_color") == "GREEN" else "PUT"

    vel = anatomy.get("velocity") or 0.0
    if anatomy.get("velocity_decisive"):
        if vel > 0:
            if _spike_dir != "CALL":
                call_pts += 30
                reasons.append(
                    f"শেষ ১০ সেকেন্ডের ভেলোসিটি উপরের দিকে "
                    f"(+{_pct(vel)} রেঞ্জ) — দ্রুত কেনা হচ্ছে")
        else:
            if _spike_dir != "PUT":
                put_pts += 30
                reasons.append(
                    f"শেষ ১০ সেকেন্ডের ভেলোসিটি নিচের দিকে "
                    f"({_pct(vel)} রেঞ্জ) — দ্রুত বেচা হচ্ছে")
    elif abs(vel) >= 0.15:
        if vel > 0:
            call_pts += 12
        else:
            put_pts += 12

    # Ending order flow (graded: dominance deserves dominance)
    buy_f = anatomy.get("buy_pct_final")
    if buy_f is not None:
        if buy_f >= 80:
            call_pts += 25
            reasons.append(f"শেষ সেগমেন্টে বাই-টিক {buy_f}% — প্রবল বায়ার প্রেসার")
        elif buy_f >= 70:
            call_pts += 20
            reasons.append(f"শেষ সেগমেন্টে বাই-টিক {buy_f}% — বায়ার প্রেসার")
        elif buy_f <= 20:
            put_pts += 25
            reasons.append(f"শেষ সেগমেন্টে সেল-টিক {100 - buy_f}% — প্রবল সেলার প্রেসার")
        elif buy_f <= 30:
            put_pts += 20
            reasons.append(f"শেষ সেগমেন্টে সেল-টিক {100 - buy_f}% — সেলার প্রেসার")

    # Close position (graded: at-the-extreme = conviction) — BUT a
    # spike-noise ending puts the close at the extreme BY CONSTRUCTION of
    # the spike, so in the spike direction it is withheld (the spike
    # guard above already identified _spike_dir).
    cpos = anatomy.get("close_position")
    if cpos is not None:
        if cpos >= 0.90 and _spike_dir != "CALL":
            call_pts += 18
            reasons.append(f"প্রাইস হাই-এ — বুলিশ কনভিকশন (ক্লোজ পজিশন {_pct(cpos)})")
        elif cpos >= 1.0 - CLOSE_POSITION_EXTREME and _spike_dir != "CALL":
            call_pts += 15
            reasons.append(f"প্রাইস হাই-এর কাছে আছে (ক্লোজ পজিশন {_pct(cpos)})")
        elif cpos <= 0.10 and _spike_dir != "PUT":
            put_pts += 18
            reasons.append(f"প্রাইস লো-এ — বেয়ারিশ কনভিকশন (ক্লোজ পজিশন {_pct(cpos)})")
        elif cpos <= CLOSE_POSITION_EXTREME and _spike_dir != "PUT":
            put_pts += 15
            reasons.append(f"প্রাইস লো-এর কাছে আছে (ক্লোজ পজিশন {_pct(cpos)})")

    # Late flip — REAL flips get continuation weight (control transfer),
    # spike-noise flips get a small REVERSAL weight (fake print).
    lf = anatomy.get("late_flip")
    if lf and lf.get("detected"):
        if lf.get("is_real"):
            w = 25
            if lf.get("to_color") == "GREEN":
                call_pts += w
                reasons.append(
                    f"লেট ফ্লিপ: {lf.get('from_color')} → GREEN, "
                    f"{lf.get('flip_ticks')} টিকে, "
                    f"{_pct(lf.get('travel_frac', 0))} রেঞ্জ ট্রাভেল — রিয়েল কন্ট্রোল ট্রান্সফার")
            else:
                put_pts += w
                reasons.append(
                    f"লেট ফ্লিপ: {lf.get('from_color')} → RED, "
                    f"{lf.get('flip_ticks')} টিকে, "
                    f"{_pct(lf.get('travel_frac', 0))} রেঞ্জ ট্রাভেল — রিয়েল কন্ট্রোল ট্রান্সফার")
        else:
            # Spike noise: 1-2 tick giant print that flipped the color —
            # the classic "57-58s red → 1 tick green" fake. A color-flipping
            # spike carries the same information as a real flip, but in
            # the OPPOSITE direction: the print is an outlier, not demand.
            if lf.get("to_color") == "GREEN":
                put_pts += 25
            else:
                call_pts += 25
            reasons.append(
                f"লেট ফ্লিপ স্পাইক-নয়েজ ({lf.get('from_color')} → {lf.get('to_color')}, "
                f"{lf.get('flip_ticks')} টিক, এক স্টেপে {lf.get('max_step_frac', 0):.0%}) — "
                f"নকল ফ্লিপ, ফিরে যাওয়ার সম্ভাবনা")

    # Tick burst — activity spike amplifies whichever side is winning.
    tb = anatomy.get("tick_burst")
    if tb and tb.get("is_burst"):
        if call_pts > put_pts:
            call_pts += 8
            reasons.append(f"টিক বার্স্ট ×{tb.get('ratio')} — উপরের মুভে বড় অংশগ্রহণ")
        elif put_pts > call_pts:
            put_pts += 8
            reasons.append(f"টিক বার্স্ট ×{tb.get('ratio')} — নিচের মুভে বড় অংশগ্রহণ")

    # Late wick rejection
    lw = anatomy.get("late_wick")
    if lw and lw.get("rejected"):
        if lw.get("side") == "UPPER":
            put_pts += 15
            reasons.append("লেট উইক রিজেকশন (উপরের ব্রেকআউট ব্যর্থ) — সেল রিঅ্যাকশন")
        else:
            call_pts += 15
            reasons.append("লেট উইক রিজেকশন (নিচের ব্রেকডাউন ব্যর্থ) — বাই রিঅ্যাকশন")

    total = call_pts + put_pts
    if total <= 0:
        return "NEUTRAL", 0, ["চোখে স্পষ্ট কিছু দেখা যাচ্ছে না — ব্যালান্সড টিক"]

    # Honest strength scale: the winning side's evidence points directly
    # (velocity 30 + flow 25 + close-pos 18 + flip 25 + burst 8 + wick 15
    # ≈ 121 theoretical max; a realistic full single-direction stack lands
    # 75-95). One signal alone ≈ 25, two aligned ≈ 45-50, everything
    # aligned clamps at 85. The eye never claims 100 — it is honest about
    # uncertainty (repo lesson: inflated confidence is worse than none).
    if call_pts > put_pts:
        direction = "CALL"
        strength = int(round(call_pts))
    else:
        direction = "PUT"
        strength = int(round(put_pts))
    strength = max(10, min(85, strength))
    return direction, strength, reasons
