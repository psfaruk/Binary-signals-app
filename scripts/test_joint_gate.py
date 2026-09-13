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


# 1. HARD-CODED ON: env cannot disable the gate
os.environ["QX_JOINT_GATE"] = "0"
r = jg.apply_joint_gate(classic(), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT"), target_time=3460)
check("gate hard-coded ON (env off-switch ignored)",
      r["rejected"] is True and "opposes" in r["reason"])
os.environ.pop("QX_JOINT_GATE", None)

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

# 7. HARD RULE: one layer VETO rejects even when aggregate says WEAKEN
jg.verify_signal = stub_verifier(
    "WEAKEN", 0.5,
    layers={"L1_price_action": {"verdict": "VETO", "reason": "counter-trend"},
            "L3_tick_momentum": {"verdict": "CONFIRM", "reason": "aligned"}})
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("CALL"), target_time=3460)
check("any single layer VETO → reject (hard rule)",
      r["rejected"] is True and "VETO in L1_price_action" in r["reason"])

# 8. fallback + PASS without ML → reject
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS, None, target_time=3460)
check("fallback + PASS without ML → reject", r["rejected"] is True)

# 9. fallback + CONFIRM → pass
jg.verify_signal = stub_verifier("CONFIRM", 1.1)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS, None, target_time=3460)
check("fallback + CONFIRM → pass", r["rejected"] is False)

# 10. fallback + ML joint agreement + clean PASS → pass (the new volume path)
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS, ml("CALL"), target_time=3460)
check("fallback + ML agree + clean PASS → pass (ML joint reopen)",
      r["rejected"] is False and r["final_confidence"] == 52
      and r["model_voice"]["state"] == "agree")

# 11. fallback + ML agree but verifier WEAKEN → reject (no false signals)
jg.verify_signal = stub_verifier("WEAKEN", 0.5)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS, ml("CALL"), target_time=3460)
check("fallback + ML agree + WEAKEN → reject",
      r["rejected"] is True)

# 12. provisional model abstains — cannot reopen a fallback
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(
    classic("CALL", conf=52, strategy="confluence_v1_fallback", fallback=True),
    "EURUSD_otc", 60, CANDLES, TICKS,
    ml("CALL", model_status="provisional"), target_time=3460)
check("provisional ML abstains → fallback stays rejected",
      r["rejected"] is True
      and r["model_voice"]["state"] == "abstain_provisional")

# 13. WEAKEN halves confidence (non-fallback, ML agree)
jg.verify_signal = stub_verifier("WEAKEN", 0.5)
r = jg.apply_joint_gate(classic("CALL", conf=80), "EURUSD_otc", 60,
                        CANDLES, TICKS, ml("CALL"), target_time=3460)
check("WEAKEN → conf 80→40", r["rejected"] is False
      and r["final_confidence"] == 40)

# 14. verifier exception → fail-closed reject
def boom(*a, **k):
    raise RuntimeError("boom")
jg.verify_signal = boom
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        None, target_time=3460)
check("verifier exception → fail-closed reject", r["rejected"] is True)

# 15. target_time mismatch in payload → ML voice absent (verifier-only)
jg.verify_signal = stub_verifier("PASS", 1.0)
r = jg.apply_joint_gate(classic("CALL"), "EURUSD_otc", 60, CANDLES, TICKS,
                        ml("PUT", target_time=999999), target_time=3460)
check("payload target_time mismatch → abstain, verifier-only",
      r["rejected"] is False and r["model_voice"]["present"] is False)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
