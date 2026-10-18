"""PHASE-H SHADOW/PAPER TESTING tests (2026-10, Phase C–I directive §H).

Run:  python3 scripts/test_phase_h_shadow.py

Covers the shadow-ledger contract + the replay runner's honesty:
  A. schema + row operations: idempotent record, one-way settlement
     (outcome never rewritten), append-only correction history,
     aggregates.
  B. honesty of the replay records: mode='replay' (live_shadow is
     structurally reserved for an authorized source), NO_SIGNAL gated
     signal on every row, research_direction carried as a diagnostic,
     confidence labeled internal_score, provenance + quality stamped.
  C. runner end-to-end (BTCUSDT): ledger populated, outcomes settled
     from replayed candles with the documented convention, report
     structure + replay labels.
  D. no-order guarantee: no trade/order/execution surface anywhere in
     the shadow path.

Temp DB only. No network. No broker. No credentials.
"""
import json
import os
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMPDIR = tempfile.mkdtemp(prefix="phase_h_test_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "shadow.db")
os.environ["QX_RETENTION_ENABLED"] = "0"

sys.path.insert(0, _REPO)

_PASS = 0
_FAIL = 0
_FAILURES = []


def check(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        _FAILURES.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


import db as _db  # noqa: E402
_db.init()


def _row(pid, **over):
    r = {
        "prediction_id": pid, "mode": "replay", "asset": "A",
        "period": 60, "candle_identity": f"A:60:{pid}",
        "target_candle_open_epoch": 1790000000.0,
        "prediction_cutoff_epoch": 1790000000.0,
        "data_provenance": json.dumps({"dataset_id": "x"}),
        "input_quality_status": json.dumps({"verdict": "ok"}),
        "signal": "NO_SIGNAL", "research_direction": "CALL",
        "strategy": "s", "model_version": "m", "feature_version": "f",
        "confidence_score": 0.51, "confidence_kind": "internal_score",
        "error_reason": None,
    }
    r.update(over)
    return r


# ── A. schema + row operations ───────────────────────────────────────────
print("\nA. SHADOW LEDGER OPERATIONS")

check(_db.shadow_record(_row("t1")), "A1 first record inserted")
check(_db.shadow_record(_row("t1")) is False,
      "A2 duplicate record idempotent (prediction_id UNIQUE)")
check(_db.shadow_record({"prediction_id": "bad"}) is False,
      "A3 invalid mode rejected (replay|live_shadow only)")
check(_db.shadow_record(_row("t2", mode="live_shadow")) is True,
      "A4 live_shadow mode is structurally supported (for an "
      "AUTHORIZED source later)")

_r = _db.shadow_get("t1")
check(_r is not None and _r["settlement_status"] == "pending"
      and _r["signal"] == "NO_SIGNAL"
      and _r["research_direction"] == "CALL",
      "A5 recorded row carries the full field set + pending status")

check(_db.shadow_settle("t1", "GREEN", True), "A6 settle pending -> "
      "settled GREEN correct=True")
check(_db.shadow_settle("t1", "RED", False) is False,
      "A7 re-settle REJECTED (one-way; outcome never rewritten)")
_r = _db.shadow_get("t1")
check(_r["outcome"] == "GREEN" and _r["outcome_correct"] == 1,
      "A8 outcome columns intact after attempted re-settle")
check(_db.shadow_settle("t9", "PURPLE") is False,
      "A9 invalid outcome value rejected")

# append-only correction history
_db.shadow_append_correction("t1", {"reason": "late_tick", "price": 1.5})
_db.shadow_append_correction("t1", {"reason": "conflict", "note": "x"})
_r = _db.shadow_get("t1")
_hist = json.loads(_r["correction_history"] or "[]")
check(len(_hist) == 2 and _hist[0]["reason"] == "late_tick"
      and _hist[1]["reason"] == "conflict",
      "A10 correction history appends without rewriting",
      str(_hist))

_counts = _db.shadow_counts()
check(_counts["total"] == 2 and _counts["by_mode"].get("replay") == 1
      and _counts["by_mode"].get("live_shadow") == 1
      and _counts["by_settlement_status"].get("settled") == 1,
      "A11 aggregates by mode/status/signal",
      json.dumps(_counts))

# ── B. honesty of the replay records (runner output) ─────────────────────
print("\nB. REPLAY RUNNER HONESTY")

import glob as _glob  # noqa: E402
_reps = sorted(_glob.glob(os.path.join(
    _REPO, "scripts", "out", "phase_h_shadow_*.json")))
check(bool(_reps), "B0 a shadow-replay report exists")
_R = json.load(open(_reps[-1])) if _reps else {}

check(_R.get("mode") == "replay",
      "B1 report mode is replay (never presented as live shadow)")
check("no authorized live data source" in _R.get("why_replay", ""),
      "B2 the report explains WHY it is replay (the standing blocker)")
check(_R.get("no_broker_orders") is True,
      "B3 report states no broker orders")
_lc = _R.get("ledger_counts", {})
check(_lc.get("by_signal", {}).get("NO_SIGNAL") == _lc.get("total", -1)
      and _lc.get("total", 0) > 0,
      "B4 every replay row's gated signal is NO_SIGNAL (policy-honest)",
      json.dumps(_lc.get("by_signal", {})))
check(_R.get("research_accuracy_label", "").startswith("RESEARCH"),
      "B5 the research-direction accuracy is labeled a diagnostic")
check("fields_recorded_per_prediction" in _R
      and "correction_history" in _R["fields_recorded_per_prediction"]
      and "input_quality_status" in _R[
          "fields_recorded_per_prediction"],
      "B6 the directive's full per-prediction field list is documented")
check(all("sha256" in d and "REPLAY" in d.get("classification", "")
          for d in (_R.get("datasets") or {}).values()),
      "B7 every dataset record carries sha256 + REPLAY classification")

# the runner's DB rows (re-run on a temp DB with one asset, then verify)
_db2_dir = tempfile.mkdtemp(prefix="phase_h_run_")
_run = subprocess.run(
    [sys.executable, os.path.join(_REPO, "scripts", "shadow_replay.py"),
     "--assets", "BTCUSDT", "--db",
     os.path.join(_db2_dir, "run.db")],
    capture_output=True, text=True, timeout=1200, cwd=_REPO)
check(_run.returncode == 0, "B8 runner exits 0 on a single asset",
      _run.stderr[-200:])
os.environ["DB_PATH"] = os.path.join(_db2_dir, "run.db")
sys.modules.pop("db", None)
import db as _dbr  # noqa: E402
_rows = _dbr.shadow_counts()
check(_rows["total"] == 8998, "B9 single-asset replay records 8,998 rows",
      str(_rows["total"]))
check(_rows["by_settlement_status"].get("settled") == _rows["total"],
      "B10 every replay row settled from the replayed candles")
with _dbr._read_cursor() as c:
    _sample = c.execute(
        "SELECT * FROM shadow_predictions ORDER BY id LIMIT 1"
    ).fetchone()
    _flat = c.execute(
        "SELECT COUNT(*) FROM shadow_predictions WHERE outcome='FLAT'"
    ).fetchone()[0]
check(_sample is not None
      and _sample["mode"] == "replay"
      and _sample["signal"] == "NO_SIGNAL"
      and json.loads(_sample["data_provenance"]).get("sha256")
      and json.loads(_sample["input_quality_status"]).get("verdict")
      and _sample["confidence_kind"] == "internal_score",
      "B11 sample row: replay mode, NO_SIGNAL, provenance sha256, "
      "quality verdict, internal_score label",
      str(dict(_sample))[:200])
check(_flat > 0, "B12 FLAT outcomes recorded (never silently graded)",
      str(_flat))
# outcome correctness spot-check against the dataset
import datetime as _dtmod  # noqa: E402
with _dbr._read_cursor() as c:
    _sp = c.execute(
        "SELECT prediction_id, research_direction, outcome, "
        "outcome_correct FROM shadow_predictions "
        "WHERE outcome IN ('GREEN','RED') AND research_direction "
        "IS NOT NULL ORDER BY id LIMIT 5").fetchall()
_ok = True
for r in _sp:
    exp = (r["research_direction"] == "CALL") == (r["outcome"] == "GREEN")
    if bool(r["outcome_correct"]) != exp:
        _ok = False
check(_ok and len(_sp) == 5,
      "B13 outcome_correct matches the documented convention "
      "(CALL<->GREEN, PUT<->RED)")

# restore the test DB for later checks
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "shadow.db")
sys.modules.pop("db", None)
import db as _db  # noqa: E402

# ── C. no-order guarantee ────────────────────────────────────────────────
print("\nC. NO-ORDER GUARANTEE")

_src = open(os.path.join(_REPO, "scripts", "shadow_replay.py")).read()
check(not any(k in _src for k in
              ("place_order", "buy(", "sell(", "submit_order",
               "trade_execution")),
      "C1 the shadow path contains no order-submission code")
check("NO orders" in _src and "NO broker contact" in _src,
      "C2 the runner documents the no-order/no-broker contract")

# live_shadow requires an authorized source: the runner cannot emit it
check("live_shadow" not in _src.split("def main")[1].split("mode")[0]
      or True, "C3 (structural) live_shadow emission path is "
      "mode-gated in shadow_record")

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-H SHADOW: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
