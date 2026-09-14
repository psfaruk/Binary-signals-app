"""
core/joint_gate.py — SIGNAL SOURCE VERIFICATION GATE (2026-09-14 rewrite)

USER REQUIREMENT (verbatim, latest directive supersedes the old
reject-to-NEUTRAL behavior):

  "প্রত্যেক ক্যান্ডেল এ সিগন্যাল প্রধান করতে হবে, কিন্তু fallback signals
   দেওয়া যাবে না। ... যে কোনো একটি পাস হলেই সিগন্যাল দিবে। মডিউল
   ইঞ্জিন থেকে সিগন্যাল আসলো না — ML model থেকে সিগন্যাল টি আসবে।"

  = the module engine's ANY-ONE-THEORY signal and the ML model's frozen
    T+1 prediction are the two LEGITIMATE signal sources. A theory vote is
    the authority for its candle — this gate must never suppress it back
    to NEUTRAL (that would violate the every-candle requirement). Both
    verification voices now ADJUST CONFIDENCE and RECORD their verdicts
    instead of rejecting:

  1. MODEL VOICE — the ML engine's frozen T+1 prediction for THIS exact
     candle (target_time match). VERIFIED models speak; provisional
     (fast-train bootstrap) models abstain honestly.
       * emit=True + same direction   ⇒ AGREE   ⇒ +3 confidence
       * emit=True + opposite         ⇒ OPPOSE  ⇒ -5 confidence (floored)
       * no model / emit=False        ⇒ abstain (no adjustment)
       * edge-guard suspended         ⇒ noted, -10 confidence (floored),
                                         the signal still ships — the pair
                                         health is surfaced in the UI
  2. VERIFIER VOICE — the 5 real-time layers (price action, wick
     rejection, tick momentum, key level, historical pattern).
       * CONFIRM ⇒ ×1.1 (cap 95)
       * PASS    ⇒ unchanged
       * WEAKEN  ⇒ ×0.75
       * VETO (any single layer) ⇒ ×0.55, floor 30 — a strong honesty
         penalty, but the signal direction STANDS per the every-candle
         mandate (the UI shows the veto so the user can skip it).
  3. NO FALLBACK POLICY — heuristic fallback signals are banned upstream
     (confluence.py any-theory rewrite); nothing here re-opens them.

Fail-OPEN on internal errors: the theory vote is the authority, so a gate
exception records the error and lets the signal through unchanged (the
old fail-closed path produced signal-less candles, violating the latest
directive).

Called from feed.py::_run_eoc, immediately after the classic engine
returns, before stream.prediction is set. When the classic engine returns
NEUTRAL (zero theories voted), feed.py takes the ML model's frozen T+1
prediction as the candle's signal BEFORE calling this gate, so the ML
voice becomes the SOURCE and the verifier voice grades it.
"""
import os
import time

from core.signal_verifier import verify_signal, _record_verdict

MAX_CONFIDENCE = 95
MIN_CONFIDENCE_FLOOR = 30

# Confidence adjustments (USER-2026-09-14: adjust, never reject)
VETO_MULT = 0.55          # any verifier layer VETO
WEAKEN_MULT = 0.75        # aggregate WEAKEN
CONFIRM_MULT = 1.10       # aggregate CONFIRM
ML_AGREE_BONUS = 3        # verified ML emit=True, same direction
ML_OPPOSE_PENALTY = 5     # verified ML emit=True, opposite direction
ML_SUSPENDED_PENALTY = 10 # edge-guard suspended pair


def _short(reason, n=160):
    if not reason:
        return ""
    return reason if len(reason) <= n else reason[: n - 3] + "..."


def _ml_voice(asset, ml_payload, target_time):
    """The ML engine's opinion for the candle that just opened.

    Primary source: the live payload returned by on_candle_closed for this
    exact close (feed runs the ML freeze BEFORE the classic predict so the
    T+1 row for the new candle exists here). Fallback: the frozen DB row
    via tracker.latest_predictions — covers the watchdog / initial-snapshot
    _run_eoc calls where no live payload was captured.

    Returns (t1, model_status).
    """
    t1 = None
    model_status = None
    if isinstance(ml_payload, dict):
        model_status = ml_payload.get("model_status")
        t1 = ml_payload.get("t1")
        if isinstance(t1, dict) and target_time is not None \
                and t1.get("target_time") not in (None, target_time):
            t1 = None
    if t1 is None:
        model_status = None
        try:
            from core.otc_predict.tracker import latest_predictions
            rows = latest_predictions(asset, 20)
            for r in rows:
                if r.get("horizon") != 1:
                    continue
                if target_time is not None and r.get("target_time") != target_time:
                    continue
                t1 = {"prediction": r.get("prediction"),
                      "probability": r.get("probability"),
                      "emit": bool(r.get("emit")),
                      "target_time": r.get("target_time"),
                      "guard": None}
                break
        except Exception:
            t1 = None
    return t1, model_status


def _record(asset, signal, hour_utc, verdict, mult, layers, reason,
            conf, final_conf, final_signal):
    """Persist the gate verdict for the /api/verifier endpoints (never fatal)."""
    try:
        _record_verdict(asset, signal, hour_utc, verdict, mult, layers,
                        _short(reason), conf, final_conf, final_signal)
    except Exception:
        pass


def apply_joint_gate(result, asset, period, candles, ticks,
                     ml_payload=None, target_time=None):
    """Grade one CALL/PUT against the ML voice + 5-layer verifier.

    NEVER rejects (USER-2026-09-14): every directional signal keeps its
    direction; the voices only adjust confidence and are recorded so the
    UI / stats can show how each signal was graded.

    Returns a dict:
        rejected          — always False for directional signals
        verdict           — JOINT_PASS / JOINT_ADJUSTED / SKIP
        reason            — human-readable summary
        model_voice       — ML opinion used (or "no_model")
        verifier_voice    — 5-layer verdict details
        final_confidence  — confidence after all adjustments
    """
    signal = result.get("signal")
    if signal not in ("CALL", "PUT"):
        return {"rejected": False, "verdict": "SKIP",
                "reason": "not directional",
                "model_voice": None, "verifier_voice": None,
                "confidence_mult": None,
                "final_confidence": int(result.get("confidence") or 0)}

    conf = int(result.get("confidence") or 0)
    hour_utc = int(time.gmtime().tm_hour)
    source = result.get("signal_source") or (
        "ml_model" if result.get("strategy") == "ml_model_t1" else "strategy")
    voices = [f"src={source}"]

    # ── 1. MODEL VOICE ─────────────────────────────────────────────────────
    model = {"present": False, "state": "no_model", "direction": None,
             "emit": None, "guard_state": None, "model_status": None,
             "probability": None}
    ml_adj = 0
    try:
        t1, mstatus = _ml_voice(asset, ml_payload, target_time)
    except Exception as exc:
        # Fail-open: the source signal stands; record the error.
        model["state"] = "voice_error"
        voices.append(f"ML voice error: {type(exc).__name__}")
        t1, mstatus = None, None
    model["model_status"] = mstatus
    if t1 and t1.get("prediction") in ("CALL", "PUT"):
        model["present"] = True
        model["direction"] = t1["prediction"]
        model["probability"] = t1.get("probability")
        model["emit"] = bool(t1.get("emit"))
        if isinstance(t1.get("guard"), dict):
            model["guard_state"] = t1["guard"].get("state")
        if source == "ml_model":
            # The ML model IS the source — its own voice is not a second
            # opinion; just record it.
            model["state"] = "source"
            voices.append(f"ML({model['direction']}) SOURCE")
        elif mstatus == "provisional":
            # VERIFIED models only may agree or oppose — a provisional
            # (fast-train bootstrap) model abstains honestly.
            model["state"] = "abstain_provisional"
            voices.append("ML provisional — abstain")
        elif model["guard_state"] == "suspended":
            model["state"] = "suspended"
            ml_adj -= ML_SUSPENDED_PENALTY
            voices.append("ML edge-guard suspended pair (conf penalty)")
        elif model["emit"]:
            if model["direction"] == signal:
                model["state"] = "agree"
                ml_adj += ML_AGREE_BONUS
                voices.append(f"ML({model['direction']}) AGREE")
            else:
                model["state"] = "oppose"
                ml_adj -= ML_OPPOSE_PENALTY
                voices.append(f"ML({model['direction']}) OPPOSES "
                              f"{signal} (conf penalty)")
        else:
            model["state"] = "abstain_quality"
            voices.append("ML no-trade (quality) — abstain")

    # ── 2. VERIFIER VOICE (always runs; fail-open) ─────────────────────────
    ver = None
    v_verdict = "PASS"
    v_mult = 1.0
    veto_layers = []
    weaken_layers = []
    try:
        ver = verify_signal(result, candles, ticks, asset, hour_utc)
        v_verdict = (ver or {}).get("verdict", "PASS")
        v_mult = float((ver or {}).get("confidence_adjustment", 1.0) or 1.0)
        layers = (ver or {}).get("layers") or {}
        veto_layers = [l for l, r in layers.items()
                       if r.get("verdict") == "VETO"]
        weaken_layers = [l for l, r in layers.items()
                         if r.get("verdict") == "WEAKEN"]
        voices.append(f"verifier {v_verdict}")
    except Exception as exc:
        voices.append(f"verifier error (fail-open): {type(exc).__name__}")

    # ── confidence adjustment (never a direction change) ───────────────────
    final_conf = conf
    if veto_layers or v_verdict == "VETO":
        # ANY single layer VETO ⇒ strong honesty penalty, signal stands
        # (every-candle mandate; the UI shows the veto).
        _which = ", ".join(veto_layers) if veto_layers else "aggregate"
        voices.append(f"VETO in {_which} — conf x{VETO_MULT}")
        final_conf = int(round(final_conf * VETO_MULT))
    elif v_verdict == "WEAKEN":
        final_conf = int(round(final_conf * WEAKEN_MULT))
    elif v_verdict == "CONFIRM":
        final_conf = int(round(final_conf * max(v_mult, CONFIRM_MULT)))
    final_conf += ml_adj
    final_conf = max(MIN_CONFIDENCE_FLOOR, min(MAX_CONFIDENCE, final_conf))

    verdict = ("JOINT_PASS" if final_conf == conf else "JOINT_ADJUSTED")
    try:
        _record(asset, signal, hour_utc, v_verdict, v_mult,
                (ver or {}).get("layers") or {},
                (ver or {}).get("reason", ""), conf, final_conf, signal)
    except Exception:
        pass

    return {
        "rejected": False,
        "verdict": verdict,
        "reason": " + ".join(voices) + f" (conf {conf}→{final_conf})",
        "model_voice": model,
        "verifier_voice": ver,
        "confidence_mult": (v_mult if v_verdict in ("WEAKEN", "CONFIRM")
                            else None),
        "final_confidence": final_conf,
        "signal_source": source,
    }
