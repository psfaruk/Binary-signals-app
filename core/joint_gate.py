"""
core/joint_gate.py — JOINT SIGNAL VERIFICATION GATE (2026-09-14, hard-coded ON)

USER REQUIREMENT (verbatim): "সব ML ও মডিউল ইঞ্জিন verify করবে — আরো হার্ড
চেক করে সিগন্যাল দেবে, false signal দেবে না, কিন্তু সিগন্যাল একটু বেশি
আসতে হবে।"

This gate is HARD-CODED ON. There is deliberately NO env off-switch:
every classic CALL/PUT must pass BOTH voices before it can reach the
UI / Telegram / webhooks.

  1. MODEL VOICE — the ML engine's frozen T+1 prediction for THIS exact
     candle (target_time match). VERIFIED models only; a provisional
     (fast-train bootstrap) model abstains.
       * emit=True + same direction   ⇒ AGREE — strong joint evidence
         (emit=True already implies calibrated prob ≥0.65 and the ML's
         own strategy second-voice agreement, so an agreeing fallback is
         a model × module × module triple confirmation)
       * emit=True + opposite direction ⇒ REJECT
       * edge-guard suspended           ⇒ REJECT (circuit breaker)
       * no model / emit=False (quality)⇒ abstain (verifier-only path)
  2. VERIFIER VOICE — the 5 real-time layers (price action, wick
     rejection, tick momentum, key level, historical pattern).
       HARD RULE: ANY single layer VETO ⇒ REJECT, even if the aggregate
       says otherwise. WEAKEN ⇒ confidence ×0.5. CONFIRM ⇒ ×1.1 (cap 95).
  3. FALLBACK POLICY (hard-coded) — classic every-candle fallback
     signals (signal_quality FALLBACK / strategy "*_fallback") are
     coin-flip by construction and emit ONLY with EITHER:
       * verifier CONFIRM, or
       * ML JOINT AGREEMENT: verified ML emit=True with the SAME
         direction AND the verifier is a clean PASS (no VETO, no WEAKEN
         layer).
     This is the "harder check AND more signals" path: the fallback
     reopens only where the ML engine independently confirms it.

Fail-CLOSED: any gate exception ⇒ REJECT (an unverified signal must
never ship).

Called from feed.py::_run_eoc, immediately after the classic engine
returns, before stream.prediction is set.
"""
import os
import time

from core.signal_verifier import verify_signal, _record_verdict

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

    HARD-CODED ON — no env off-switch (see module docstring).

    Returns a dict:
        rejected          — True ⇒ caller must convert to NEUTRAL
        reason / verdict  — human-readable summary
        model_voice       — ML opinion used (or "no_model")
        verifier_voice    — 5-layer verdict details
        final_confidence  — confidence after verifier adjustment
    """
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
             "emit": None, "guard_state": None, "model_status": None,
             "probability": None}
    try:
        t1, mstatus = _ml_voice(asset, ml_payload, target_time)
    except Exception as exc:
        return _reject(asset, signal, hour_utc, conf, model, None,
                       f"model voice failed: {type(exc).__name__}")
    model["model_status"] = mstatus
    if t1 and t1.get("prediction") in ("CALL", "PUT"):
        model["present"] = True
        model["direction"] = t1["prediction"]
        model["probability"] = t1.get("probability")
        model["emit"] = bool(t1.get("emit"))
        if isinstance(t1.get("guard"), dict):
            model["guard_state"] = t1["guard"].get("state")
        if mstatus == "provisional":
            # VERIFIED models only may confirm or veto — a provisional
            # (fast-train bootstrap) model abstains honestly.
            model["state"] = "abstain_provisional"
            voices.append("ML provisional — abstain")
        elif model["guard_state"] == "suspended":
            return _reject(asset, signal, hour_utc, conf, model, None,
                           f"ML edge-guard suspended this pair "
                           f"(state={model['guard_state']}) — no trade")
        elif model["emit"]:
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
    layers = (ver or {}).get("layers") or {}
    veto_layers = [l for l, r in layers.items()
                   if r.get("verdict") == "VETO"]
    weaken_layers = [l for l, r in layers.items()
                     if r.get("verdict") == "WEAKEN"]
    voices.append(f"verifier {v_verdict}")

    # HARD RULE (2026-09-14): ANY single layer VETO ⇒ reject, even when
    # the aggregate verdict softened it to WEAKEN via a confirming layer.
    # The aggregate VETO itself also always rejects.
    if veto_layers or v_verdict == "VETO":
        _which = ", ".join(veto_layers) if veto_layers else "aggregate"
        return _reject(asset, signal, hour_utc, conf, model, ver,
                       f"5-layer verifier VETO in {_which} — "
                       f"{_short((ver or {}).get('reason'))}")

    # ── 3. FALLBACK POLICY (hard-coded) ───────────────────────────────────
    if is_fallback:
        ml_joint = (model["state"] == "agree")
        confirmed = (v_verdict == "CONFIRM")
        clean_pass = (v_verdict == "PASS" and not weaken_layers)
        if not (confirmed or (ml_joint and clean_pass)):
            return _reject(asset, signal, hour_utc, conf, model, ver,
                           f"fallback signal (strategy="
                           f"{result.get('strategy')}) without verifier "
                           f"CONFIRM or ML joint agreement — coin-flip by "
                           f"construction")
        if ml_joint and not confirmed:
            voices.append("FALLBACK reopened by ML joint agreement")

    # ── confidence adjustment ──────────────────────────────────────────────
    final_conf = conf
    if v_verdict == "WEAKEN":
        final_conf = int(round(conf * v_mult))
    elif v_verdict == "CONFIRM":
        final_conf = min(MAX_CONFIDENCE, int(round(conf * max(v_mult, 1.0))))
    final_conf = max(0, min(MAX_CONFIDENCE, final_conf))

    try:
        _record_verdict(asset, signal, hour_utc, v_verdict, v_mult,
                        layers, (ver or {}).get("reason", ""), conf,
                        final_conf, signal)
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
