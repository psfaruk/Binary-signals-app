"""Regression tests for the history-gate SUPPRESS policy (2026-09-19).

FIX (FADE-REMOVAL-2026-09-19): the gate no longer INVERTS measured-bad
directions (CALL↔PUT fade — gambler's fallacy at these sample sizes, and
it fed a fade-feedback loop in the ledger). A direction/pair the ledger
has MEASURED anti-predictive is now SUPPRESSED to NEUTRAL with the
`_history_gate_suppressed` marker; feed.py bypasses the ML model and the
fade-default for suppressed candles, so a measured-bad pair honestly
carries NO signal. Run:  py scripts/test_history_gate_fade.py
(filename kept for history; the policy is suppress, not fade).
"""
import os
import sqlite3
import sys
import tempfile

TMP = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DB_PATH"] = TMP  # must be set BEFORE importing core.signal_history_gate

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.signal_history_gate as hg

conn = sqlite3.connect(TMP)
conn.execute("""CREATE TABLE signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT, period INTEGER, signal TEXT, accuracy TEXT, ctime REAL)""")

_row_id = [0]


def add(asset, n_rows, wins, wrong_last=0, call_split=None):
    """Insert n_rows for an asset: `wins` correct rows total; `wrong_last`
    newest rows forced wrong (streak construction); call_split=(call_n,
    call_wins) overrides to build a specific CALL-side history."""
    period = 60
    if call_split is None:
        calls = wins
        puts = 0
        call_wins = wins
        put_wins = 0
    else:
        call_n, call_wins = call_split
        calls = call_n
        puts = n_rows - call_n
        put_wins = wins - call_wins
    # build rows oldest → newest; the LAST wrong_last rows become wrong
    rows = []
    # distribute wins: simplest — correct rows first except the tail
    seq = []
    for i in range(n_rows):
        seq.append("correct")
    if wrong_last:
        for i in range(n_rows - wrong_last, n_rows):
            seq[i] = "wrong"
    else:
        # make exactly `wins` correct: correct count = wins, place wrongs
        seq = ["correct"] * wins + ["wrong"] * (n_rows - wins)
    # shuffle into stable interleave while keeping tail: build CALL/PUT split
    i = 0
    while i < n_rows:
        is_call = i < calls
        if call_split is not None:
            # CALL rows: first call_wins correct; PUT rows: put_wins correct
            if is_call:
                acc = "correct" if (i < call_wins) else "wrong"
            else:
                put_idx = i - calls
                acc = "correct" if (put_idx < put_wins) else "wrong"
        else:
            acc = seq[i]
        _row_id[0] += 1
        rows.append((asset, period, "CALL" if is_call else "PUT",
                     acc, 1000.0 + _row_id[0]))
        i += 1
    conn.executemany(
        "INSERT INTO signal_log (asset, period, signal, accuracy, ctime) "
        "VALUES (?,?,?,?,?)", rows)
    conn.commit()  # the gate opens its OWN connection — rows must be visible


def base_result(signal="CALL", conf=70):
    return {"signal": signal, "confidence": conf, "strength": "MEDIUM",
            "reasons": []}


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# ── Case 1: pair floor — shrunk WR 41.7% < 46% → SUPPRESS (no inversion)
#     (rows end with wrongs → the streak cooldown fires first; either mode
#     is a valid suppression — what matters is NEVER inverting the signal.)
add("EURUSD_otc", 30, 10)
r = hg.apply_history_gate(base_result(), "EURUSD_otc", 60)
check("pair-floor/streak → suppressed to NEUTRAL (never inverted)",
      r["signal"] == "NEUTRAL" and r.get("_history_gate_suppressed") is True)
check("suppressed confidence zeroed", r["confidence"] == 0)
check("suppress audit trail present",
      r["history_gate"]["verdict"] == "suppress"
      and r["history_gate"]["mode"] in
      ("cooldown", "suppressed-pair"))

# ── Case 2: healthy pair — no suppression, confidence untouched
add("GBPUSD_otc", 30, 20, call_split=(20, 14))
r = hg.apply_history_gate(base_result(), "GBPUSD_otc", 60)
check("healthy pair → direction untouched", r["signal"] == "CALL"
      and not r.get("_history_gate_suppressed") and r["confidence"] == 70)

# ── Case 3: learning mode (n < 20) — damped confidence, no judgement
add("USDBDT_otc", 5, 3)
r = hg.apply_history_gate(base_result(), "USDBDT_otc", 60)
check("learning mode → allow + conf damped",
      r["signal"] == "CALL" and r["confidence"] == 68
      and r["history_gate"]["mode"] == "learning")

# ── Case 4: 6-loss streak → suppressed (regime-change evidence, NOT flipped)
add("USDZAR_otc", 20, 14, wrong_last=6)
r = hg.apply_history_gate(base_result("PUT", 65), "USDZAR_otc", 60)
check("loss streak → suppressed to NEUTRAL (never flipped)",
      r["signal"] == "NEUTRAL" and r.get("_history_gate_suppressed") is True
      and r["history_gate"]["mode"] == "cooldown")

# ── Case 5: direction floor — CALL side broken (37% shrunk) while the
#     pair overall passes the 46% floor → suppress the CALL side only
add("AUDCAD_otc", 20, 9, call_split=(15, 4))
r = hg.apply_history_gate(base_result(), "AUDCAD_otc", 60)
check("direction floor → CALL suppressed (pair floor passes)",
      r["signal"] == "NEUTRAL" and r.get("_history_gate_suppressed") is True
      and r["history_gate"]["mode"] == "suppressed-direction"
      and "CALL side" in str(r["reasons"]))
# The OTHER direction on the same pair must not be blocked by the
# DIRECTION floor (PUT side here is 5/5). NOTE: with this synthetic fleet
# (GBPUSD 61.9%, USDZAR 62.5% medians) the pair may still be suppressed
# by the separate below-fleet rule — that is correct behavior, not a
# direction-floor leak; verify_history_gate.py test 7 covers the fleet
# rule with fleet-neutral data.
hg.invalidate_cache()
r_put = hg.apply_history_gate(base_result("PUT", 70), "AUDCAD_otc", 60)
check("direction floor did NOT block the opposite side",
      r_put.get("history_gate", {}).get("mode") != "suppressed-direction"
      and (r_put["signal"] == "PUT"
           or r_put.get("history_gate", {}).get("mode") == "below-fleet"))

conn.close()
failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
