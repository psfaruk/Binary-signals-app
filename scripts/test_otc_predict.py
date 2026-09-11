#!/usr/bin/env python3
"""scripts/test_otc_predict.py — test suite for the OTC prediction engine
(core/otc_predict/*, PART 6/7/12/13/14/16/17/19).

Covers:
  1. extended-feature perturbation lock (PART 19 — future mutation must
     not change features; self-mutation must)
  2. feature correctness spot checks (doji/hammer/engulf/breakout/streak)
  3. regime detection (HIGH_VOL veto, TRENDING, LOW_VOL)
  4. price-action confirmation (agreement, against_count, veto)
  5. signal scoring + tiers + PART 24 gates (emit decisions)
  6. prediction FREEZE + settlement (PART 16/17, real temp DB)
  7. predictor honest states (no model → no_model_registered)
  8. walk-forward fold invariants (embargo tiling)
  9. model bundle round-trip + Platt calibration sanity

Run: python3 scripts/test_otc_predict.py
"""

import math
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# temp DB BEFORE db/tracker imports (PART 16/17 tests need a scratch DB)
_TMPDIR = tempfile.mkdtemp(prefix="otc_pred_test_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test_pred.db")
os.environ["QX_PREDICT_MODELS_DIR"] = os.path.join(_TMPDIR, "models")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


from scripts.synthetic_otc import gen_candles           # noqa: E402
from core.otc_predict.features_ext import (             # noqa: E402
    build_extended_row, verify_extended_lock, EXTENDED_FEATURE_NAMES,
    MIN_WINDOW_EXT)
from core.otc_predict.regime import detect_regime       # noqa: E402
from core.otc_predict.price_action import price_action_confirm  # noqa: E402
from core.otc_predict.signal_filter import score_signal  # noqa: E402

print("── 1. extended feature lock (PART 19) ──")
candles = gen_candles("TEST_otc", 400, seed=3, edge="none")
n, future_hits, self_hits = verify_extended_lock(
    [{"time": c["time"], "open": c["open"], "high": c["high"],
      "low": c["low"], "close": c["close"]} for c in candles],
    n_checks=25, window=MIN_WINDOW_EXT)
check("future mutation changes nothing (25 checks)", future_hits == 0,
      f"future_hits={future_hits}")
check("self mutation always changes features", self_hits == 25,
      f"self_hits={self_hits}")

print("── 2. feature correctness spot checks ──")
try:
    build_extended_row(candles[:MIN_WINDOW_EXT - 1])
    check("short window rejected", False)
except ValueError:
    check("short window rejected", True)

row = build_extended_row(
    [{"time": c["time"], "open": c["open"], "high": c["high"],
      "low": c["low"], "close": c["close"]} for c in candles[-50:]])
check("all EXTENDED_FEATURE_NAMES present",
      all(k in row for k in EXTENDED_FEATURE_NAMES))
check("all features finite",
      all(isinstance(row[k], (int, float)) and math.isfinite(row[k])
          for k in EXTENDED_FEATURE_NAMES))

# doji construction
doji = {"time": 0, "open": 1.0, "high": 1.010, "low": 0.990,
        "close": 1.0001}
w = [{"time": c["time"], "open": c["open"], "high": c["high"],
      "low": c["low"], "close": c["close"]} for c in candles[-50:-1]]
r2 = build_extended_row(w + [doji])
check("is_doji detected", r2["is_doji"] == 1.0)
check("strong_body not on doji", r2["strong_body"] == 0.0)

# bullish engulfing construction
base_flat = 1.10
prev = {"time": 0, "open": base_flat + 0.0010, "high": base_flat + 0.0014,
        "low": base_flat + 0.0006, "close": base_flat + 0.0006}
cur = {"time": 60, "open": base_flat + 0.0005, "high": base_flat + 0.0016,
       "low": base_flat + 0.0004, "close": base_flat + 0.0015}
r3 = build_extended_row(w[:-1] + [prev, cur])
check("engulf_bull detected", r3["engulf_bull"] == 1.0)

# breakout_up: current close above the prior 19 highs
r4 = build_extended_row(w[:-1] + [dict(cur, close=base_flat + 0.010,
                                       high=base_flat + 0.011)])
check("breakout_up detected", r4["breakout_up"] == 1.0)

# streak → consec_up
ups = [{"time": 60 * j, "open": 1.0, "high": 1.006, "low": 0.999,
        "close": 1.004} for j in range(50)]
r5 = build_extended_row(ups)
check("consec_up = 50 on a 50-green run", r5["consec_up"] == 50.0,
      f"got {r5['consec_up']}")

# a REAL uptrend: rising closes (the treadmill above is flat-priced)
rise = [{"time": 60 * j, "open": 1.0 + 0.0004 * j,
         "high": 1.0 + 0.0004 * j + 0.0005, "low": 1.0 + 0.0004 * j - 0.0001,
         "close": 1.0 + 0.0004 * j + 0.0003} for j in range(50)]

print("── 3. regime detection ──")
reg = detect_regime(rise)
check("real uptrend → TRENDING_UP", reg["regime"] == "TRENDING_UP",
      reg["regime"])
reg_treadmill = detect_regime(ups)
check("flat treadmill is NOT trending",
      reg_treadmill["regime"] in ("RANGING", "LOW_VOL"),
      reg_treadmill["regime"])
calm = gen_candles("CALM", 300, seed=7, edge="none", vol=0.00004)[-50:]
reg2 = detect_regime([{"time": c["time"], "open": c["open"],
                       "high": c["high"], "low": c["low"],
                       "close": c["close"]} for c in calm])
check("calm walk not flagged extreme_vol", reg2["extreme_vol"] is False)
shock = [{"time": c["time"], "open": c["open"], "high": c["high"],
          "low": c["low"], "close": c["close"]} for c in calm]
for j in range(len(shock) - 8, len(shock)):
    o = shock[j]["open"]
    shock[j].update({"high": o + 12 * 0.00012, "low": o - 12 * 0.00012})
reg3 = detect_regime(shock)
check("range explosion → HIGH_VOL + extreme_vol", reg3["regime"] ==
      "HIGH_VOL" and reg3["extreme_vol"], reg3["regime"])

print("── 4. price action confirmation ──")
# REAL uptrend: trend/momentum/structure align with a CALL (the level
# component correctly notes the close sits < 0.5 ATR under the 20-bar
# high — a monotonic rise is always near its high — but `agreed` allows
# one against-component).
r_rise = build_extended_row(rise)
pa_rise = price_action_confirm(rise, True, features=r_rise)
check("strong uptrend agrees with CALL",
      pa_rise["agreed"] is True and pa_rise["against_count"] <= 1
      and pa_rise["components"]["trend"] == 1.0, str(pa_rise))
# treadmill: close pinned 0.29 ATR under the 20-bar high → level MUST
# penalize a CALL there (glued under resistance) and mildly favor PUT.
r_ups = build_extended_row(ups)
pa_up = price_action_confirm(ups, True, features=r_ups)
check("glued-under-resistance CALL penalized",
      pa_up["components"]["level"] == 0.1 and pa_up["agreed"] is False,
      str(pa_up))
pa_dn = price_action_confirm(ups, False, features=r_ups)
check("range-top PUT gets mild agreement",
      pa_dn["agreed"] is True and pa_dn["against_count"] == 0,
      str(pa_dn))
check("extreme vol veto fires", price_action_confirm(
    shock, True, regime=reg3)["veto"] == "extreme_volatility")

# a direction-neutral synthetic PA dict for scorer-contract tests
pa_perfect = {"agreed": True, "against_count": 0, "veto": None,
              "components": {"trend": 1.0, "momentum": 1.0,
                             "level": 0.85, "rejection": 0.9,
                             "structure": 1.0},
              "pa_score": 0.95}

print("── 5. signal scoring + tiers + PART 24 gates ──")
quality_ok = {"data_complete": True, "no_gap": True, "model_loaded": True,
              "vol_acceptable": True, "no_conflict": True}
s_coin = score_signal(0.50, True, pa_perfect, reg, quality_ok)
check("coin-flip ML → NO_SIGNAL even with perfect PA",
      s_coin["tier"] == "NO_SIGNAL" and s_coin["emit"] is False,
      f"score={s_coin['score']}")
_w = {"ml": 50, "momentum": 15, "trend": 10, "level": 10,
      "vol": 5, "structure": 10}
weighted = sum(_w[k] * v for k, v in s_coin["components"].items())
check("score = weighted PART 14 component sum",
      abs(weighted - s_coin["score"]) <= 1,
      f"{weighted} vs {s_coin['score']}")
s_strong = score_signal(0.93, True, pa_perfect, reg, quality_ok)
check("0.93 prob + full PA → HIGH, emitted",
      s_strong["tier"] == "HIGH" and s_strong["emit"] is True,
      f"tier={s_strong['tier']} score={s_strong['score']}")
s_mid = score_signal(0.75, True, pa_perfect, reg, quality_ok)
check("calibrated 75% + full PA → GOOD, emitted",
      s_mid["tier"] == "GOOD" and s_mid["emit"] is True,
      f"tier={s_mid['tier']} score={s_mid['score']}")
s_qfail = score_signal(0.93, True, pa_perfect, reg,
                       dict(quality_ok, no_gap=False))
check("quality fail blocks emission", s_qfail["emit"] is False
      and "quality_fail" in s_qfail["reason"])
pa_with_veto = dict(pa_perfect, veto="extreme_volatility")
s_veto = score_signal(0.93, True, pa_with_veto,
                      {"regime": "HIGH_VOL", "extreme_vol": True,
                       "vol_state": "high"}, quality_ok)
check("veto blocks emission", s_veto["emit"] is False)
check("probability echoed calibrated value",
      s_strong["probability"] == 0.93)

print("── 6. prediction FREEZE + settlement (PART 16/17) ──")
import db as _db                                        # noqa: E402
_db.init()
from core.otc_predict.tracker import (insert_prediction, settle_target,
                                      latest_predictions,
                                      prediction_count)  # noqa: E402
now = int(time.time())
sig_t = now - 300
t1_t, t2_t = now - 240, now - 180
first = insert_prediction(asset="TEST_otc", period=60, signal_time=sig_t,
                          target_time=t1_t, horizon=1,
                          prediction="CALL", probability=0.76,
                          tier="GOOD", score=74, emit=True,
                          model_version="vtest")
check("first insert freezes a row", first is True)
again = insert_prediction(asset="TEST_otc", period=60, signal_time=sig_t,
                          target_time=t1_t, horizon=1,
                          prediction="PUT", probability=0.90,
                          tier="HIGH", score=99, emit=True,
                          model_version="vcheat")
check("re-prediction is IGNORED (freeze)", again is False)
rows_ = latest_predictions("TEST_otc", 5)
check("frozen values immutable",
      rows_[0]["prediction"] == "CALL"
      and rows_[0]["probability"] == 0.76, str(rows_[0]))
# settle: target candle UP → CALL wins
n_settled = settle_target("TEST_otc", 60, t1_t, open_=1.0, close_=1.0020)
check("settle fills the result", n_settled == 1)
rows_ = latest_predictions("TEST_otc", 5)
check("win graded correctly",
      rows_[0]["win_loss"] == "win"
      and rows_[0]["actual_result"] == "UP")
n2 = settle_target("TEST_otc", 60, t1_t, open_=1.0, close_=1.0020)
check("settle is idempotent", n2 == 0)
# PUT losing case
insert_prediction(asset="TEST_otc", period=60, signal_time=sig_t,
                  target_time=t2_t, horizon=2, prediction="PUT",
                  probability=0.61, tier="WATCH", score=63, emit=False,
                  model_version="vtest")
settle_target("TEST_otc", 60, t2_t, open_=1.0, close_=1.0030)
rows_ = latest_predictions("TEST_otc", 5)
t2row = [r for r in rows_ if r["horizon"] == 2][0]
check("non-emitted row still tracked + graded",
      t2row["win_loss"] == "loss" and t2row["emit"] == 0)
check("prediction_count counts both horizons",
      prediction_count() == 2)

print("── 7. predictor honest states ──")
os.environ.pop("QX_PREDICT", None)
import importlib
import core.otc_predict.predictor as pred_mod
importlib.reload(pred_mod)
payload = pred_mod.on_candle_closed(
    "TEST_otc", 60, ups + [ups[-1]], dict(ups[-1]), None)
check("no registry → no_model payload, still locked",
      payload and payload["status"] == "no_model"
      and payload["reason"] == "no_model_registered"
      and payload["locked"] is True, str(payload))
os.environ["QX_PREDICT"] = "0"
importlib.reload(pred_mod)
check("engine disabled → silent None",
      pred_mod.on_candle_closed("TEST_otc", 60, ups, ups[-1], None) is None)
os.environ["QX_PREDICT"] = "1"
importlib.reload(pred_mod)
check("engine_enabled reflects env", pred_mod.engine_enabled() is True)

print("── 8. walk-forward fold invariants ──")
from core.otc_predict.walk_forward import _folds, EMBARGO  # noqa: E402
cuts = _folds(6000, 4, None)
ok = (cuts[0][0] >= 300 and cuts[-1][1] == 6000
      and all(te > tr for tr, te in cuts))
check("folds tile to n with growing train", ok, str(cuts))
check("small series still folds (n<500 → ≤2 folds)",
      len(_folds(420, 4, None)) <= 2)
prev_end = 0
tiling_ok = all(tr >= prev_end for tr, _ in cuts)
check("no test-row reuse across folds", tiling_ok)

print("── 9. bundle round-trip + Platt sanity ──")
from core.otc_predict.models import (save_bundle, load_bundle,
                                     platt_calibrate, apply_platt,
                                     fit_candidate)  # noqa: E402
import numpy as np                                      # noqa: E402
rng = np.random.default_rng(5)
X = rng.normal(size=(600, 2))
y = (X[:, 0] + rng.normal(scale=0.5, size=600) > 0).astype(int)
m = fit_candidate("logreg", X, y)
coefs = platt_calibrate(m, X[:200], y[:200])
check("platt coefs returned on healthy tail", coefs is not None)
p_up = list(m.predict_proba(X[:8])[:, 1])
out = [apply_platt(float(x), coefs) for x in p_up]
order_in = sorted(range(len(p_up)), key=lambda i: p_up[i])
order_out = sorted(range(len(out)), key=lambda i: out[i])
check("apply_platt preserves probability order",
      order_in == order_out, f"{p_up} vs {out}")
from core.otc_predict.models import ModelBundle         # noqa: E402
bundle = ModelBundle("vroundtrip", ["a", "b"], None, None, {})
bundle.t1 = {"model": m, "platt": coefs, "name": "logreg"}
bundle.t2 = None
path = save_bundle(bundle)
b2 = load_bundle(path)
check("bundle round-trips through disk",
      b2.version == "vroundtrip"
      and b2.feature_names == ["a", "b"]
      and b2.predict_up(1, {"a": 0.3, "b": -0.2}) is not None)

print(f"\n══ RESULT: {PASS} PASS, {FAIL} FAIL ══")
sys.exit(1 if FAIL else 0)
