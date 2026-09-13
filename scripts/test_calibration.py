"""Regression test for the calibration-table P(UP) mirroring bug.

The frozen row's `probability` is ALWAYS P(UP) (predict_proba[:,1],
Platt-calibrated). The old table mirrored PUT rows (p_up = 1-p), so
confident PUT predictions (P(UP)=0.25) were recorded as failed UP
predictions in the 70%+ buckets — making the model look inverted at
high confidence. This test pins the fixed behavior.

Run:  py scripts/test_calibration.py
"""
import os
import sqlite3
import sys
import tempfile

TMP = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DB_PATH"] = TMP  # must be set BEFORE importing db/tracker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

conn = sqlite3.connect(TMP)
conn.execute("""CREATE TABLE otc_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT, period INTEGER, horizon INTEGER,
    signal_time INTEGER, target_time INTEGER,
    prediction TEXT, probability REAL, tier TEXT, score REAL,
    emit INTEGER, components TEXT, regime TEXT, pa_agreed INTEGER,
    quality TEXT, reason TEXT, model_version TEXT,
    feature_json TEXT, close_i REAL,
    actual_open REAL, actual_close REAL, actual_result TEXT,
    win_loss TEXT, settled_at INTEGER
)""")
rows = []
t = 1000
# 4 confident CALL rows (P(UP)=0.72): 1 win, 3 losses
for i, wl in enumerate(["win", "loss", "loss", "loss"]):
    rows.append(("EURUSD_otc", 60, 1, t + i * 60, t + (i + 1) * 60,
                 "CALL", 0.72, "GOOD", 80.0, 1, "{}", "{}", 1, "{}",
                 "ok", "test", None, 1.10, 1.10,
                 1.1002 if wl == "win" else 1.0998,
                 "UP" if wl == "win" else "DOWN", wl, 1))
# 4 confident PUT rows (P(UP)=0.25): 3 wins (DOWN), 1 loss (UP)
for i, wl in enumerate(["win", "win", "win", "loss"]):
    rows.append(("EURUSD_otc", 60, 1, t + 400 + i * 60,
                 t + 400 + (i + 1) * 60, "PUT", 0.25, "GOOD", 80.0, 1,
                 "{}", "{}", 1, "{}", "ok", "test", None, 1.10, 1.10,
                 1.0998 if wl == "win" else 1.1002,
                 "DOWN" if wl == "win" else "UP", wl, 1))
conn.executemany("""INSERT INTO otc_predictions
 (asset, period, horizon, signal_time, target_time, prediction, probability,
  tier, score, emit, components, regime, pa_agreed, quality, reason,
  model_version, feature_json, close_i, actual_open, actual_close,
  actual_result, win_loss, settled_at)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
conn.commit()
conn.close()

from core.otc_predict.tracker import prediction_analytics
a = prediction_analytics()

cal = {(b["lo"], b["hi"]): b for b in a["calibration"]}
checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


hi = cal[(0.70, 0.76)]
check("high bucket contains ONLY the 4 CALL rows (no mirrored PUTs)",
      hi["n"] == 4)
check("high bucket actual_wr = 25% (1/4 UP, honest)", hi["actual_wr"] == 25.0)
lo_b = cal[(0.24, 0.30)]
check("low bucket holds the 4 confident PUT rows", lo_b["n"] == 4)
check("low bucket actual UP rate = 25% (model 75% correct on DOWN)",
      lo_b["actual_wr"] == 25.0)
check("brier_n = 8", a["brier_n"] == 8)
check("avg_confidence is directional (~0.735)",
      abs(a["avg_confidence"] - 0.735) < 0.01)
check("no high bucket polluted with mirrored PUT rows",
      all(b["n"] <= 4 for b in a["calibration"] if b["lo"] >= 0.60))

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
