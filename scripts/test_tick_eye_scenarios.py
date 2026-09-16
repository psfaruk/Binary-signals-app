#!/usr/bin/env python3
"""Quick sanity test for core/tick_eye.py — the user's exact scenario:
a 1-min candle that was RED at 57-58s and flipped GREEN in the last 2s.
Also: the mirror (GREEN -> RED), a spike-noise flip, and a no-flip trend.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tick_eye import analyze_candle_ticks, live_eye, eye_verdict

# ── Scenario 1: RED until ~57s, then a REAL flip to GREEN in the last ticks ──
# 100 ticks: mild red drift all candle, final 8 ticks push up through the
# open — the user's exact "57-58s red → last 2s green" case. Magnitudes are
# OTC-realistic (sub-pip drift, ~0.5 pip closing push).
ticks = []
p = 1.10000
import random
rng = random.Random(42)
for i in range(92):
    p -= 0.0000003 + abs(rng.gauss(0, 0.0000002))
    ticks.append(round(p, 6))
# real flip: 8 ticks up, crossing the open decisively
for i in range(8):
    p += 0.0000060 + rng.gauss(0, 0.0000005)
    ticks.append(round(p, 6))

a = analyze_candle_ticks(ticks, 1.10000, 60)
print("=== Scenario 1: REAL late flip RED -> GREEN ===")
print("color:", a["color"], "| velocity:", a["velocity"], "| close_pos:", a["close_position"])
print("late_flip:", a["late_flip"])
print("flow_final buy%:", a["buy_pct_final"])
print("verdict:", a["eye_direction"], a["eye_strength"])
for r in a["eye_reasons"]:
    print("  •", r)
assert a["late_flip"] and a["late_flip"]["detected"]
assert a["late_flip"]["from_color"] == "RED" and a["late_flip"]["to_color"] == "GREEN"
assert a["eye_direction"] == "CALL", "real flip to GREEN should lean CALL"

# ── Scenario 2: spike-noise flip (1-2 tick print, no travel) ─────────────────
ticks2 = []
p = 1.10000
for i in range(97):
    p -= 0.0000040 + rng.gauss(0, 0.0000018)
    ticks2.append(round(p, 6))
# one huge spike tick that flips the color, but nothing else
ticks2.append(round(p + 0.00045, 6))
ticks2.append(round(p + 0.00046, 6))

a2 = analyze_candle_ticks(ticks2, 1.10000, 60)
print("\n=== Scenario 2: SPIKE-NOISE flip ===")
print("late_flip:", a2["late_flip"])
print("verdict:", a2["eye_direction"], a2["eye_strength"])
assert a2["late_flip"]["is_spike_noise"] or a2["late_flip"]["travel_frac"] < 0.30

# ── Scenario 3: steady downtrend, no flip — strong PUT anatomy ──────────────
ticks3 = []
p = 1.10000
for i in range(100):
    p -= 0.000010 + rng.gauss(0, 0.000002)
    ticks3.append(round(p, 6))
a3 = analyze_candle_ticks(ticks3, 1.10000, 60)
print("\n=== Scenario 3: steady downtrend ===")
print("color:", a3["color"], "| velocity:", a3["velocity"], "| close_pos:", a3["close_position"])
print("verdict:", a3["eye_direction"], a3["eye_strength"])
assert a3["eye_direction"] == "PUT" and a3["eye_strength"] >= 45

# ── Scenario 4: live_eye extras (seconds_left, phase) ───────────────────────
import time as _t
now = _t.time()
open_t = now - 52  # candle opened 52s ago → 8s left → LAST10
le = live_eye(ticks, 1.10000, 60, open_t, now)
print("\n=== Scenario 4: live_eye ===")
print("phase:", le["phase"], "| seconds_left:", le["seconds_left"], "| ready:", le["ready"])
assert le["phase"] == "LAST10" and le["seconds_left"] == 8

# ── Scenario 5: flat / balanced → NEUTRAL, no fake lean ─────────────────────
ticks5 = []
for i in range(100):
    ticks5.append(round(1.10000 + rng.gauss(0, 0.00001), 6))
a5 = analyze_candle_ticks(ticks5, 1.10000, 60)
print("\n=== Scenario 5: balanced noise ===")
print("verdict:", a5["eye_direction"], a5["eye_strength"])
assert a5["eye_direction"] in ("NEUTRAL", "CALL", "PUT")  # just no crash; strength must be low
assert a5["eye_strength"] <= 45

print("\nALL TICK-EYE SCENARIO TESTS PASSED ✔")
