"""Offline unit tests for core/joint_gate.py — no DB, no live feed, no network.

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


def classic(signal="CALL", conf=70, strategy="confluence_v1",
            quality="HIGH", fallback=False):
    r = {"signal": signal, "confidence": conf, "strength": "MEDIUM",
         "strategy": strategy, "signal_quality": quality}
    if fallback:
        r.update({"fallback": True, "signal_quality": "FALLBACK"})
    return r


def ml(signal="CALL", emit=True, guard_state=None, target_time=3460):
    t1 = {"prediction": signal, "probability": 0.72, "emit": emit,
          "target_time": target_time}
    if guard_state:
        t1["guard"] = {"state": guard_state}
    return {"asset": "EURUSD_otc", "period": 60, "t1": t1}


def stub_verifier(verdict, mult=1.0):
    def _stub(prediction, candles, ticks, asset, hour_utc):
        return {"verdict": verdict, "confidence_adjustment": mult,
                "layers": {"L1_price_action": {"verdict": "PASS",
                                               "reason": "stub"}},
                "reason": f"stub {verdict}"}
    return _stub


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# 1. gate disabled → pass-through (legacy behavior)
os.environ["QX_JOINT_GATE"] = "0"
r = jg.apply_joint_gate(classic(), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT"), target_time=3460)
check("gate disabled passes through", r["rejected"] is False)
os.environ["QX_JOINT_GATE"] = "1"

# 2. ML opposes → reject
jg.verify_signal = stub_verifier("PASS")
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT"), target_time=3460)
check("ML opposes → reject", r["rejected"] is True
      and "opposes" in r["reason"])

# 3. ML agrees + CONFIRM → pass, confidence boosted ×1.1
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(classic("CALL", conf=70), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("ML agree + CONFIRM → pass, conf 70→77",
      r["rejected"] is False and r["final_confidence"] == 77)

# 4. guard-suspended pair → hard reject even when directions agree
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("CALL", emit=False, guard_state="suspended"),
                        target_time=3460)
check("guard suspended → reject", r["rejected"] is True
      and "suspended" in r["reason"])

# 5. ML emit=False on quality (not suspended) → voice abstains, verifier decides
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT", emit=False), target_time=3460)
check("ML quality no-trade abstains → verifier-only path",
      r["rejected"] is False and r["model_voice"]["state"] == "abstain_quality")

# 6. no ML voice → verifier-only; VETO kills
jg.verify_signal = stub_verifier("VETO", 0.0)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        None, target_time=3460)
check("verifier VETO rejects", r["rejected"] is True)

# 7. fallback bar: PASS → reject; CONFIRM → pass
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS, None, target_time=3460)
check("fallback + PASS → reject", r["rejected"] is True)
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS, None, target_time=3460)
check("fallback + CONFIRM → pass", r["rejected"] is False)

# 8. WEAKEN halves confidence
jg.verify_signal = stub_verifier("WEAKEN", 0.5)
r = jg.apply_joint_gate(classic("CALL", conf=80), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("WEAKEN → conf 80→40", r["rejected"] is False
      and r["final_confidence"] == 40)

# 9. verifier exception → fail-closed reject
def boom(*a, **k):
    raise RuntimeError("boom")
jg.verify_signal = boom
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        None, target_time=3460)
check("verifier exception → fail-closed reject", r["rejected"] is True)

# 10. target_time mismatch in payload → ML voice falls back to DB/absent
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT", target_time=999999), target_time=3460)
check("payload target_time mismatch → abstain, verifier-only",
      r["rejected"] is False and r["model_voice"]["present"] is False)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
