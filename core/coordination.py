"""
core/coordination.py — LIVE COORDINATION SIGNAL (COORDINATION-MS 2026-09-16).

USER REQUIREMENT (verbatim):
  "অ্যাপ এর প্রত্যেকটি ডেটা মিলি সেকেন্ড এ আপডেট হবে, একটি ক্যান্ডেল এ কি
   হচ্ছে মিলি সেকেন্ড এ ইঞ্জিন দেখবে। তারও সব কিছু মিলিয়ে সিগন্যাল আসবে
   রানিং ক্যান্ডেল এনালাইসিস, মডিউল, মডেল ইঞ্জিন এই সব কিছু মিলিয়ে
   করডিনেশন সিগন্যাল আসবে।"

WHAT THIS IS
────────────
On EVERY tick (event-driven, no timer, no throttle) the coordinator merges
the four independent voices that exist at that millisecond and answers one
question: "এই মুহূর্তে সব ভয়েস কি এক দিকে আছে?" (are all voices pointing
the same way right now?)

  ANCHOR — সিগন্যাল (module engine's published prediction for THIS candle;
           itself the product of the 14 blender modules + joint gate)
  VOICE 1 — চোখ (EYE): the running candle's live tick-anatomy verdict
           (core.tick_eye.live_eye) — phase-weighted because the user
           taught: the last 10 seconds is where confirmations happen
  VOICE 2 — মডেল (MODEL): the ML engine's frozen T+1 prediction for THIS
           candle (cached at EOC from the joint gate's model voice — zero
           DB reads in the tick loop). When the ML engine IS the signal
           source it is shown as "সোর্স" and excluded from scoring (it
           cannot verify itself).
  VOICE 3 — রান-কনফার্ম (RUNCONF): the running candle's first-order
           confirmation of the prediction (CONFIRMING / OPPOSING)

Output state machine:
  NO_SIGNAL       — this candle has no CALL/PUT anchor
  WAITING         — anchor exists but no other voice is ready yet
  ALIGNED_CALL /  — alignment >= ALIGNED_MIN (all/most voices agree)
  ALIGNED_PUT
  PARTIAL         — mixed voices (PARTIAL_MIN <= alignment < ALIGNED_MIN)
  CONFLICT        — voices predominantly oppose the anchor (< PARTIAL_MIN)

DESIGN CONSTRAINTS (repo lessons, see core/constants.py):
  * PURE function over already-computed inputs — no I/O, no DB, no clock
    reads beyond the single time.time() for the ms stamp. Designed to run
    on every tick of every stream: measured < 100 µs per call.
  * NO look-ahead: every input is the state as of THIS tick.
  * Honest abstention: a voice that cannot speak (eye needs >= 12 ticks and
    strength >= 30; provisional model abstains; runconf needs >= 5 ticks)
    is EXCLUDED from the denominator — coordination never borrows evidence
    it doesn't have.
  * Output is DATA + a coordination verdict. It never overrides the
    published signal (CONFLUENCE-V1: signal is final & immutable for the
    candle); it tells the user, at millisecond resolution, whether the
    live evidence still supports it.

Public API:
  compute_coordination(eye, prediction, model_voice, runconf) -> dict
"""

from __future__ import annotations

import os
import time

# ── Tunables (env-overridable, repo convention) ──────────────────────────────
# Voice base weights (normalised over PRESENT voices only).
EYE_WEIGHT    = float(os.environ.get("QX_COORD_EYE_W", "45"))
MODEL_WEIGHT  = float(os.environ.get("QX_COORD_MODEL_W", "30"))
RUNCONF_WEIGHT = float(os.environ.get("QX_COORD_RUNCONF_W", "25"))

# Phase multipliers for the EYE voice — the eye's verdict on 12 ticks at
# second 5 is far weaker than the same verdict at second 55. The user's
# teaching: "লাস্ট ১০ সেকেন্ডে কনফার্মেশন" — so LAST10 is the eye's
# strongest moment (×1.25), EARLY its weakest (×0.5).
PHASE_MULT = {
    "EARLY":  0.50,
    "MID":    0.80,
    "LATE":   1.00,
    "LAST10": 1.25,
    "UNKNOWN": 1.00,
}

# Eye must show at least this strength (0-100 honest scale) to speak.
EYE_MIN_STRENGTH = int(os.environ.get("QX_COORD_EYE_MIN_STR", "30"))
# Alignment bands.
ALIGNED_MIN  = int(os.environ.get("QX_COORD_ALIGNED_MIN", "70"))
PARTIAL_MIN  = int(os.environ.get("QX_COORD_PARTIAL_MIN", "40"))

__all__ = ["compute_coordination", "EYE_MIN_STRENGTH",
           "ALIGNED_MIN", "PARTIAL_MIN"]

# Bengali voice labels (surface directly in the UI panel).
_EYE_LABEL    = "চোখ"
_MODEL_LABEL  = "মডেল"
_RUNCONF_LABEL = "রান-কনফার্ম"
_SIGNAL_LABEL = "সিগন্যাল"


def _agree(dir_a: str | None, anchor: str) -> bool:
    return dir_a is not None and dir_a == anchor


def compute_coordination(eye: dict | None,
                         prediction: dict | None,
                         model_voice: dict | None,
                         runconf: str | None) -> dict:
    """Merge the four voices at THIS tick into one coordination state.

    Parameters (all already computed by the tick pipeline — this function
    adds zero I/O):
      eye          — core.tick_eye.live_eye output (full anatomy or the
                     minimal "not ready" shell), or None
      prediction   — the stream's published prediction dict for the running
                     candle (module engine + joint gate product), or None
      model_voice  — cached ML T+1 voice for THIS candle:
                     {direction, probability, emit, state} or None
      runconf      — 'CONFIRMING' | 'OPPOSING' | None
    """
    t0 = time.perf_counter()

    sig = (prediction or {}).get("signal")
    conf = (prediction or {}).get("confidence")
    strength_lbl = (prediction or {}).get("strength")
    strategy = (prediction or {}).get("strategy")

    # ── Anchor chip (always emitted so the UI can render the row) ──────────
    voices_out = [{
        "name": "signal",
        "label": _SIGNAL_LABEL,
        "dir": sig if sig in ("CALL", "PUT") else None,
        "note": (f"কনফিডেন্স {conf}% · {strength_lbl or '—'}"
                 if sig in ("CALL", "PUT") else "এই ক্যান্ডেলে সিগন্যাল নেই"),
        "agree": True,   # the anchor agrees with itself by definition
        "role": "anchor",
    }]

    # ── No anchor → nothing to coordinate ─────────────────────────────────
    if sig not in ("CALL", "PUT"):
        return _finish("NO_SIGNAL", None, voices_out,
                       "এই ক্যান্ডেলে কোনো CALL/PUT সিগন্যাল নেই — "
                       "কোঅর্ডিনেশন করার অঙ্কর নেই।", t0, phase_of(eye))

    # ── Voice 1: EYE (running candle analysis) ─────────────────────────────
    eye_w = 0.0
    eye_dir = None
    eye_ready = bool(eye and eye.get("ready"))
    if eye is not None:
        phase = eye.get("phase") or "UNKNOWN"
        e_dir = eye.get("eye_direction")
        e_str = int(eye.get("eye_strength") or 0)
        mult = PHASE_MULT.get(phase, 1.0)
        eye_dir = e_dir if e_dir in ("CALL", "PUT") else None
        if not eye_ready:
            voices_out.append({
                "name": "eye", "label": _EYE_LABEL, "dir": None,
                "note": f"টিক {eye.get('tick_count', 0)} — চোখের জন্য যথেষ্ট না",
                "agree": None, "role": "abstain",
            })
        elif eye_dir is None or e_str < EYE_MIN_STRENGTH:
            voices_out.append({
                "name": "eye", "label": _EYE_LABEL, "dir": eye_dir,
                "note": f"শক্তি {e_str}% — চোখ স্পষ্ট দেখছে না",
                "agree": None, "role": "abstain",
            })
        else:
            eye_w = EYE_WEIGHT * mult
            agree = _agree(eye_dir, sig)
            voices_out.append({
                "name": "eye", "label": _EYE_LABEL, "dir": eye_dir,
                "note": f"শক্তি {e_str}% · {phase}",
                "agree": agree, "role": "voice", "weight": round(eye_w, 1),
            })
    else:
        voices_out.append({
            "name": "eye", "label": _EYE_LABEL, "dir": None,
            "note": "ডেটা নেই", "agree": None, "role": "abstain",
        })

    # ── Voice 2: MODEL (ML engine T+1 for this candle) ─────────────────────
    model_w = 0.0
    m = model_voice or {}
    m_dir = m.get("direction") if m.get("direction") in ("CALL", "PUT") else None
    m_state = m.get("state") or ("present" if m_dir else "no_model")
    if strategy == "ml_model_t1":
        # The ML engine IS the source of the anchor — it cannot verify
        # itself. Show it as the source, exclude from the score.
        voices_out.append({
            "name": "model", "label": _MODEL_LABEL, "dir": sig,
            "note": "সিগন্যালের সোর্স (ML ইঞ্জিন)", "agree": None,
            "role": "source",
        })
    elif m_state == "abstain_provisional" or m_state == "provisional":
        voices_out.append({
            "name": "model", "label": _MODEL_LABEL, "dir": None,
            "note": "প্রোভিশনাল মডেল — মত দেয় না", "agree": None,
            "role": "abstain",
        })
    elif m_dir is None:
        voices_out.append({
            "name": "model", "label": _MODEL_LABEL, "dir": None,
            "note": "এই ক্যান্ডেলে ফ্রিজ করা মডেল মত নেই", "agree": None,
            "role": "abstain",
        })
    else:
        model_w = MODEL_WEIGHT
        agree = _agree(m_dir, sig)
        prob = m.get("probability")
        note = (f"প্রবাব {prob:.0%}" if isinstance(prob, (int, float))
                else "মত দিয়েছে")
        voices_out.append({
            "name": "model", "label": _MODEL_LABEL, "dir": m_dir,
            "note": note, "agree": agree, "role": "voice",
            "weight": round(model_w, 1),
        })

    # ── Voice 3: RUNCONF (running confirmation) ────────────────────────────
    runconf_w = 0.0
    if runconf == "CONFIRMING":
        runconf_w = RUNCONF_WEIGHT
        voices_out.append({
            "name": "runconf", "label": _RUNCONF_LABEL, "dir": sig,
            "note": "রানিং টিক সিগন্যাল কনফার্ম করছে", "agree": True,
            "role": "voice", "weight": round(runconf_w, 1),
        })
    elif runconf == "OPPOSING":
        runconf_w = RUNCONF_WEIGHT
        voices_out.append({
            "name": "runconf", "label": _RUNCONF_LABEL, "dir":
                ("PUT" if sig == "CALL" else "CALL"),
            "note": "রানিং টিক সিগন্যালের বিপরীতে", "agree": False,
            "role": "voice", "weight": round(runconf_w, 1),
        })
    else:
        voices_out.append({
            "name": "runconf", "label": _RUNCONF_LABEL, "dir": None,
            "note": "যথেষ্ট টিক নেই", "agree": None, "role": "abstain",
        })

    # ── Alignment score over PRESENT voices only ───────────────────────────
    total_w = eye_w + model_w + runconf_w
    if total_w <= 0:
        return _finish("WAITING", None, voices_out,
                       "সিগন্যাল আছে, কিন্তু অন্য ভয়েসগুলো এখনো প্রস্তুত না — "
                       "কোঅর্ডিনেশনের অপেক্ষায়।", t0, phase_of(eye))

    agree_w = 0.0
    for v in voices_out:
        if v.get("role") == "voice" and v.get("agree"):
            agree_w += float(v.get("weight") or 0.0)
    alignment = int(round(100.0 * agree_w / total_w))

    # ── State machine ─────────────────────────────────────────────────────
    if alignment >= ALIGNED_MIN:
        state = "ALIGNED_CALL" if sig == "CALL" else "ALIGNED_PUT"
        summary = (f"কোঅর্ডিনেশন {alignment}% — সব ভয়েস "
                   f"{'কল' if sig == 'CALL' else 'পুট'} দিকে মিলেছে।")
    elif alignment >= PARTIAL_MIN:
        state = "PARTIAL"
        summary = (f"কোঅর্ডিনেশন {alignment}% — আংশিক মিল; কিছু ভয়েস "
                   f"সিগন্যালের বিপরীতে আছে, সতর্ক থাকুন।")
    else:
        state = "CONFLICT"
        summary = (f"কোঅর্ডিনেশন মাত্র {alignment}% — রানিং ক্যান্ডেলের প্রমাণ "
                   f"সিগন্যালের বিপরীতে যাচ্ছে।")

    return _finish(state, alignment, voices_out, summary, t0, phase_of(eye))


def phase_of(eye: dict | None) -> str | None:
    """The running candle's phase, taken from the eye shell when present."""
    return (eye or {}).get("phase")


def _finish(state: str, alignment, voices: list, summary_bn: str,
            t0: float, phase: str | None) -> dict:
    """Stamp ms-proof fields and return the coordination payload."""
    compute_us = int((time.perf_counter() - t0) * 1_000_000)
    return {
        "state": state,                       # state machine label
        "signal_state": state.split("_")[0] if state.startswith("ALIGNED")
                        else state,
        "alignment": alignment,                # 0-100 or None (WAITING)
        "voices": voices,                     # per-voice chips
        "phase": phase,                       # EARLY/MID/LATE/LAST10
        "summary_bn": summary_bn,             # one-line Bengali verdict
        "server_ms": int(time.time() * 1000), # server timestamp (ms)
        "compute_us": compute_us,             # compute time (microseconds)
    }
