"""
core/joint_gate.py — JOINT SIGNAL VERIFICATION GATE (2026-09-13)

USER REQUIREMENT (verbatim): "মডেল ও মডিউল ইঞ্জিন একসাথে কাজ করুক —
প্রতিটি সিগন্যাল verify হয়ে তবেই emit হবে।"

Why this exists
---------------
The app had two independent prediction engines that never consulted each
other, plus a 5-layer verifier that was written but never called:

  * CLASSIC engine (feed._run_eoc → engines → blender → confluence)
    produced a CALL/PUT on EVERY candle — 98% of live signals via the
    every-candle fallback, whose pre-fix tie-break chain measured
    32-45.5% win (anti-predictive). Fallback still forces a direction
    today, so near-coin-flip signals reach the UI.
  * ML engine (core/otc_predict) produced its own T+1/T+2 predictions
    with honest emit flags, cross-checked against the strategy modules
    but never against the classic engine's final signal.
  * core/signal_verifier.verify_signal existed with ZERO call sites.

This module is the single choke point BOTH voices must pass before a
classic CALL/PUT reaches the UI / Telegram / webhooks:

  1. MODEL VOICE  — the ML engine's frozen T+1 prediction for THIS exact
     candle (target_time match). Verified model + emit=True ⇒ directions
     must AGREE, else REJECT. A guard-suspended pair ⇒ hard REJECT
     (live edge circuit breaker). No model / emit=False on quality
     grounds ⇒ the voice abstains (verifier-only path).
  2. VERIFIER VOICE — 5 real-time layers (price action, wick rejection,
     tick momentum, key level, historical pattern):
       VETO ⇒ REJECT | WEAKEN ⇒ confidence ×0.5 | CONFIRM ⇒ ×1.1 (cap 95)
  3. FALLBACK BAR — classic fallback signals (signal_quality FALLBACK /
     strategy "*_fallback") are coin-flip by construction and may emit
     ONLY with a verifier CONFIRM; otherwise REJECT.

Fail-CLOSED: any gate exception ⇒ REJECT (an unverified signal must
never ship). Disable the whole gate with env QX_JOINT_GATE=0.

Called from feed.py::_run_eoc, immediately after the classic engine
returns, before stream.prediction is set.
"""
import os
import time

from core.signal_verifier import verify_signal, _record_verdict

GATE_ENV = "QX_JOINT_GATE"                # "1" = enabled (default)
STRICT_FALLBACK_ENV = "QX_JOINT_GATE_FALLBACK_STRICT"  # "1" = default
MAX_CONFIDENCE = 95


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
    """
    t1 = None
    if isinstance(ml_payload, dict):
        t1 = ml_payload.get("t1")
        if isinstance(t1, dict) and target_time is not None \
                and t1.get("target_time") not in (None, target_time):
            t1 = None
    if t1 is None:
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
    return t1


def _reject(asset, signal, hour_utc, conf, model, ver, reason):
    """Build a REJECT verdict and record it for the /api/verifier endpoints."""
    layers = (ver or {}).get("layers") or {}
    try:
        _record_verdict(asset, signal, hour_utc, "VETO", 0.0, layers,
                        _short(reason), conf, 0, "NEUTRAL")
    except Exception:
        pass
    return {
        "rejected": True,
        "verdict": "JOINT_REJECT",
        "reason": reason,
        "model_voice": model,
        "verifier_voice": ver,
        "confidence_mult": None,
        "final_confidence": 0,
        "fallback": bool(model and model.get("fallback")),
    }


def apply_joint_gate(result, asset, period, candles, ticks,
                     ml_payload=None, target_time=None):
    """Verify one classic CALL/PUT against the ML voice + 5-layer verifier.

    Args:
        result: classic engine prediction dict (signal, confidence,
                strategy, signal_quality, fallback, ...).
        asset: pair name (e.g. "EURUSD_otc").
        period: candle period in seconds (for target_time alignment).
        candles: closed candle dicts (last candle = the one just closed).
        ticks: tick prices of the just-closed candle.
        ml_payload: live payload returned by predictor.on_candle_closed
                    for this close (or None for non-close recomputes).
        target_time: open time of the NEW candle = ML T+1 target.

    Returns a dict:
        rejected          — True ⇒ caller must convert to NEUTRAL
        reason / verdict  — human-readable summary
        model_voice       — ML opinion used (or "no_model")
        verifier_voice    — 5-layer verdict details
        final_confidence  — confidence after verifier adjustment
    """
    if os.environ.get(GATE_ENV, "1") != "1":
        return {"rejected": False, "verdict": "DISABLED",
                "reason": "joint gate disabled (QX_JOINT_GATE=0)",
                "model_voice": None, "verifier_voice": None,
                "confidence_mult": None,
                "final_confidence": int(result.get("confidence") or 0),
                "fallback": bool(result.get("fallback"))}

    signal = result.get("signal")
    if signal not in ("CALL", "PUT"):
        return {"rejected": False, "verdict": "SKIP",
                "reason": "not directional",
                "model_voice": None, "verifier_voice": None,
                "confidence_mult": None,
                "final_confidence": int(result.get("confidence") or 0),
                "fallback": False}

    conf = int(result.get("confidence") or 0)
    hour_utc = int(time.gmtime().tm_hour)
    is_fallback = (result.get("signal_quality") == "FALLBACK"
                   or bool(result.get("fallback"))
                   or str(result.get("strategy", "")).endswith("_fallback"))
    voices = []

    # ── 1. MODEL VOICE ─────────────────────────────────────────────────────
    model = {"present": False, "state": "no_model", "direction": None,
             "emit": None, "guard_state": None}
    try:
        t1 = _ml_voice(asset, ml_payload, target_time)
    except Exception as exc:
        return _reject(asset, signal, hour_utc, conf, model, None,
                       f"model voice failed: {type(exc).__name__}")
    if t1 and t1.get("prediction") in ("CALL", "PUT"):
        model["present"] = True
        model["direction"] = t1["prediction"]
        model["emit"] = bool(t1.get("emit"))
        if isinstance(t1.get("guard"), dict):
            model["guard_state"] = t1["guard"].get("state")
        if model["guard_state"] == "suspended":
            return _reject(asset, signal, hour_utc, conf, model, None,
                           f"ML edge-guard suspended this pair "
                           f"(state={model['guard_state']}) — no trade")
        if model["emit"]:
            if model["direction"] != signal:
                return _reject(asset, signal, hour_utc, conf, model, None,
                               f"ML T+1 says {model['direction']} — opposes "
                               f"classic {signal} (joint agreement required)")
            model["state"] = "agree"
            voices.append(f"ML({model['direction']}) AGREE")
        else:
            model["state"] = "abstain_quality"
            voices.append("ML no-trade (quality) — abstain")

    # ── 2. VERIFIER VOICE (always runs — fail-closed) ─────────────────────
    try:
        ver = verify_signal(result, candles, ticks, asset, hour_utc)
    except Exception as exc:
        return _reject(asset, signal, hour_utc, conf, model, None,
                       f"verifier failed: {type(exc).__name__} "
                       f"(fail-closed)")
    v_verdict = (ver or {}).get("verdict", "PASS")
    v_mult = float((ver or {}).get("confidence_adjustment", 1.0) or 1.0)
    voices.append(f"verifier {v_verdict}")

    if v_verdict == "VETO":
        return _reject(asset, signal, hour_utc, conf, model, ver,
                       f"5-layer verifier VETO — {_short((ver or {}).get('reason'))}")

    # ── 3. FALLBACK BAR ────────────────────────────────────────────────────
    if is_fallback and os.environ.get(STRICT_FALLBACK_ENV, "1") == "1":
        if v_verdict != "CONFIRM":
            return _reject(asset, signal, hour_utc, conf, model, ver,
                           f"fallback signal (strategy="
                           f"{result.get('strategy')}) without verifier "
                           f"CONFIRM — coin-flip by construction")

    # ── confidence adjustment ──────────────────────────────────────────────
    final_conf = conf
    if v_verdict == "WEAKEN":
        final_conf = int(round(conf * v_mult))
    elif v_verdict == "CONFIRM":
        final_conf = min(MAX_CONFIDENCE, int(round(conf * max(v_mult, 1.0))))
    final_conf = max(0, min(MAX_CONFIDENCE, final_conf))

    try:
        _record_verdict(asset, signal, hour_utc, v_verdict, v_mult,
                        (ver or {}).get("layers") or {},
                        (ver or {}).get("reason", ""), conf, final_conf,
                        signal)
    except Exception:
        pass

    return {
        "rejected": False,
        "verdict": "JOINT_PASS",
        "reason": " + ".join(voices) + f" (conf {conf}→{final_conf})",
        "model_voice": model,
        "verifier_voice": ver,
        "confidence_mult": (v_mult if v_verdict in ("WEAKEN", "CONFIRM")
                            else None),
        "final_confidence": final_conf,
        "fallback": is_fallback,
    }
