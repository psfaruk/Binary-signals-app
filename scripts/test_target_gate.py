#!/usr/bin/env python3
"""Unit test for core/target_gate.py — controller behaviour + grading safety.

Run: python3 scripts/test_target_gate.py
Uses a throwaway DB; prints PASS/FAIL per assertion, exit 1 on any FAIL.
"""
import os
import sys
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["DB_PATH"] = os.path.join(REPO, "test_target_gate_tmp.db")

for f in ("test_target_gate_tmp.db", "test_target_gate_tmp.db-wal",
          "test_target_gate_tmp.db-shm"):
    if os.path.exists(f):
        os.unlink(f)

import db as _db                        # noqa: E402
_db.init()
from core import target_gate as tg      # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        FAILS.append(name)


def seed(asset, direction, outcomes, period=60):
    """outcomes: list of 'correct'/'wrong' — written chronologically."""
    import time as _t
    t0 = int(_t.time()) - len(outcomes) * 60
    for i, acc in enumerate(outcomes):
        _db.log_signal(asset, period, t0 + i * 60, direction, 1.0, 80,
                       "[]", "up" if acc == "correct" else "down", acc,
                       strength="STRONG", category="otc")


# ── 1. grading-safety: WAIT reason must NOT match the recovery regex ────────
gated = tg.apply_gate({"signal": "CALL", "confidence": 40, "score": 1.0,
                       "strength": "STRONG", "reasons": ["test"]},
                      "TESTPAIR_otc", 60)
check("gated→NEUTRAL", gated.get("signal") == "NEUTRAL")
check("gated keeps origin", gated.get("gated_from") == "CALL")
reasons_text = " ".join(gated.get("reasons", []))
check("no recovery marker", re.search(r"opposed original (CALL|PUT)",
                                      reasons_text) is None, reasons_text[-80:])
check("no conf-recovery marker", "_RECOVERED_CONFIDENCE" not in reasons_text)
check("gated conf=0", gated.get("confidence") == 0)

# ── 2. pass-through at/above bar ────────────────────────────────────────────
ok = tg.apply_gate({"signal": "PUT", "confidence": 85, "score": 2.0,
                    "strength": "STRONG", "reasons": []},
                   "TESTPAIR_otc", 60)
check("above-bar passes", ok.get("signal") == "PUT")
check("bar echoed", ok.get("target_gate_bar") == tg.GATE_INIT)

# ── 3. controller: WR below target raises the gate ──────────────────────────
seed("RAISE_otc", "CALL", ["wrong"] * 18 + ["correct"] * 2)   # 10% WR, n=20
gate0, wr0, n0 = tg.get_gate("RAISE_otc", "CALL", 60)
check("rolling WR read", n0 == 20 and wr0 is not None and wr0 < 20,
      f"n={n0} wr={wr0}")
tg.note_graded("RAISE_otc", "CALL", False, 60)
gate1, _, _ = tg.get_gate("RAISE_otc", "CALL", 60)
check("gate raised", gate1 > gate0, f"{gate0} → {gate1}")

# ── 4. controller: sustained WR ≥ target+margin eases the gate ──────────────
seed("EASE_otc", "CALL", ["correct"] * 28 + ["wrong"] * 2)    # 93% WR, n=30
ge0, we0, _ = tg.get_gate("EASE_otc", "CALL", 60)
# push the stored gate up first so easing has room
tg._persist_gate("EASE_otc", "CALL", tg.GATE_CAP, we0, 30)
tg.get_gate.cache_clear() if hasattr(tg.get_gate, "cache_clear") else None
with tg._LOCK:
    tg._cache.pop(("EASE_otc", "CALL"), None)
ge1, _, _ = tg.get_gate("EASE_otc", "CALL", 60)
check("persisted cap", ge1 == tg.GATE_CAP, f"gate={ge1}")
tg.note_graded("EASE_otc", "CALL", True, 60)
ge2, _, _ = tg.get_gate("EASE_otc", "CALL", 60)
check("gate eased −1", ge2 == ge1 - 1, f"{ge1} → {ge2}")

# ── 5. clamp: floor/cap respected ───────────────────────────────────────────
check("clamp low", tg._clamp(-100) == tg.GATE_FLOOR)
check("clamp high", tg._clamp(9999) == tg.GATE_CAP)

# ── 6. fail-open: DB garbage never raises out of apply_gate ────────────────
try:
    _db._conn().execute("DROP TABLE pair_gate_state")
    _db._conn().commit()
    _db._conn().close()
except Exception:
    pass
try:
    r = tg.apply_gate({"signal": "CALL", "confidence": 40, "reasons": []},
                      "FAILSAFE_otc", 60)
    check("fail-open returns result", isinstance(r, dict))
except Exception as exc:
    check("fail-open never raises", False, str(exc))

# ── 7. starvation relief lowers both gates after N waits ────────────────────
tg._ensure_table(_db._conn())
tg._persist_gate("STARVE_otc", "CALL", 80, 60.0, 30)
tg._persist_gate("STARVE_otc", "PUT", 80, 60.0, 30)
with tg._LOCK:
    tg._cache.pop(("STARVE_otc", "CALL"), None)
    tg._cache.pop(("STARVE_otc", "PUT"), None)
for _ in range(tg.STARVATION_CANDLES):
    tg._starve_note("STARVE_otc")
gc, _, _ = tg.get_gate("STARVE_otc", "CALL", 60)
gp, _, _ = tg.get_gate("STARVE_otc", "PUT", 60)
check("starvation eased CALL", gc == 79, f"gate={gc}")
check("starvation eased PUT", gp == 79, f"gate={gp}")

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAIL → {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS")
