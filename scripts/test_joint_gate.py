"""Offline unit tests for core/joint_gate.py — no DB, no live feed, no network.

UPDATED (2026-09-14) for the ANY-THEORY + ML-HAND-OFF contract:

  USER: "প্রত্যেক ক্যান্ডেল এ সিগন্যাল প্রধান করতে হবে, কিন্তু fallback
  signals দেওয়া যাবে না। ... যে কোনো একটি পাস হলেই সিগন্যাল দিবে। মডিউল
  ইঞ্জিন থেকে সিগন্যাল আসলো না — ML model থেকে সিগন্যাল টি আসবে।"

The gate NEVER rejects a directional signal anymore (rejected is always
False) — it GRADES it: the ML voice and the 5-layer verifier adjust
confidence and are recorded. Banned fallback strategies no longer exist
upstream, and the gate does not re-open them.

Run:  py scripts/test_joint_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.joint_gate as jg

CANDLES = [
    {"time": 1000 + i * 60,
     "open": 1.1000 + i * 0.0001,
     "high": 1.1000 + i * 0.0001 + 0.0002,
     "low": 1.1000 + i * 0.0001 - 0.0002,
     "close": 1.1000 + i * 0.0001 + 0.0001}
    for i in range(40)
]
TICKS = [1.0999 + i * 0.00005 for i in range(30)]


def classic(signal="CALL", conf=70, strategy="confluence_v1_any",
            quality="MEDIUM", source="strategy"):
    return {"signal": signal, "confidence": conf, "strength": "MEDIUM",
            "strategy": strategy, "signal_quality": quality,
            "signal_source": source}


def ml(signal="CALL", emit=True, guard_state=None, target_time=3460,
       model_status="verified"):
    t1 = {"prediction": signal, "probability": 0.72, "emit": emit,
          "target_time": target_time}
    if guard_state:
        t1["guard"] = {"state": guard_state}
    return {"asset": "EURUSD_otc", "period": 60,
            "model_status": model_status, "t1": t1}


def stub_verifier(verdict, mult=1.0, layers=None):
    def _stub(prediction, candles, ticks, asset, hour_utc):
        return {"verdict": verdict, "confidence_adjustment": mult,
                "layers": layers or {"L1_price_action":
                                    {"verdict": "PASS", "reason": "stub"}},
                "reason": f"stub {verdict}"}
    return _stub


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# 1. env cannot disable grading — and grading never rejects
os.environ["QX_JOINT_GATE"] = "0"
jg.verify_signal = stub_verifier("PASS")
r = jg.apply_joint_gate(classic(), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT"), target_time=3460)
check("gate always grades; never rejects (env off-switch meaningless)",
      r["rejected"] is False and r["verdict"] in ("JOINT_PASS", "JOINT_ADJUSTED"))
os.environ.pop("QX_JOINT_GATE", None)

# 2. ML opposes → signal STANDS with a confidence penalty (-5)
jg.verify_signal = stub_verifier("PASS")
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("PUT"), target_time=3460)
check("ML opposes → no reject; conf 70→65 (penalty)",
      r["rejected"] is False and r["model_voice"]["state"] == "oppose"
      and r["final_confidence"] == 65)

# 3. ML agrees + CONFIRM → confidence boosted ×1.1 + agree bonus
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("ML agree + CONFIRM → conf 70→(77→80 capped 95)",
      r["rejected"] is False and r["final_confidence"] >= 77
      and r["model_voice"]["state"] == "agree")

# 4. guard-suspended pair → penalty, signal STILL ships (every-candle)
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS,
                        ml("CALL", emit=False, guard_state="suspended"),
                        target_time=3460)
check("guard suspended → penalty applied, no reject",
      r["rejected"] is False and r["model_voice"]["state"] == "suspended"
      and r["final_confidence"] < 70)

# 5. ML emit=False on quality (not suspended) → voice abstains
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT", emit=False), target_time=3460)
check("ML quality no-trade abstains → verifier-only path",
      r["rejected"] is False and r["model_voice"]["state"] == "abstain_quality")

# 6. verifier VETO → strong honesty penalty (×0.55), signal stands
jg.verify_signal = stub_verifier("VETO", 0.0)
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS, None, target_time=3460)
check("verifier VETO → conf 70→38 (x0.55, banker-rounded), no reject",
      r["rejected"] is False and r["final_confidence"] == 38)

# 7. any single layer VETO beats aggregate WEAKEN → ×0.55 penalty
jg.verify_signal = stub_verifier(
    "WEAKEN", 0.5,
    layers={"L1_price_action": {"verdict": "VETO", "reason": "counter-trend"},
            "L3_tick_momentum": {"verdict": "CONFIRM", "reason": "aligned"}})
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("single layer VETO → x0.55 + ML agree bonus, reason names the layer",
      r["rejected"] is False and r["final_confidence"] == 41
      and "VETO in L1_price_action" in r["reason"])

# 8. ML-source signal: the model IS the source — its voice recorded as such
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(
    classic("CALL", conf=62, strategy="ml_model_t1", source="ml_model"),
    "EURUSD_otc", 60, CANDLES, TICKS, ml("CALL"), target_time=3460)
check("ML-source signal → model voice 'source', no self-penalty",
      r["rejected"] is False and r["model_voice"]["state"] == "source"
      and r["final_confidence"] == 62)

# 9. WEAKEN scales confidence ×0.75
jg.verify_signal = stub_verifier("WEAKEN", 0.5)
r = jg.apply_joint_gate(classic("CALL", conf=80), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("WEAKEN → conf 80→63 (x0.75 + ML agree bonus)", r["rejected"] is False
      and r["final_confidence"] == 63)

# 10. verifier exception → fail-open: the source signal stands
def boom(*a, **k):
    raise RuntimeError("boom")
jg.verify_signal = boom
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS, None, target_time=3460)
check("verifier exception → fail-open, conf unchanged",
      r["rejected"] is False and r["final_confidence"] == 70
      and "fail-open" in r["reason"])

# 11. target_time mismatch in payload → ML voice absent (verifier-only)
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT", target_time=999999), target_time=3460)
check("payload target_time mismatch → abstain, verifier-only",
      r["rejected"] is False and r["model_voice"]["present"] is False)

# 12. non-directional input → SKIP (unchanged semantics)
r = jg.apply_joint_gate({"signal": "NEUTRAL", "confidence": 0},
                        "EURUSD_otc", 60, CANDLES, TICKS, None)
check("NEUTRAL input → SKIP verdict", r["rejected"] is False
      and r["verdict"] == "SKIP")

# 13. provisional model abstains honestly (no agree/oppose voice)
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS,
                        ml("CALL", model_status="provisional"),
                        target_time=3460)
check("provisional ML → abstain_provisional, conf unchanged",
      r["rejected"] is False
      and r["model_voice"]["state"] == "abstain_provisional"
      and r["final_confidence"] == 70)

# 14. confidence floor: heavy penalties never go below MIN_CONFIDENCE_FLOOR
jg.verify_signal = stub_verifier("VETO", 0.0)
r = jg.apply_joint_gate(classic("CALL", conf=40), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("PUT"), target_time=3460)
check("floor respected under stacked penalties (≥30)",
      r["rejected"] is False and r["final_confidence"] >= jg.MIN_CONFIDENCE_FLOOR)

# 15. confidence cap: never above MAX_CONFIDENCE
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(classic("CALL", conf=92), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("cap respected (≤95)", r["rejected"] is False
      and r["final_confidence"] <= jg.MAX_CONFIDENCE)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
