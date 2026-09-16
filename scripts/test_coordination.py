#!/usr/bin/env python3
"""scripts/test_coordination.py — COORDINATION-MS unit tests + latency proof.

COORDINATION-MS (2026-09-16): core/coordination.py merges the four voices
(running-candle eye × module signal × model engine × runconf) on every tick.
This script verifies the state machine deterministically, then benchmarks the
compute time — the USER's requirement is millisecond-level data, so the test
asserts the coordination itself computes in < 1 ms (target: < 100 µs).

Run:  python scripts/test_coordination.py
Exit: 0 = all pass, 1 = failure.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.coordination import (
    compute_coordination, EYE_MIN_STRENGTH, ALIGNED_MIN, PARTIAL_MIN,
    EYE_WEIGHT, MODEL_WEIGHT, RUNCONF_WEIGHT, PHASE_MULT,
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def eye_stub(phase="LAST10", direction="CALL", strength=70, ready=True,
             ticks=120):
    """A minimal live_eye-shaped dict for the coordinator input."""
    return {
        "ready": ready,
        "phase": phase,
        "tick_count": ticks,
        "eye_direction": direction,
        "eye_strength": strength,
        "eye_reasons": [],
    }


def pred(direction="CALL", conf=62, strategy="strategy_engine"):
    return {"signal": direction, "confidence": conf, "strength": "MEDIUM",
            "strategy": strategy}


def model(direction="CALL", prob=0.61, state="present", emit=True):
    return {"direction": direction, "probability": prob,
            "state": state, "emit": emit}


# ── 1. No prediction → NO_SIGNAL ─────────────────────────────────────────────
print("1) NO SIGNAL state")
c = compute_coordination(eye_stub(), None, model(), None)
check("state is NO_SIGNAL", c["state"] == "NO_SIGNAL", c["state"])
check("anchor chip present", any(
    v["name"] == "signal" for v in c["voices"]))
check("summary is Bengali", "সিগন্যাল নেই" in c["summary_bn"])

# ── 2. Prediction but no voices → WAITING ───────────────────────────────────
print("2) WAITING state")
c = compute_coordination(eye_stub(ready=False, ticks=3), pred(), None, None)
check("state is WAITING", c["state"] == "WAITING", c["state"])
check("alignment None", c["alignment"] is None)
check("eye abstains (few ticks)",
      any(v["name"] == "eye" and v["role"] == "abstain"
          for v in c["voices"]))

# ── 3. All voices agree at LAST10 → ALIGNED, 100% ───────────────────────────
print("3) ALIGNED state (all voices agree)")
c = compute_coordination(eye_stub(phase="LAST10", direction="CALL",
                                  strength=75),
                         pred("CALL"), model("CALL", 0.61), "CONFIRMING")
check("state is ALIGNED_CALL", c["state"] == "ALIGNED_CALL", c["state"])
check("alignment 100", c["alignment"] == 100, c["alignment"])
check("summary mentions মিলেছে", "মিলেছে" in c["summary_bn"])
_voice_names = [v["name"] for v in c["voices"]]
check("four voices emitted", _voice_names == ["signal", "eye", "model",
                                              "runconf"], _voice_names)

# ── 4. Eye + runconf oppose → CONFLICT ──────────────────────────────────────
print("4) CONFLICT state (live evidence opposes)")
c = compute_coordination(eye_stub(direction="PUT", strength=72),
                         pred("CALL"), None, "OPPOSING")
# weights: eye 45×1.25 (LAST10) + runconf 25 = 81.25; agreeing 0 → 0%
check("state is CONFLICT", c["state"] == "CONFLICT", c["state"])
check("alignment 0", c["alignment"] == 0, c["alignment"])
_eye_v = next(v for v in c["voices"] if v["name"] == "eye")
check("eye marked disagree", _eye_v["agree"] is False)
check("summary warns", "বিপরীতে" in c["summary_bn"])

# ── 5. Model alone agrees, eye abstains → partial support ───────────────────
print("5) Eye abstains (strength < threshold)")
c = compute_coordination(
    eye_stub(strength=EYE_MIN_STRENGTH - 1, direction="PUT"),
    pred("CALL"), model("CALL"), "CONFIRMING")
check("eye voice abstains", any(
    v["name"] == "eye" and v["role"] == "abstain" for v in c["voices"]))
# agreeing: model 30 + runconf 25 = 55 of present 55 → 100
check("model+runconf still align 100", c["alignment"] == 100,
      c["alignment"])
check("state ALIGNED despite weak eye", c["state"] == "ALIGNED_CALL")

# ── 6. ML-source signal → model voice shown as source, not scored ──────────
print("6) ML-source signal excludes model from scoring")
c = compute_coordination(
    eye_stub(direction="CALL", strength=80),
    pred("CALL", strategy="ml_model_t1"), model("CALL", 0.58), "CONFIRMING")
_mv = next(v for v in c["voices"] if v["name"] == "model")
check("model role source", _mv["role"] == "source", _mv)
check("model agree is None", _mv["agree"] is None)
# eye 56.25 + runconf 25 = 81.25 all agree → 100
check("alignment still 100", c["alignment"] == 100, c["alignment"])

# ── 7. Provisional model abstains ───────────────────────────────────────────
print("7) Provisional model abstains")
c = compute_coordination(
    eye_stub(direction="CALL", strength=80), pred("CALL"),
    model(None, state="abstain_provisional"), "CONFIRMING")
check("model voice abstains", any(
    v["name"] == "model" and v["role"] == "abstain" for v in c["voices"]))

# ── 8. Phase weighting is monotonic (LAST10 > EARLY for same eye) ───────────
print("8) Phase weighting")
_c_early = compute_coordination(eye_stub(phase="EARLY", strength=70),
                               pred("CALL"), None, None)
_c_last10 = compute_coordination(eye_stub(phase="LAST10", strength=70),
                                 pred("CALL"), None, None)
# both should be ALIGNED (only voice), but the recorded eye weight differs
_w_early = next(v for v in _c_early["voices"] if v["name"] == "eye")["weight"]
_w_last10 = next(v for v in _c_last10["voices"] if v["name"] == "eye")["weight"]
check("LAST10 eye weight > EARLY", _w_last10 > _w_early,
      f"{_w_early} vs {_w_last10}")
check("weights match constants",
      abs(_w_last10 - round(EYE_WEIGHT * PHASE_MULT["LAST10"], 1)) < 0.01,
      _w_last10)

# ── 9. Alignment math: 2 of 3 weighted voices agree ─────────────────────────
print("9) Weighted alignment math")
# eye CALL (LAST10 ×1.25 = 56.25) agree; model PUT (30) disagree; runconf
# CONFIRMING (25) agree → (56.25+25)/(56.25+30+25) = 81.25/111.25 = 73%
c = compute_coordination(eye_stub(direction="CALL", strength=70),
                         pred("CALL"), model("PUT", 0.55), "CONFIRMING")
_expected = int(round(100 * (56.25 + 25) / (56.25 + 30 + 25)))
check("alignment matches weighted math", c["alignment"] == _expected,
      f"{c['alignment']} vs {_expected}")
check("73% >= ALIGNED_MIN → ALIGNED", c["state"] == "ALIGNED_CALL")

# model PUT + runconf OPPOSING vs eye CALL:
c = compute_coordination(eye_stub(direction="CALL", strength=70),
                         pred("CALL"), model("PUT", 0.55), "OPPOSING")
_expected = int(round(100 * 56.25 / 111.25))   # 51 → PARTIAL
check("51% falls to PARTIAL", c["state"] == "PARTIAL",
      f"{c['state']} (alignment {c['alignment']})")

# ── 10. ms-proof fields ─────────────────────────────────────────────────────
print("10) Millisecond proof fields")
c = compute_coordination(eye_stub(), pred(), model(), "CONFIRMING")
check("server_ms present", isinstance(c["server_ms"], int)
      and c["server_ms"] > 1_600_000_000_000)
check("compute_us present", isinstance(c["compute_us"], int)
      and c["compute_us"] >= 0)

# ── 11. Latency benchmark — the USER's ms requirement ───────────────────────
print("11) Latency benchmark (10,000 calls)")
_eye = eye_stub()
_pred = pred()
_model = model()
_rc = "CONFIRMING"
N = 10_000
_t0 = time.perf_counter()
for _ in range(N):
    compute_coordination(_eye, _pred, _model, _rc)
_dt = time.perf_counter() - _t0
_avg_us = _dt / N * 1_000_000
print(f"     avg = {_avg_us:.1f} µs/call over {N} calls")
check("avg compute < 1000 µs (1 ms)", _avg_us < 1000.0, f"{_avg_us:.1f}µs")
check("avg compute < 100 µs (target)", _avg_us < 100.0, f"{_avg_us:.1f}µs")

# Also benchmark with a FULL live_eye anatomy (worst-case dict size)
try:
    from core.tick_eye import live_eye
    import random
    random.seed(7)
    _ticks = [1.1000]
    for _ in range(400):
        _ticks.append(_ticks[-1] + random.choice((-1, 0, 0, 1)) * 1e-4)
    _full_eye = live_eye(_ticks, 1.1000, 60, time.time() - 45)
    _t0 = time.perf_counter()
    for _ in range(1000):
        compute_coordination(_full_eye, _pred, _model, _rc)
    _full_us = (time.perf_counter() - _t0) / 1000 * 1_000_000
    print(f"     full-anatomy avg = {_full_us:.1f} µs/call")
    check("full-anatomy compute < 100 µs", _full_us < 100.0,
          f"{_full_us:.1f}µs")
except ImportError:
    print("     (live_eye unavailable — skipped full-anatomy bench)")

# ── Result ──────────────────────────────────────────────────────────────────
print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
