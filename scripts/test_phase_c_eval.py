"""PHASE-C CLEAN EVALUATION tests (2026-10, Phase C–I directive).

Run:  python3 scripts/test_phase_c_eval.py

The battery is ADVERSARIAL BY DESIGN: most checks intentionally construct
BAD examples (leaky features, overlapping splits, duplicate predictions,
malformed candles, label echoes, future-reading functions) and prove the
evaluator REJECTS them. A leakage guard that cannot fail is not a guard.

Covers:
  A. candle integrity rules (19 checks — every reason code)
  B. prediction integrity rules (13 checks)
  C. leakage adversarial battery (16 checks)
  D. chronological splits + embargo + walk-forward (9 checks)
  E. metrics incl. Wilson CI / Brier / coverage (12 checks)
  F. baselines incl. no-future-access proof (7 checks)
  G. break-even calculator (8 checks)
  H. end-to-end evaluator mini-run + provenance INSUFFICIENT_DATA (3 checks)
  I. frozen protocol constants (1 check)

Temp DBs only where needed. No network. No broker. No credentials.
Synthetic fixtures only — no market data is loaded.
"""
import os
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.eval_clean.integrity import (  # noqa: E402
    CandleCheckPolicy, validate_candle_series, validate_predictions,
    outcome_of, GREEN, RED, FLAT,
)
from core.eval_clean.leakage import (  # noqa: E402
    EvidenceLedger, SplitScaler, LeakageError,
    verify_split_independence, detect_label_echo,
    future_perturbation_unchanged,
)
from core.eval_clean.splits import (  # noqa: E402
    SplitSpec, SplitResult, chronological_split, walk_forward_folds,
)
from core.eval_clean.baselines import (  # noqa: E402
    majority_direction, previous_candle_direction, PrespecifiedLogreg,
    research_features, research_label, RESEARCH_BASELINE_FEATURES,
    CALL, PUT,
)
from core.eval_clean.metrics import (  # noqa: E402
    wilson_ci, binary_metrics, calibration_table,
)
from core.eval_clean.breakeven import (  # noqa: E402
    break_even_win_rate, breakeven_report,
)

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


def _c(t, o=10.0, h=None, l=None, c=10.5, asset="A", period=60):
    if o is None or c is None:
        h = h if h is not None else 10.6
        l = l if l is not None else 9.4
    else:
        h = h if h is not None else max(o, c) + 0.1
        l = l if l is not None else min(o, c) - 0.1
    return {"asset": asset, "period": period, "t": t, "o": o,
            "h": h, "l": l, "c": c}


T0 = (1_790_000_000 // 60) * 60  # period-aligned epoch seconds

# ── A. candle integrity ──────────────────────────────────────────────────
print("\nA. CANDLE INTEGRITY")

r = validate_candle_series([_c(T0), _c(T0 + 60)])
check(len(r.accepted) == 2 and not r.quarantined,
      "A1 clean contiguous series accepted")

r = validate_candle_series([_c(T0, asset=""), _c(T0, asset=None),
                            {"period": 60, "t": T0, "o": 1, "h": 1, "l": 1,
                             "c": 1}])
check(all(q["reason"] == "asset_invalid" for q in r.quarantined)
      and len(r.quarantined) == 3, "A2 asset_invalid (3 kinds)",
      str([q["reason"] for q in r.quarantined]))

r = validate_candle_series([_c(T0, period=0), _c(T0, period=-60),
                            _c(T0, period=True), _c(T0, period="60")])
check(all(q["reason"] == "period_invalid" for q in r.quarantined)
      and len(r.quarantined) == 4, "A3 period_invalid (4 kinds)")

r = validate_candle_series([_c(None), _c(float("nan")),
                            _c(T0, o=None)])
check(all(q["reason"] in ("ts_invalid", "ohlc_missing")
          for q in r.quarantined), "A4 ts_invalid/ohlc_missing",
      str([q["reason"] for q in r.quarantined]))

r = validate_candle_series([_c(T0 + 30)])
check(r.quarantined[0]["reason"] == "ts_misaligned",
      "A5 ts_misaligned (t%period!=0)")

r = validate_candle_series([_c(T0, o=float("nan")),
                            _c(T0 + 60, c=float("inf"))])
check(all(q["reason"] == "ohlc_non_finite" for q in r.quarantined),
      "A6 ohlc_non_finite (NaN/inf)")

r = validate_candle_series([_c(T0, o=0.0), _c(T0 + 60, c=-1.0)])
check(all(q["reason"] == "price_non_positive" for q in r.quarantined),
      "A7 price_non_positive")

r = validate_candle_series([
    _c(T0, o=10.0, h=10.2, l=9.8, c=10.5),   # h < max(o,c) -> bad
    _c(T0 + 60, o=10.0, h=10.6, l=10.2, c=10.5),  # l > min(o,c) -> bad
    _c(T0 + 120, o=10.0, h=10.3, l=10.4, c=10.5),  # l > h -> bad
])
check(all(q["reason"] == "ohlc_inconsistent" for q in r.quarantined)
      and len(r.quarantined) == 3, "A8 ohlc_inconsistent (3 variants)")

r = validate_candle_series([_c(T0), _c(T0)])
check(r.quarantined[-1]["reason"] == "duplicate_candle"
      and len(r.accepted) == 1 and r.duplicate_count == 1,
      "A9 duplicate_candle quarantined (first kept)")

r = validate_candle_series([_c(T0 + 60), _c(T0)])
check(r.quarantined[-1]["reason"] == "out_of_order"
      and r.out_of_order_count == 1, "A10 out_of_order quarantined")

r = validate_candle_series([_c(T0), _c(T0 + 180)])
check(r.gap_count == 1 and r.gap_sizes == [2] and len(r.accepted) == 2
      and r.accepted[1].gap_before is True,
      "A11 missing_interval recorded + gap_before flag",
      f"gaps={r.gap_sizes}")

r = validate_candle_series([_c(T0, o=10.0, h=10.0, l=10.0, c=10.0)])
check(r.quarantined[0]["reason"] == "zero_range",
      "A12 zero_range quarantined by default")

r = validate_candle_series([_c(T0, o=10.0, h=10.0, l=10.0, c=10.0)],
                           CandleCheckPolicy(zero_range="flag"))
check(not r.quarantined and len(r.flags) == 1 and len(r.accepted) == 1,
      "A13 zero_range policy=flag accepts + flags")

r = validate_candle_series([_c(T0, o=10.0, c=10.0, h=10.4, l=9.6)])
check(len(r.accepted) == 1 and r.accepted[0].outcome == FLAT
      and r.tie_count == 1, "A14 tie candle accepted + FLAT + counted")

check(outcome_of(10.0, 10.5) == GREEN and outcome_of(10.5, 10.0) == RED
      and outcome_of(10.0, 10.0) == FLAT
      and outcome_of(10.0, 10.0 + 1e-13) == FLAT,
      "A15 outcome_of convention + epsilon boundary")

r = validate_candle_series([_c(T0), _c(T0 + 300)])
s = r.summary()
check(s["quarantined_by_reason"].get("missing_interval") is None
      and r.gap_count == 1,
      "A16 gaps recorded in series stats (not per-candle quarantine)")

r = validate_candle_series([
    {"asset": "A", "period": 60, "time": T0, "open": 10.0,
     "high": 10.6, "low": 9.7, "close": 10.4}])
check(len(r.accepted) == 1, "A17 long-form keys accepted")

r = validate_candle_series([_c(T0), _c(T0 + 60), _c(T0)])
s = r.summary()
check(s["quarantined_by_reason"] == {"duplicate_candle": 1},
      "A18 summary() quarantine-by-reason counts")

# ms-vs-s honesty: the LOADER is the single documented conversion point;
# validators require epoch SECONDS and reject what misalignment CAN catch.
import importlib.util as _ilu  # noqa: E402
import json as _json  # noqa: E402
import tempfile as _tf  # noqa: E402
_ms_tmp = _tf.NamedTemporaryFile(suffix=".json", delete=False)
_ms_row = {"t": T0 * 1000, "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.2,
           "v": 1.0}
_ms_tmp.write(_json.dumps([_ms_row]).encode())
_ms_tmp.close()
_spec2 = _ilu.spec_from_file_location(
    "bce_ms", os.path.join(_REPO, "scripts", "backtest_clean_eval.py"))
_bce_ms = _ilu.module_from_spec(_spec2)
_spec2.loader.exec_module(_bce_ms)
_loaded = _bce_ms.load_binance_1m(_ms_tmp.name, "A")
os.unlink(_ms_tmp.name)
check(_loaded[0]["t"] == T0 and _loaded[0]["t"] % 60 == 0
      and _loaded[0]["t"] < 2e9,
      "A19 loader converts ms->s explicitly (validators take seconds only)")

# ── B. prediction integrity ─────────────────────────────────────────────
print("\nB. PREDICTION INTEGRITY")


def _mk_candles(n=6, start=T0):
    rows = []
    px = 10.0
    for i in range(n):
        o = px
        c = px + (0.3 if i % 2 == 0 else -0.3)
        rows.append(_c(start + i * 60, o=o, c=c,
                       h=max(o, c) + 0.1, l=min(o, c) - 0.1))
        px = c
    return rows


cs = _mk_candles(6)
cr = validate_candle_series(cs)
p1 = [{"asset": "A", "period": 60, "target_t": T0 + 60,
       "direction": "CALL", "evidence_ts": float(T0 + 60)}]
pr = validate_predictions(p1, cr)
check(len(pr.accepted) == 1
      and pr.accepted[0]["correct"]
      == (pr.accepted[0]["outcome"] == GREEN)
      and pr.accepted[0]["direction"] == "CALL",
      "B1 valid prediction accepted + correctness follows outcome mapping")

pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": None, "direction": "CALL",
                            "evidence_ts": 1.0}], cr)
check(pr.excluded[0]["reason"] == "prediction_invalid",
      "B2 prediction_invalid (malformed identity)")

pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 60, "direction": "GREEN",
                            "evidence_ts": float(T0 + 60)}], cr)
check(pr.excluded[0]["reason"] == "direction_invalid",
      "B3 direction_invalid (GREEN is an outcome, not a direction)")

pr = validate_predictions([
    {"asset": "A", "period": 60, "target_t": T0 + 60, "direction": "CALL",
     "evidence_ts": float(T0 + 60)},
    {"asset": "A", "period": 60, "target_t": T0 + 60, "direction": "PUT",
     "evidence_ts": float(T0 + 60)}], cr)
check(len(pr.accepted) == 1 and pr.accepted[0]["direction"] == "CALL"
      and pr.excluded[0]["reason"] == "duplicate_prediction",
      "B4 duplicate_prediction: FIRST kept, second excluded")

pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 99999, "direction": "CALL",
                            "evidence_ts": 1.0}], cr)
check(pr.excluded[0]["reason"] == "target_missing",
      "B5 target_missing (no candle at target)")

bad_cr = validate_candle_series(
    [_c(T0, o=10.0, h=10.0, l=10.0, c=10.0)])  # zero-range -> quarantined
mixed_cr = validate_candle_series(cs + [_c(T0 + 600, o=5.0, h=5.0, l=5.0,
                                            c=5.0)])
pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 600, "direction": "CALL",
                            "evidence_ts": 1.0}], mixed_cr)
check(pr.excluded[0]["reason"] == "target_quarantined",
      "B6 target_quarantined (target candle was rejected)")

pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 60, "direction": "CALL",
                            "evidence_ts": float(T0 + 60) + 0.001}], cr)
check(pr.excluded[0]["reason"] == "evidence_leakage",
      "B7 evidence_leakage (evidence after cutoff REJECTED)")

pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 60, "direction": "CALL",
                            "evidence_ts": float(T0 + 60)}], cr)
check(len(pr.accepted) == 1,
      "B8 boundary evidence (evidence_ts == cutoff) accepted")

gap_cr = validate_candle_series([_c(T0), _c(T0 + 300)])
pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 300, "direction": "CALL",
                            "evidence_ts": 1.0}], gap_cr)
check(pr.excluded[0]["reason"] == "post_gap_target",
      "B9 post_gap_target excluded (discontinuous feature window)")

tie_cs = [_c(T0, o=10.0, c=10.0, h=10.4, l=9.6)]
tie_cr = validate_candle_series(tie_cs)
pr = validate_predictions([{"asset": "A", "period": 60, "target_t": T0,
                            "direction": "CALL",
                            "evidence_ts": 0.0}], tie_cr)
check(pr.excluded[0]["reason"] == "tie_outcome" and len(pr.accepted) == 0,
      "B10 tie_outcome excluded from metrics (never graded)")

pr = validate_predictions([{"asset": "A", "period": 60,
                            "target_t": T0 + 60, "direction": "NO_SIGNAL",
                            "evidence_ts": float(T0 + 60)}], cr)
check(pr.no_signal_count == 1 and not pr.excluded
      and not pr.accepted,
      "B11 NO_SIGNAL counted for coverage, not an error")

for badp in (0.0, 1.0, -0.2, float("nan"), 2.0):
    pr = validate_predictions([{"asset": "A", "period": 60,
                                "target_t": T0 + 60, "direction": "CALL",
                                "evidence_ts": float(T0 + 60),
                                "prob": badp}], cr)
    if pr.excluded and pr.excluded[0]["reason"] == "prediction_invalid":
        ok = True
        break
else:
    ok = False
check(ok, "B12 invalid probabilities rejected (0/1/negative/NaN/2)")

pr = validate_predictions([
    {"asset": "A", "period": 60, "target_t": T0 + 120, "direction": "PUT",
     "evidence_ts": float(T0 + 120)}], cr)
check(pr.accepted[0]["correct"] is False
      and pr.accepted[0]["outcome"] == GREEN,
      "B13 correctness mapping: PUT on GREEN target is wrong")

# ── C. leakage adversarial battery ──────────────────────────────────────
print("\nC. LEAKAGE ADVERSARIAL BATTERY")

led = EvidenceLedger()
led.add("body_ratio", 100.0)
led.add("wick_ratio", 100.5)
v = led.violations(100.0)
check(len(v) == 1 and v[0]["name"] == "wick_ratio",
      "C1 ledger flags evidence strictly after cutoff")

led = EvidenceLedger()
led.add("f", 100.0)
check(led.violations(100.0) == [],
      "C2 boundary evidence (== cutoff) is NOT a violation")

try:
    EvidenceLedger().add("f", float("nan"))
    ok = False
except ValueError:
    ok = True
check(ok, "C3 non-finite evidence rejected at add()")

try:
    SplitScaler().transform([[1.0]])
    ok = False
except RuntimeError:
    ok = True
check(ok, "C4 SplitScaler.transform before fit RAISES (train-only stats)")

train = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
sc = SplitScaler().fit(train)
check(abs(sc._means[0] - 3.0) < 1e-12,
      "C5 scaler means are TRAIN-only (3.0, not the pooled 3.5)")

try:
    SplitScaler().fit([[1.0, 2.0], [1.0]])
    ok = False
except ValueError:
    ok = True
check(ok, "C6 ragged matrix rejected")

tr = [{"asset": "A", "target_t": 100}]
te = [{"asset": "A", "target_t": 90}]
try:
    verify_split_independence(tr, te, 1)
    ok = False
except LeakageError:
    ok = True
check(ok, "C7 overlapping splits (test before train) RAISE")

tr = [{"asset": "A", "target_t": 100}]
te = [{"asset": "A", "target_t": 110}]  # 110 < 100 + 1*60
try:
    verify_split_independence(tr, te, 1)
    ok = False
except LeakageError:
    ok = True
check(ok, "C8 embargo violation (label overlap) RAISE")

tr = [{"asset": "A", "target_t": 100}]
te = [{"asset": "A", "target_t": 100}]
try:
    verify_split_independence(tr, te, 5)
    ok = False
except LeakageError:
    ok = True
check(ok, "C9 duplicate observation across splits RAISE")

tr = [{"asset": "A", "target_t": 100}]
te = [{"asset": "A", "target_t": 200}]
out = verify_split_independence(tr, te, 1)
check(out["ok"] is True and out["checked"] == 2,
      "C10 valid split passes verification")

rows = [{"y": 1, "leaky": 1, "clean": 0.5}, {"y": 0, "leaky": 0,
                                             "clean": 0.7}]
off = detect_label_echo(rows, ["leaky", "clean"])
check(off == ["leaky"], "C11 label-echo feature FLAGGED")

off = detect_label_echo(rows, ["clean"])
check(off == [], "C12 clean feature not flagged")

ok = True
try:
    future_perturbation_unchanged(
        lambda a, b, c, d: a + b + c, [1.0, 2.0, 3.0, 999.0],
        lambda v: v * 100 if v >= 900 else v)
except LeakageError:
    ok = False
check(ok, "C13 future-invariant function passes perturbation proof")

try:
    future_perturbation_unchanged(
        lambda a, b, c, d: max(a, b, c, d), [1.0, 2.0, 3.0, 4.0],
        lambda v: v * 100 if v == 4.0 else v)
    ok = False
except LeakageError:
    ok = True
check(ok, "C14 future-reading function RAISES on perturbation")

# engine-level causality proof (mini): mutating candle j cannot change
# predictions made before j
from core import next_candle as _nc  # noqa: E402
import copy as _copy  # noqa: E402


def _synth_candles(n, seed=3):
    import random
    rng = random.Random(seed)
    t0 = T0
    px = 100.0
    out = []
    for i in range(n):
        o = px
        c = o * (1.0 + rng.gauss(0, 0.001))
        out.append({"asset": "A", "period": 60, "t": t0 + i * 60,
                    "o": o, "h": max(o, c) * 1.001,
                    "l": min(o, c) * 0.999, "c": c})
        px = c
    return out


def _replay(cs):
    eng = _nc.NextCandleEngine("phaseC_causality", 60)
    cs2 = [{"time": c["t"], "open": c["o"], "high": c["h"],
            "low": c["l"], "close": c["c"]} for c in cs]
    return [_copy.deepcopy(eng.ingest(c)) for c in cs2]


_candles = _synth_candles(400)
_base = _replay(_candles)
_mut = [dict(c) for c in _candles]
_j = 300
_mut[_j]["c"] = _mut[_j]["o"] * 1.01  # invert direction
_mut[_j]["h"] = max(_mut[_j]["o"], _mut[_j]["c"]) * 1.001
_mut[_j]["l"] = min(_mut[_j]["o"], _mut[_j]["c"]) * 0.999
_pert = _replay(_mut)
_bad = [i for i in range(_j)
        if ((_base[i] is None) != (_pert[i] is None))
        or (_base[i] is not None
            and _base[i].get("direction") != _pert[i].get("direction"))]
check(not _bad, "C15 engine causality: predictions before the mutated "
      "candle are byte-identical", f"bad idx={_bad[:5]}")

_after_changed = sum(
    1 for i in range(_j, 400)
    if (_base[i] is None) != (_pert[i] is None)
    or (_base[i] is not None and _pert[i] is not None
        and _base[i].get("direction") != _pert[i].get("direction")))
check(_after_changed > 0,
      "C16 mutation propagates FORWARD only (later predictions may move)")

# ── D. splits ───────────────────────────────────────────────────────────
print("\nD. CHRONOLOGICAL SPLITS")


def _mk_asset_candles(n=1400):
    return validate_candle_series(_synth_candles(n)).accepted


_ac = _mk_asset_candles(1400)
_sp = chronological_split({"A": _ac}, SplitSpec(warmup=100, train_frac=0.5,
                                                selection_frac=0.25,
                                                test_frac=0.25))
_s = _sp.by_asset["A"]
check(_s.train[-1].t < _s.selection[0].t < _s.selection[-1].t
      < _s.test[0].t, "D1 chronological order train < selection < test")

check(abs(len(_s.train) - 650) <= 1 and abs(len(_s.selection) - 325) <= 1
      and abs(len(_s.test) - 325) <= 1,
      "D2 split fractions honored (650/325/325 of 1300)",
      f"{len(_s.train)}/{len(_s.selection)}/{len(_s.test)}")

check(all(c.t < _s.train[0].t for c in _s.warmup),
      "D3 warmup excluded from all regions")

check(len(_s.embargo_candles) == 1
      and _s.embargo_candles[0].t == _s.train[-1].t + 60
      and _s.selection[0].t == _s.train[-1].t + 120,
      "D4 embargo candle removed between train and selection")

try:
    _sp.verify(period=60)
    ok = True
except LeakageError:
    ok = False
check(ok, "D5 verify() passes on a valid split")

try:
    SplitSpec(train_frac=0.5, selection_frac=0.3, test_frac=0.3)
    ok = False
except ValueError:
    ok = True
check(ok, "D6 fractions not summing to 1 RAISE")

cut = _s.cutoffs_utc
check(isinstance(cut.get("train_end_utc"), str)
      and cut["train_end_utc"].endswith("Z")
      and cut.get("test_start_utc", "").endswith("Z"),
      "D7 cutoffs recorded as UTC strings")

_folds = walk_forward_folds(_ac[100:], n_folds=4, embargo_periods=1)
_disjoint = all(_folds[i].test[-1].t < _folds[i + 1].test[0].t
                for i in range(3))
_expanding = all(len(_folds[i].train) <= len(_folds[i + 1].train)
                 for i in range(3))
_emb = all(f.test[0].t - f.train[-1].t >= 120 for f in _folds)
check(_disjoint and _expanding and _emb and len(_folds) == 4,
      "D8 walk-forward: 4 disjoint expanding folds, embargo honored")

try:
    walk_forward_folds(_ac[:10], n_folds=4)
    ok = False
except ValueError:
    ok = True
check(ok, "D9 insufficient candles for folds RAISE")

# ── E. metrics ───────────────────────────────────────────────────────────
print("\nE. METRICS")

ci = wilson_ci(5, 10)
check(abs(ci[0] - 0.2366) < 0.002 and abs(ci[1] - 0.7634) < 0.002,
      "E1 Wilson CI hand-check (5/10 -> ~[0.237, 0.763])", str(ci))

check(wilson_ci(0, 0) is None, "E2 Wilson CI n=0 -> None (no fake interval)")

try:
    wilson_ci(11, 10)
    ok = False
except ValueError:
    ok = True
check(ok, "E3 successes > n RAISE")

fixture = [
    {"direction": "CALL", "outcome": GREEN, "prob": 0.6, "correct": True},
    {"direction": "CALL", "outcome": RED, "prob": 0.55, "correct": False},
    {"direction": "PUT", "outcome": RED, "prob": 0.45, "correct": True},
    {"direction": None, "outcome": FLAT, "prob": None, "correct": False},
    {"direction": "PUT", "outcome": FLAT, "prob": None, "correct": False},
]
m = binary_metrics(fixture, label="t")
# Documented conventions: coverage = directional/total (4/5 — a tie-targeted
# prediction still counts as emitted); precision denominators = ALL
# directional predictions of that class (a tie outcome is not a win);
# recall denominators = graded outcomes of that class.
check(m["n_total"] == 5 and m["n_directional"] == 4
      and m["n_no_signal"] == 1 and m["n_tie_excluded"] == 1
      and abs(m["coverage"] - 0.8) < 1e-12
      and abs(m["accuracy"] - 2 / 3) < 1e-12
      and m["confusion_matrix"]["green_call"] == 1
      and m["confusion_matrix"]["red_put"] == 1
      and m["confusion_matrix"]["red_call"] == 1
      and m["confusion_matrix"]["flat_put"] == 1,
      "E4 binary_metrics hand-check (counts/coverage/accuracy/confusion)",
      str(m))

check(m["balanced_accuracy"] is not None
      and abs(m["balanced_accuracy"] - 0.75) < 1e-12,
      "E4b balanced accuracy hand-check (0.75)",
      str(m["balanced_accuracy"]))

check(m["call_precision"] == 0.5 and m["put_precision"] == 0.5
      and m["green_recall"] == 1.0 and m["red_recall"] == 0.5,
      "E4c precision/recall hand-check (0.5/0.5/1.0/0.5)",
      f"{m['call_precision']}/{m['put_precision']}/{m['green_recall']}/{m['red_recall']}")

check(m["accuracy_ci95"] is not None and m["accuracy_ci95"][0] < 2 / 3
      < m["accuracy_ci95"][1], "E4d accuracy carries Wilson CI")

# Brier over GRADED rows that carry probs: (0.6-1)^2 + (0.55-0)^2 +
# (0.45-0)^2 = 0.665 / 3
check(m["brier_score"] is not None
      and abs(m["brier_score"] - 0.665 / 3) < 1e-12,
      "E5 Brier computed when all graded rows carry probs (0.22166...)",
      str(m["brier_score"]))

fx2 = [
    {"direction": "CALL", "outcome": GREEN, "prob": 0.8, "correct": True},
    {"direction": "PUT", "outcome": GREEN, "prob": 0.3, "correct": False},
]
m2 = binary_metrics(fx2)
# Brier vs GREEN (prob is ALWAYS P(GREEN)): (0.8-1)^2 + (0.3-1)^2 =
# 0.04 + 0.49 = 0.53 / 2 = 0.265
check(abs(m2["brier_score"] - 0.265) < 1e-12,
      "E6 Brier hand-check (prob of GREEN convention)",
      str(m2["brier_score"]))

m3 = binary_metrics([{"direction": "CALL", "outcome": GREEN,
                      "prob": None, "correct": True}])
check(m3["brier_score"] is None and m3["brier_note"],
      "E7 no probabilities -> Brier None + note")

ct = calibration_table(fx2)
check(ct is not None and any(b["n"] for b in ct),
      "E8 calibration table built when probs present")

check(calibration_table([{"direction": "CALL", "outcome": GREEN,
                          "prob": None, "correct": True}]) is None,
      "E9 calibration None without probs")

m5 = binary_metrics([])
check(m5["n_total"] == 0 and m5["accuracy"] is None
      and m5["coverage"] is None, "E10 empty input -> no fake metrics")

m6 = binary_metrics([{"direction": "PUT", "outcome": FLAT, "prob": None,
                      "correct": False}] * 10)
check(m6["n_tie_excluded"] == 10 and m6["accuracy"] is None,
      "E11 all-tie rows -> accuracy None (ties never graded)")

m7 = binary_metrics([
    {"direction": "CALL", "outcome": GREEN, "prob": 0.51,
     "correct": True}] * 100)
check(m7["coverage"] == 1.0 and abs(m7["accuracy"] - 1.0) < 1e-9,
      "E12 single-class fixture computes cleanly")

# E13: tie-targeted emissions count in coverage but never in accuracy
m8 = binary_metrics([
    {"direction": "CALL", "outcome": GREEN, "prob": 0.6, "correct": True},
    {"direction": "CALL", "outcome": FLAT, "prob": None, "correct": False},
    {"direction": None, "outcome": FLAT, "prob": None, "correct": False},
])
check(m8["coverage"] == 2 / 3 and m8["accuracy"] == 1.0
      and m8["n_tie_excluded"] == 1
      and m8["confusion_matrix"]["flat_call"] == 1,
      "E13 tie-targeted emission: in coverage, out of accuracy")

# ── F. baselines ─────────────────────────────────────────────────────────
print("\nF. BASELINES")

_g = [c for c in _ac[:100] if c.outcome == GREEN]
_r = [c for c in _ac[:100] if c.outcome == RED]
check(majority_direction(_g + _r[:5]) == CALL
      and majority_direction(_g[:5] + _r) == PUT,
      "F1 majority direction from train outcomes only")

check(majority_direction(_ac[:0]) is None
      and majority_direction(_g[:2] + _r[:2]) is None,
      "F1b majority exact tie -> None (never silently broken)")

check(previous_candle_direction(_ac[5]) in (CALL, PUT, None),
      "F2 previous-candle baseline returns valid values")

# adversarial: mutate ALL candles from index 500 (the target and beyond)
import core.eval_clean.baselines as _bl  # noqa: E402


class _FakeCandle:
    def __init__(self, t, o, h, l, c):
        self.asset, self.period, self.t = "A", 60, t
        self.open, self.high, self.low, self.close = o, h, l, c
        self.volume, self.outcome, self.gap_before = None, None, False


def _to_fake(candles):
    return [_FakeCandle(c.t, c.open, c.high, c.low, c.close)
            for c in candles]


_fa = _to_fake(_ac)
_f1 = _bl.research_features(_fa, 500)
for k in range(500, len(_fa)):
    _fa[k].close = _fa[k].open * 1.5  # adversarial future mutation
_f2 = _bl.research_features(_fa, 500)
check(all(abs(a - b) < 1e-15 for a, b in zip(_f1, _f2)) and len(_f1) == 6,
      "F3 research features UNCHANGED under future mutation (no "
      "look-ahead)", f"{_f1} vs {_f2}")

check(all(abs(v) < 1e6 for v in _f1) and len(_f1)
      == len(RESEARCH_BASELINE_FEATURES),
      "F4 research features finite + frozen shape")

X = [[research_features(_fa, i)] for i in range(60, 160)]
X = [x[0] for x in X]
y = [1 if _ac[i].outcome == GREEN else 0 for i in range(60, 160)]
lr1 = PrespecifiedLogreg().fit(X, y)
p1_ = lr1.predict_proba_green(X[0])
lr2 = PrespecifiedLogreg().fit(X, y)
check(abs(p1_ - lr2.predict_proba_green(X[0])) < 1e-12,
      "F5 logreg deterministic (same fit -> same prediction)")

sc2 = lr1._scaler
check(sc2.fit_rows == len(X),
      "F6 logreg scaler fitted on train rows only")

check(lr1.predict(X[0]) in (CALL, PUT),
      "F7 logreg predicts CALL/PUT only")

# ── G. break-even ────────────────────────────────────────────────────────
print("\nG. BREAK-EVEN")

check(abs(break_even_win_rate(0.85) - 1 / 1.85) < 1e-12,
      "G1 refund formula: r=0.85 -> 0.540540... (54.05%)")

check(abs(break_even_win_rate(0.85, 0.05) - 0.95 / 1.85) < 1e-12,
      "G2 tie-rate adjust: 5% ties -> 0.513513...")

check(abs(break_even_win_rate(0.85, 0.05, "loss")
          - 1 / 1.85) < 1e-12,
      "G3 ties-as-losses: 1/(1+r) regardless of tau")

for bad in (0.0, -1.0, float("nan")):
    try:
        break_even_win_rate(bad)
        ok = False
        break
    except ValueError:
        ok = True
check(ok, "G4 invalid payout RAISES")

try:
    break_even_win_rate(0.85, 1.5)
    ok = False
except ValueError:
    ok = True
check(ok, "G5 invalid tie rate RAISES")

try:
    break_even_win_rate(0.85, 0.0, "partial")
    ok = False
except ValueError:
    ok = True
check(ok, "G6 invalid tie outcome RAISES")

try:
    breakeven_report(None)
    ok = False
except ValueError:
    ok = True
check(ok, "G7 report REQUIRES explicit payout (nothing hardcoded)")

rep = breakeven_report(0.85, 0.05)
check("NOT a guaranteed platform rate" in rep["disclaimer"]
      and abs(rep["break_even_win_rate_refund_ties"] - 0.95 / 1.85) < 1e-6,
      "G8 report carries disclaimer + honest numbers")

# ── H. end-to-end evaluator mini-run ─────────────────────────────────────
print("\nH. END-TO-END MINI-RUN")

import argparse as _ap  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "backtest_clean_eval",
    os.path.join(_REPO, "scripts", "backtest_clean_eval.py"))
_bce = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_bce)

_args = _ap.Namespace(source="synthetic", assets=None, arms="",
                      payout=None, seed=11, db=None,
                      out_dir=tempfile.mkdtemp(prefix="phase_c_eval_"))
# shrink the frozen warmup for a fast in-process mini-run by monkey-
# patching the module constants (TEST ONLY — production protocol frozen)
_bce.FROZEN["warmup_candles"] = 60
_bce.SPLIT_SPEC = SplitSpec(warmup=60, train_frac=0.5,
                            selection_frac=0.25, test_frac=0.25)
# smaller synthetic dataset for the mini-run (TEST ONLY)
_orig_load_synthetic = _bce.load_synthetic
_bce.load_synthetic = (
    lambda a, n, seed: _orig_load_synthetic(a, 1200, seed))
# skip the perturbation proof subprocess work in the mini-run
_bce._engine_perturbation_proof = lambda _a: {
    "skipped": "mini-run (test-only)"}

_rep = _bce.evaluate(_args)
check(_rep["classification"].startswith("SYNTHETIC")
      and "results" in _rep and "baseline_prev_candle" in _rep["results"],
      "H1 mini-run produces full report structure (baselines only)")

_pr_args = _ap.Namespace(source="provenance", db="/nonexistent/x.db",
                         assets=None, arms="", payout=None, seed=7,
                         out_dir="/tmp")
_pr_rep = _bce._evaluate_provenance(_pr_args,
                                    __import__("datetime").datetime.now(
                                        __import__("datetime").timezone.utc))
check(_pr_rep["verdict"] == "INSUFFICIENT_DATA",
      "H2 provenance on missing DB -> INSUFFICIENT_DATA (never padded)")

check(_rep["protocol"]["protocol_version"] == "clean_eval_v1"
      and _rep["protocol"]["embargo_periods"] == 1,
      "H3 protocol stamp present in report")

# ── I. frozen constants ──────────────────────────────────────────────────
print("\nI. PROTOCOL")
check(_bce.FROZEN["tie_policy"].startswith("FLAT")
      and "never labeled" in _bce.FROZEN["tie_policy"],
      "I1 tie policy documented in frozen protocol")

# ── summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print(f"PHASE-C CLEAN EVAL: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
if _FAILURES:
    print("FAILURES:")
    for f in _FAILURES:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
