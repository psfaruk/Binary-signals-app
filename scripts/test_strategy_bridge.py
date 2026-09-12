"""scripts/test_strategy_bridge.py — UNIFIED-SIGNAL (2026-09-13) tests.

Covers:
  1. strategy_votes contract — 13 module features + clusters + scalars,
     bounded values, deterministic, CALL/PUT sign convention
  2. LEAK-SAFETY (PART 19) — perturbing every FUTURE candle changes nothing;
     perturbing the last closed candle DOES (non-vacuity)
  3. build_unified_row — superset of extended names, all keys present
  4. agreement score math — agree=1.0 / oppose=0.0 / abstain=0.5
  5. score_signal with strategy — component present, weight behaviour,
     backward compat (strategy=None keeps old-style score structure)
  6. model_performance cross-tab — synthetic frozen rows → correct matrix,
     Wilson-LB ranking, by_type aggregation
  7. live predictor payload carries the strategy verdict (frozen row too)
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {extra}")


def synth_candles(n=120, seed=7, period=60, t0=1700000000):
    rng = random.Random(seed)
    out = []
    price = 1.1000
    for i in range(n):
        o = price
        c = o + rng.gauss(0, 0.00012)
        h = max(o, c) + abs(rng.gauss(0, 0.00006))
        l = min(o, c) - abs(rng.gauss(0, 0.00006))
        out.append({"time": t0 + i * period, "open": o, "high": h,
                    "low": l, "close": c})
        price = c
    return out


def main():
    print("== 1. strategy_votes contract ==")
    from core.otc_predict.strategy_bridge import (
        strategy_votes, STRATEGY_FEATURE_NAMES, STRATEGY_MODULE_NAMES,
        CLUSTER_NAMES, strategy_agreement_score, MIN_WINDOW_STRATEGY)
    candles = synth_candles()
    window = candles[-50:]
    feats, summ = strategy_votes(window)

    check("13 module features present",
          all(f"sv_{m}" in feats for m in STRATEGY_MODULE_NAMES))
    check("6 cluster features present",
          all(f"svc_{c}" in feats for c in CLUSTER_NAMES))
    check("scalar features present",
          all(k in feats for k in ("sv_net", "sv_agree_frac",
                                   "sv_voter_frac")))
    check("all values bounded [-1,1]",
          all(-1.0 <= v <= 1.0 for v in feats.values()))
    check("STRATEGY_FEATURE_NAMES matches feats keys",
          set(STRATEGY_FEATURE_NAMES) == set(feats.keys()))
    check("summary direction valid",
          summ["direction"] in ("CALL", "PUT", "NEUTRAL"))
    check("summary counts consistent",
          summ["agree_count"] + summ["against_count"] == summ["voters"])
    check("voters <= 13", summ["voters"] <= 13)
    check("per_module has all 13",
          set(summ["per_module"].keys()) == set(STRATEGY_MODULE_NAMES))

    # determinism
    f2, s2 = strategy_votes(window)
    check("deterministic", feats == f2 and summ == s2)

    # short window → all abstain
    f3, s3 = strategy_votes(candles[:10])
    check("short window abstains",
          all(v == 0.0 for v in f3.values()) and s3["voters"] == 0)

    print("== 2. LEAK-SAFETY (PART 19) ==")
    from core.otc_predict.strategy_bridge import verify_strategy_lock
    n_checks, fut, selfh = verify_strategy_lock(candles, n_checks=15)
    check(f"future mutations invisible ({fut} hits)", fut == 0)
    check(f"self mutations visible ({selfh} hits)", selfh > 0)

    print("== 3. build_unified_row ==")
    from core.otc_predict.features_ext import (
        build_unified_row, build_extended_row, UNIFIED_FEATURE_NAMES,
        EXTENDED_FEATURE_NAMES)
    row = build_unified_row(window, micro=None)
    check("unified is superset of extended",
          set(EXTENDED_FEATURE_NAMES) < set(row.keys()))
    check("unified row carries all sv features",
          all(k in row for k in STRATEGY_FEATURE_NAMES))
    check("UNIFIED names = extended + strategy",
          list(UNIFIED_FEATURE_NAMES) ==
          list(EXTENDED_FEATURE_NAMES) + list(STRATEGY_FEATURE_NAMES))

    print("== 4. agreement score math ==")
    check("full agree -> 1.0",
          abs(strategy_agreement_score(
              {"voters": 5, "net": 1.0}, True) - 1.0) < 1e-9)
    check("full oppose -> 0.0",
          abs(strategy_agreement_score(
              {"voters": 5, "net": 1.0}, False) - 0.0) < 1e-9)
    check("abstain -> 0.5",
          abs(strategy_agreement_score({"voters": 0, "net": 0.0},
                                       True) - 0.5) < 1e-9)
    check("no summary -> 0.5",
          abs(strategy_agreement_score(None, True) - 0.5) < 1e-9)

    print("== 5. score_signal unified component ==")
    from core.otc_predict.signal_filter import score_signal
    pa = {"components": {"momentum": 0.5, "trend": 0.5, "level": 0.5,
                         "structure": 0.5}, "agreed": False,
          "against_count": 0, "veto": None}
    reg = {"regime": "RANGING", "vol_state": "normal"}
    qual = {"data_complete": True, "no_gap": True, "model_loaded": True,
            "vol_acceptable": True, "no_conflict": True}
    base = score_signal(0.5, True, pa, reg, qual, strategy=None)
    check("no-strategy call keeps structure",
          "strategy" in base["components"]
          and base["components"]["strategy"] == 0.5)
    agree = score_signal(0.5, True, pa, reg, qual,
                         strategy={"voters": 6, "net": 1.0})
    oppose = score_signal(0.5, True, pa, reg, qual,
                          strategy={"voters": 6, "net": -1.0})
    check("agree > base > oppose",
          agree["score"] > base["score"] > oppose["score"],
          f"{agree['score']} / {base['score']} / {oppose['score']}")
    check("components.strategy 1.0 / 0.5 / 0.0",
          agree["components"]["strategy"] == 1.0
          and base["components"]["strategy"] == 0.5
          and oppose["components"]["strategy"] == 0.0)
    check("strategy_agree field present",
          agree["strategy_agree"] == 1.0 and oppose["strategy_agree"] == 0.0)

    print("== 6. model_performance cross-tab ==")
    # isolated DB (db.init() creates the schema; env var must be set
    # BEFORE the first db import in this process — reload handles it)
    import db as _db
    test_db = "/tmp/qx_test_mperf.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    os.environ["QX_DB_PATH"] = test_db
    import importlib
    importlib.reload(_db)
    _db.init()
    from core.otc_predict import tracker
    importlib.reload(tracker)

    t0 = 1700000000
    # pair A / model v1: 6 wins 2 losses (T+1), 4/4 (T+2)  -> 10W 2L
    seq_a = (["win"] * 6 + ["loss"] * 2 + ["win"] * 4)
    for i, wl in enumerate(seq_a):
        tracker.insert_prediction(
            asset="AAA_otc", period=60, signal_time=t0 + i * 60,
            target_time=t0 + (i + 1) * 60, horizon=1 if i < 8 else 2,
            prediction="CALL", probability=0.52, tier="WATCH",
            score=40, emit=False, components={}, regime="RANGING",
            pa_agreed=0, quality={}, reason="", model_version="vX-1",
            close_i=1.1)
    # pair B / model v2: 3 wins 7 losses
    for i, wl in enumerate(["win"] * 3 + ["loss"] * 7):
        tracker.insert_prediction(
            asset="BBB_otc", period=60, signal_time=t0 + i * 60,
            target_time=t0 + (i + 1) * 60, horizon=1,
            prediction="PUT", probability=0.51, tier="WATCH",
            score=38, emit=False, components={}, regime="RANGING",
            pa_agreed=0, quality={}, reason="", model_version="vY-2",
            close_i=1.2)
    # settle them: CALL wins iff UP
    for i in range(12):
        up = (i < 8 and seq_a[i] == "win") or \
             (i >= 8 and i != 9)   # A: idx 8..11 wins except idx9? keep wins
    # simpler: settle each by explicit target times
    from core.otc_predict.tracker import settle_target
    for i, wl in enumerate(seq_a):
        settle_target("AAA_otc", 60, t0 + (i + 1) * 60,
                      1.0, 1.5 if wl == "win" else 0.5)
    for i, wl in enumerate(["win"] * 3 + ["loss"] * 7):
        settle_target("BBB_otc", 60, t0 + (i + 1) * 60,
                      1.5, 1.0 if wl == "win" else 2.0)  # PUT wins iff DOWN

    tracker.register_model(
        "AAA_otc", "vX-1", "pair", "AAA_otc",
        {"status": "provisional", "rows": 100,
         "walk_forward": {"y1_up": {"selected": "rf"}}}, "/tmp/x")
    tracker.register_model(
        "BBB_otc", "vY-2", "pair", "BBB_otc",
        {"status": "verified", "rows": 100,
         "walk_forward": {"y1_up": {"selected": "logreg"}}}, "/tmp/y")

    mp = tracker.model_performance()
    rows = {r["asset"]: r for r in mp["rows"]}
    check("two rows in matrix", len(rows) == 2)
    a = rows.get("AAA_otc", {})
    b = rows.get("BBB_otc", {})
    check("AAA dir_win_rate 83.33", abs(
        (a.get("dir_win_rate") or 0) - 83.33) < 0.1,
        str(a.get("dir_win_rate")))
    check("BBB dir_win_rate 30.0", abs(
        (b.get("dir_win_rate") or 0) - 30.0) < 0.1,
        str(b.get("dir_win_rate")))
    check("AAA rated above BBB",
          (a.get("rating") or 0) > (b.get("rating") or 0))
    check("model types joined",
          a.get("model_type") == "rf" and b.get("model_type") == "logreg")
    check("statuses joined",
          a.get("status") == "provisional" and b.get("status") == "verified")
    check("t1/t2 split present",
          a["t1"]["n"] == 8 and a["t2"]["n"] == 4)
    check("by_type has rf & logreg",
          "rf" in mp["by_type"] and "logreg" in mp["by_type"])
    check("by_type rf wins 10",
          mp["by_type"]["rf"]["wins"] == 10)
    # Wilson sanity: 3/10 → LB well below raw
    check("wilson LB < raw for small sample",
          (mp["by_type"]["logreg"]["rating"] or 0) < 30.0)

    print("== 7. live predictor payload strategy verdict ==")
    # fresh DB for the predictor test (avoid polluting state)
    test_db2 = "/tmp/qx_test_unified_pred.db"
    if os.path.exists(test_db2):
        os.remove(test_db2)
    os.environ["QX_DB_PATH"] = test_db2
    importlib.reload(_db)
    _db.init()
    importlib.reload(tracker)
    from core.otc_predict import predictor
    importlib.reload(predictor)

    from core.otc_predict.models import (ModelBundle, save_bundle)
    from core.otc_predict.features_ext import UNIFIED_FEATURE_NAMES
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    Xs = np.random.RandomState(0).rand(300, len(UNIFIED_FEATURE_NAMES))
    ys = (np.random.RandomState(1).rand(300) > 0.5).astype(int)
    m = LogisticRegression(max_iter=200).fit(Xs, ys)
    bundle = ModelBundle("vTest-1", UNIFIED_FEATURE_NAMES,
                         {"model": m, "platt": None, "name": "logreg"},
                         {"model": m, "platt": None, "name": "logreg"},
                         {"status": "provisional", "trained_rows": 300})
    path = save_bundle(bundle, "/tmp/qx_unified_bundle.joblib")
    tracker.register_model("CCC_otc", "vTest-1", "pair", "CCC_otc",
                           {"status": "provisional", "rows": 300,
                            "walk_forward": {"y1_up": {"selected": "logreg"}}},
                           path)
    predictor._cache["checked_at"] = 0.0

    candles_c = synth_candles(n=120, seed=11)
    closed = candles_c[-1]
    payload = predictor.on_candle_closed(
        "CCC_otc", 60, candles_c, closed, None)
    check("payload produced", payload is not None and payload.get("t1"))
    t1 = payload["t1"]
    check("t1 has strategy verdict", isinstance(t1.get("strategy"), dict))
    check("strategy has direction/count fields",
          t1["strategy"].get("direction") in ("CALL", "PUT", "NEUTRAL")
          and "agree_count" in t1["strategy"]
          and "voters" in t1["strategy"])
    check("t1 has strategy_agree in [0,1]",
          0.0 <= (t1.get("strategy_agree") or 0) <= 1.0)
    check("t2 has strategy too",
          isinstance(payload["t2"].get("strategy"), dict))
    check("candle geometry still present",
          isinstance(t1.get("candle"), dict))

    # frozen row components carry the strategy component
    lp = tracker.latest_predictions("CCC_otc", 4)
    check("frozen rows exist", len(lp) >= 2)
    comp = json.loads(lp[0]["components"] or "{}")
    check("frozen components include strategy",
          "strategy" in comp, str(comp.keys()))

    # OLD-style bundle (extended-only names) still predicts (back-compat)
    from core.otc_predict.features_ext import EXTENDED_FEATURE_NAMES
    Xs2 = np.random.RandomState(2).rand(300, len(EXTENDED_FEATURE_NAMES))
    m2 = LogisticRegression(max_iter=200).fit(Xs2, ys)
    old_bundle = ModelBundle("vOld-9", EXTENDED_FEATURE_NAMES,
                             {"model": m2, "platt": None, "name": "logreg"},
                             {"model": m2, "platt": None, "name": "logreg"},
                             {"status": "verified", "trained_rows": 300})
    p2 = save_bundle(old_bundle, "/tmp/qx_old_bundle.joblib")
    tracker.register_model("CCC_otc", "vOld-9", "pair", "CCC_otc",
                           {"status": "verified", "rows": 300,
                            "walk_forward": {"y1_up": {"selected": "logreg"}}},
                           p2)
    predictor._cache["checked_at"] = 0.0
    predictor._cache["bundles"].clear()
    payload2 = predictor.on_candle_closed(
        "CCC_otc", 60, candles_c, closed, None)
    check("old bundle predicts with unified feats dict",
          payload2 is not None and payload2.get("t1")
          and payload2["t1"]["prediction"] in ("CALL", "PUT")
          and payload2["t1"]["probability"] is not None)

    print(f"\n{'=' * 50}\nPASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
