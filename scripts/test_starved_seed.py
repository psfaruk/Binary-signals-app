#!/usr/bin/env python3
"""STARVED-BAND regression test (2026-09-14).

Reproduces the user's perpetual "মডেল এখনো প্রস্তুত নয়" state:
a DB where every pair holds a handful (40) of stale real candles —
NOT zero (so the old zero-only synth seed skipped them) and far below
the ~131 candles needed for FAST_MIN_PAIR_ROWS=80 rows (so training
skipped them too) => registered=NONE forever, no error.

After the STARVED-SEED fix the same DB must: seed 18/18, train, and
register 18/18 provisional models with data_source=synthetic.

Also verifies the ZERO-REASONS surface: with QX_SYNTH_SEED=0 on an
empty DB, a completed run must expose per-pair reasons via
predictor.describe_status()['fast_train']['zero_reasons'].
"""
import os, sys, sqlite3, tempfile, shutil, json

BASE = "/home/z/my-project/repo"
sys.path.insert(0, BASE)

RUN = tempfile.mkdtemp(prefix="starved_test_")
os.environ["DB_PATH"] = os.path.join(RUN, "signals.db")
os.environ["QX_PREDICT_MODELS_DIR"] = os.path.join(RUN, "models")
os.environ["QX_SKIP_DOTENV"] = "1"
os.environ.pop("QX_TOKEN", None)

import db as _db
_db.init()

from core.constants import ALLOWED_PAIRS_OTC

# ── build the starved DB: 40 stale real candles per pair ────────────────
now = int(os.environ.get("TEST_NOW", __import__("time").time())) // 60 * 60
conn = sqlite3.connect(os.environ["DB_PATH"])
for a in ALLOWED_PAIRS_OTC:
    rows = []
    p = 1.10
    for i in range(40):
        t = now - 86400 - (40 - i) * 60   # yesterday, 40 minutes
        o = p; c = o + (0.0007 if i % 2 else -0.0007); p = c
        rows.append((a, 60, t, round(o, 5), round(max(o, c) + 0.0004, 5),
                     round(min(o, c) - 0.0004, 5), round(c, 5),
                     52, 48, "buy", 0, 0, 777, 0.0001))  # tick_count=777 marker
    conn.executemany(
        "INSERT OR IGNORE INTO candle_micro(asset, period, ctime, open, high,"
        " low, close, buy_pct, sell_pct, pressure, is_fight, crosses,"
        " tick_count, net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
conn.commit(); conn.close()
print(f"[setup] starved DB: 40 stale candles × {len(ALLOWED_PAIRS_OTC)} pairs "
      f"(tick_count=777 marker)")

# ── run the real bootstrap (in-process, exactly what the daemon calls) ──
from core.otc_predict import fast_train
res = fast_train.run_bootstrap(force=True)
summary = res.get("summary") or {}
regd = summary.get("pairs_registered") or []
print(f"[bootstrap] registered={len(regd)} pairs in {summary.get('secs')}s "
      f"(dataset_rows={summary.get('dataset_rows')})")

assert len(regd) == len(ALLOWED_PAIRS_OTC), \
    f"EXPECTED {len(ALLOWED_PAIRS_OTC)} registered, got {len(regd)}"

# ── verify honesty fields: provisional + data_source=synthetic ──────────
from core.otc_predict.tracker import active_models
reg = active_models()
bad = []
for a in ALLOWED_PAIRS_OTC:
    row = reg.get(a) or {}
    m = json.loads(row.get("metrics") or "{}")
    if m.get("status") != "provisional" or m.get("data_source") != "synthetic":
        bad.append((a, m.get("status"), m.get("data_source")))
assert not bad, f"honesty fields wrong: {bad}"
print(f"[honesty] 18/18 provisional + data_source=synthetic ✓")

# ── the 40 real rows must still exist (sacred, INSERT OR IGNORE) ────────
# NOTE: the stale rows sit INSIDE the synth block's minute-slots — INSERT
# OR IGNORE means the REAL row wins that minute and the synth candle for
# it is discarded. The tick_count=777 marker proves they survived intact.
conn = sqlite3.connect(os.environ["DB_PATH"])
marked = {a: conn.execute(
    "SELECT COUNT(*) FROM candle_micro WHERE asset=? AND tick_count=777",
    (a,)).fetchone()[0] for a in ALLOWED_PAIRS_OTC}
totals = {a: conn.execute(
    "SELECT COUNT(*) FROM candle_micro WHERE asset=? AND period=60",
    (a,)).fetchone()[0] for a in ALLOWED_PAIRS_OTC}
conn.close()
assert all(n == 40 for n in marked.values()), \
    f"real rows clobbered: {marked}"
assert all(n == 2880 for n in totals.values()), \
    f"unexpected totals (seed slots=2880, real kept inside): {totals}"
print("[sacred] all 18×40 pre-existing real rows survived the seed "
      "(marker intact, totals still 2880) ✓")

# ── idempotence: second run must NOT re-stack synth blocks ──────────────
res2 = fast_train.run_bootstrap(force=True)
s2 = res2.get("summary") or {}
counts = fast_train._micro_counts()
assert all(n <= 2880 + 40 for n in counts.values()), \
    f"synth re-stacked: {counts}"
print(f"[idempotent] second run kept counts bounded ({min(counts.values())}"
      f"..{max(counts.values())}) ✓ registered={len(s2.get('pairs_registered') or [])}")

# ── ZERO-REASONS surface (fresh empty DB + seeding disabled) ────────────
RUN2 = tempfile.mkdtemp(prefix="zeroreasons_")
old_db, old_models = os.environ["DB_PATH"], os.environ["QX_PREDICT_MODELS_DIR"]
os.environ["DB_PATH"] = os.path.join(RUN2, "signals.db")
os.environ["QX_PREDICT_MODELS_DIR"] = os.path.join(RUN2, "models")
os.environ["QX_SYNTH_SEED"] = "0"
import importlib
import core.otc_predict.fast_train as ft2
importlib.reload(ft2)
import db as _db2
_db2.DB_PATH = os.environ["DB_PATH"]; _db2.init()
res3 = ft2.run_bootstrap(force=True)
s3 = res3.get("summary") or {}
assert not (s3.get("pairs_registered") or []), "expected zero registrations"

import core.otc_predict.predictor as pred2
importlib.reload(pred2)
ds = pred2.describe_status("USDBDT_otc")
ft = ds.get("fast_train") or {}
reasons = ft.get("zero_reasons") or []
runs = ft.get("runs")
print(f"[zero-reasons] runs={runs}, reasons[:2]={reasons[:2]}")
assert runs and runs >= 1, "runs counter missing"
assert reasons, "zero_reasons not surfaced for a zero-registration run"
assert any("খালি" in r or "কম" in r for r in reasons), \
    f"reasons lack the expected Bengali diagnosis: {reasons}"

os.environ["QX_SYNTH_SEED"] = "1"
os.environ["DB_PATH"], os.environ["QX_PREDICT_MODELS_DIR"] = old_db, old_models
shutil.rmtree(RUN2, ignore_errors=True)
shutil.rmtree(RUN, ignore_errors=True)
print("\nALL STARVED-BAND TESTS PASSED ✅")
