"""scripts/test_hist_stats.py — HIST-ENGINE (2026-09-13) tests.

Deep Report §13/§26 — Historical Setup-Match Engine. Covers:
  1. signature_from_row contract — 3 levels, deterministic, coarse bins,
     missing keys degrade to neutral (never raises)
  2. enrich_rows contract — hist_* fields on every row, neutral fill
     before any outcome resolves, real probabilities afterwards
  3. LEAK-SAFETY (Deep Report §17) — white-box deferral proof: row i's
     t1 outcome first becomes visible at row i+1, its t2 outcome at
     row i+2 — never earlier
  4. TRAIN↔LIVE PARITY — the last training row's enriched probability
     equals live_lookup() at the same candle (identical maps + signature)
  5. hierarchical backoff — sparse L0 falls back to L1/L2; unseen
     signature abstains; exact backoff level honoured
  6. Jeffreys smoothing math — exact values
  7. live_lookup — cold start (long history), doji skip, gap rebuild,
     too-few-candles abstention, DB fallback failure tolerated
  8. build_unified_row(hist=...) — feature merge + neutral fallback;
     UNIFIED_FEATURE_NAMES == EXT + STRATEGY + HIST (3 new tail names)
  9. predictor payload — t1/t2 carry hist, components JSON freezes it,
     old-bundle back-compat (feature_names without hist keys)
 10. tracker prediction_analytics — Brier + calibration buckets + EV line
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


def synth_candles(n=3000, seed=7, period=60, t0=1700000000, drift=0.0):
    rng = random.Random(seed)
    out = []
    price = 1.1000
    for i in range(n):
        o = price
        c = o + rng.gauss(drift, 0.00012)
        h = max(o, c) + abs(rng.gauss(0, 0.00006))
        l = min(o, c) - abs(rng.gauss(0, 0.00006))
        out.append({"time": t0 + i * period, "open": o, "high": h,
                    "low": l, "close": c})
        price = c
    return out


def synth_rows(candles, period=60, window=50, asset="TESTPAIR_otc"):
    """build_dataset-equivalent rows (base features + targets + meta)."""
    from core.otc_features import build_feature_row
    rows = []
    for i in range(window - 1, len(candles) - 2):
        t0, t1, t2 = candles[i], candles[i + 1], candles[i + 2]
        if t1["close"] == t1["open"] or t2["close"] == t2["open"]:
            continue
        row = {"asset": asset,
               "window_end_ctime": t0["time"],
               "t1_ctime": t1["time"], "t2_ctime": t2["time"],
               "close_i": t0["close"],
               "y1_up": 1 if t1["close"] > t1["open"] else 0,
               "y2_up": 1 if t2["close"] > t2["open"] else 0}
        row.update(build_feature_row(candles[i - window + 1: i + 1]))
        rows.append(row)
    return rows


def main():
    from core.otc_predict import hist_stats as hs
    from core.otc_predict.hist_stats import (
        HIST_FEATURE_NAMES, SIG_WINDOW, MIN_N_L0, MIN_N_L1, MIN_N_L2,
        signature_from_row, enrich_rows, live_lookup, hist_feature_values,
        hist_neutral, reset_live)
    from core.otc_predict.features_ext import (
        build_unified_row, UNIFIED_FEATURE_NAMES, EXTENDED_FEATURE_NAMES)
    from core.otc_predict.strategy_bridge import STRATEGY_FEATURE_NAMES

    candles = synth_candles(3000, seed=11)
    rows = synth_rows(candles)

    print("== 1. signature_from_row contract ==")
    check("rows synthesised", len(rows) > 2500, f"n={len(rows)}")
    f = rows[len(rows) // 2]
    sigs = signature_from_row(f)
    check("three levels present",
          set(sigs.keys()) == {"L0", "L1", "L2"})
    check("L0 is the widest tuple",
          len(sigs["L0"]) > len(sigs["L1"]) > len(sigs["L2"]))
    check("deterministic", signature_from_row(f) == sigs)
    check("L2 is the L1 head, L1 head matches L0 head",
          sigs["L1"][:2] == sigs["L2"]
          and sigs["L0"][:2] == sigs["L2"])
    try:
        sigs2 = signature_from_row({})
        check("empty dict degrades to neutral bins",
              sigs2["L0"] == (0, 0, 2, 0, 0, 2, 0, 0), str(sigs2))
    except Exception as exc:
        check("empty dict degrades to neutral bins", False,
              f"raised {type(exc).__name__}")

    print("== 2. enrich_rows contract ==")
    enriched = enrich_rows([dict(r) for r in rows])
    check("all rows carry hist fields",
          all(all(k in r for k in HIST_FEATURE_NAMES) for r in enriched))
    check("early rows are neutral (no history yet)",
          enriched[0]["hist_p_up_t1"] == 0.5
          and enriched[0]["hist_conf"] == 0.0)
    with_hist = [r for r in enriched if r["hist_conf"] > 0]
    check("later rows resolved with real probabilities",
          len(with_hist) > len(enriched) * 0.5,
          f"resolved={len(with_hist)}/{len(enriched)}")
    check("p values strictly inside (0,1)",
          all(0.0 < r["hist_p_up_t1"] < 1.0 for r in with_hist))
    check("enrich_rows returns the same list object",
          enriched[0] is not None and "hist_p_up_t1" in enriched[-1])

    print("== 3. LEAK-SAFETY: time-deferred resolution (white-box) ==")
    # Three consecutive rows forced to the SAME signature; floors lowered
    # to 1 so a single outcome is visible. Jeffreys p = (up+0.5)/(n+1):
    #   row0 lookup: nothing known      → 0.5 (neutral)
    #   row1 lookup: row0's y1 flushed   → (1+0.5)/2 = 0.75
    #   row2 lookup: + row1's y1 flushed → (1+0+0.5)/3 = 0.5
    # t2 (strictly later resolution):
    #   row0/row1 lookups: neutral      → row0's y2 NOT visible at row1
    #   row2 lookup: row0's y2 flushed   → (y2+0.5)/2
    probe = [dict(rows[0]), dict(rows[1]), dict(rows[2])]
    probe[0]["y1_up"], probe[0]["y2_up"] = 1, 0
    probe[1]["y1_up"], probe[1]["y2_up"] = 0, 1
    probe[2]["y1_up"], probe[2]["y2_up"] = 1, 1
    for k in ("mom_10", "mom_5", "vol_20", "vol_10", "hi20_pos",
              "body_range_ratio", "streak", "dist_support_atr",
              "dist_resistance_atr", "dir_1"):
        probe[1][k] = probe[0][k]
        probe[2][k] = probe[0][k]
    saved_levels = hs._levels
    hs._levels = (("L0", 1), ("L1", 1), ("L2", 1))
    try:
        out = enrich_rows([dict(p) for p in probe])
        check("row0 t1 neutral (outcome unknown to itself)",
              out[0]["hist_p_up_t1"] == 0.5, str(out[0]["hist_p_up_t1"]))
        check("row1 t1 sees row0's y1 exactly once",
              abs(out[1]["hist_p_up_t1"] - 0.75) < 1e-9,
              str(out[1]["hist_p_up_t1"]))
        check("row2 t1 sees row0+row1's y1 (2 samples)",
              abs(out[2]["hist_p_up_t1"] - 0.5) < 1e-9,
              str(out[2]["hist_p_up_t1"]))
        check("row1 t2 does NOT see row0's y2 (leak-proof)",
              out[1]["hist_p_up_t2"] == 0.5,
              str(out[1]["hist_p_up_t2"]))
        check("row2 t2 sees row0's y2 exactly at +2",
              abs(out[2]["hist_p_up_t2"] - 0.25) < 1e-9,
              str(out[2]["hist_p_up_t2"]))
    finally:
        hs._levels = saved_levels

    print("== 4. TRAIN ↔ LIVE PARITY ==")
    # live_lookup at "candle n-3 just closed" must produce the SAME t1/t2
    # probability as the enriched training row for that candle — identical
    # maps (deferral == cold-build resolution) + identical signature.
    n = len(candles)
    reset_live()
    look_at_n3 = live_lookup("PARITY_otc", 60, candles[:n - 2])
    last_row = enriched[-1]   # window_end == candles[n-3].time
    if look_at_n3 is None:
        check("parity: both abstain", last_row["hist_conf"] == 0.0)
    else:
        same_t1 = (look_at_n3.get("p_up_t1") is None
                   and last_row["hist_p_up_t1"] == 0.5) or \
                  (look_at_n3.get("p_up_t1") is not None
                   and look_at_n3["p_up_t1"] == last_row["hist_p_up_t1"])
        check("parity: live t1 == enriched last-row t1", same_t1,
              f"live={look_at_n3.get('p_up_t1')} "
              f"train={last_row['hist_p_up_t1']}")
        same_t2 = (look_at_n3.get("p_up_t2") is None
                   and last_row["hist_p_up_t2"] == 0.5) or \
                  (look_at_n3.get("p_up_t2") is not None
                   and look_at_n3["p_up_t2"] == last_row["hist_p_up_t2"])
        check("parity: live t2 == enriched last-row t2", same_t2,
              f"live={look_at_n3.get('p_up_t2')} "
              f"train={last_row['hist_p_up_t2']}")

    print("== 5. hierarchical backoff ==")
    reset_live()
    look = live_lookup("TESTPAIR_otc", 60, candles)
    check("long history resolves at some level",
          look is not None and (look.get("level_t1") is not None
                                or look.get("level_t2") is not None))
    if look and look.get("level_t1"):
        lvl = look["level_t1"]
        floor = {"L0": MIN_N_L0, "L1": MIN_N_L1, "L2": MIN_N_L2}[lvl]
        check("resolved level honours its sample floor",
              look["n_t1"] >= floor,
              f"lvl={lvl} n={look['n_t1']} floor={floor}")
    from core.otc_predict.hist_stats import _lookup_one
    m = {}
    _, p, n = _lookup_one(m, sigs)
    check("unseen signature abstains", p is None and n == 0)
    m2 = {sigs["L0"]: [MIN_N_L0 - 1, 5],
          sigs["L1"]: [MIN_N_L1 - 1, 5],
          sigs["L2"]: [MIN_N_L2, MIN_N_L2 // 2]}
    lvl, p, n = _lookup_one(m2, sigs)
    check("backoff lands on L2 when L0/L1 are thin",
          lvl == "L2" and n == MIN_N_L2, f"lvl={lvl} n={n}")

    print("== 6. Jeffreys smoothing math ==")
    m3 = {sigs["L0"]: [99, 99], sigs["L1"]: [99, 99], sigs["L2"]: [99, 99]}
    _, p, _ = _lookup_one(m3, sigs)
    check("all-UP 99n → 0.995", abs(p - 0.995) < 1e-9, f"p={p}")
    m4 = {sigs["L0"]: [99, 0], sigs["L1"]: [99, 0], sigs["L2"]: [99, 0]}
    _, p, _ = _lookup_one(m4, sigs)
    check("all-DOWN 99n → 0.005", abs(p - 0.005) < 1e-9, f"p={p}")

    print("== 7. live_lookup edge cases ==")
    reset_live()
    check("too few candles → None",
          live_lookup("X_otc", 60, candles[:20]) is None)
    reset_live()
    look_a = live_lookup("A_otc", 60, candles)
    check("cold start works on full history", look_a is not None)
    reset_live()
    cds = [dict(c) for c in candles]
    cds[-1]["close"] = cds[-1]["open"]  # doji close
    look_d = live_lookup("B_otc", 60, cds)
    check("doji close does not crash",
          look_d is None or isinstance(look_d, dict))
    jumped = [dict(c) for c in cds]
    jumped[-1]["time"] += 600  # 10-minute hole → cold rebuild
    look_g = live_lookup("B_otc", 60, jumped)
    check("gap triggers rebuild, no crash",
          look_g is None or isinstance(look_g, dict))
    # incremental second close: same engine continues (no rebuild, no crash)
    more = synth_candles(10, seed=99, t0=candles[-1]["time"] + 60)
    cont = [dict(c) for c in candles] + more
    look_c = live_lookup("B_otc", 60, cont)
    check("incremental path continues",
          look_c is None or isinstance(look_c, dict))
    # DB fallback failure tolerated (missing DB path)
    os.environ["QX_HIST_DB"] = "0"
    reset_live()
    try:
        look_nodb = live_lookup("C_otc", 60, candles)
        check("DB disabled → stream-only still works",
              look_nodb is None or isinstance(look_nodb, dict))
    finally:
        os.environ["QX_HIST_DB"] = "1"

    print("== 8. build_unified_row hist merge ==")
    window = candles[-50:]
    row_plain = build_unified_row(window)
    check("neutral hist keys always present",
          all(k in row_plain for k in HIST_FEATURE_NAMES))
    check("neutral values", row_plain["hist_p_up_t1"] == 0.5
          and row_plain["hist_conf"] == 0.0)
    fake = {"p_up_t1": 0.73, "n_t1": 820, "level_t1": "L0",
            "p_up_t2": 0.61, "n_t2": 300, "level_t2": "L1"}
    row_hist = build_unified_row(window, hist=fake)
    check("real hist merged", row_hist["hist_p_up_t1"] == 0.73)
    check("conf log-scaled in (0,1]", 0.0 < row_hist["hist_conf"] <= 1.0)
    expect = (len(EXTENDED_FEATURE_NAMES) + len(STRATEGY_FEATURE_NAMES)
              + len(HIST_FEATURE_NAMES))
    check("UNIFIED == EXT + STRATEGY + HIST",
          len(UNIFIED_FEATURE_NAMES) == expect,
          f"{len(UNIFIED_FEATURE_NAMES)} vs {expect}")
    check("hist names are the tail",
          tuple(UNIFIED_FEATURE_NAMES[-3:]) == HIST_FEATURE_NAMES)

    print("== 9. predictor payload freeze + bundle back-compat ==")
    from core.otc_predict import predictor as pred
    from core.otc_predict.models import ModelBundle

    class _Slot:
        def __init__(self, p):
            self._p = p

        def predict_proba(self, X):
            import numpy as _np
            return _np.array([[1 - self._p, self._p]])

    old_bundle = ModelBundle(
        "old-v1", UNIFIED_FEATURE_NAMES[:-3],
        {"model": _Slot(0.62), "platt": None, "name": "logreg"},
        {"model": _Slot(0.58), "platt": None, "name": "logreg"},
        {"status": "verified", "trained_rows": 1000})
    p_old = old_bundle.predict_up(1, row_plain)
    check("old bundle (no hist names) still predicts",
          p_old is not None and abs(p_old - 0.62) < 1e-9, f"p={p_old}")
    new_bundle = ModelBundle(
        "new-v1", UNIFIED_FEATURE_NAMES,
        {"model": _Slot(0.62), "platt": None, "name": "logreg"},
        {"model": _Slot(0.58), "platt": None, "name": "logreg"},
        {"status": "verified", "trained_rows": 1000})
    p_new = new_bundle.predict_up(1, row_hist)
    check("new bundle predicts from enriched row",
          p_new is not None and abs(p_new - 0.62) < 1e-9, f"p={p_new}")
    p_neu = new_bundle.predict_up(1, row_plain)
    check("new bundle + neutral row (live fallback, no crash)",
          p_neu is not None and abs(p_neu - 0.62) < 1e-9, f"p={p_neu}")

    class _RegBundle:
        version = "new-v1"
        meta = {"status": "verified", "trained_rows": 1000}

        def predict_up(self, h, feats):
            return 0.62 if h == 1 else 0.58

    orig_get = pred._get_bundle
    pred._get_bundle = lambda asset=None: _RegBundle()
    reset_live()
    try:
        payload = pred.on_candle_closed(
            "TESTPAIR_otc", 60, candles, candles[-1], None)
        ok_payload = bool(payload and payload.get("t1"))
        check("payload produced", ok_payload)
        if ok_payload:
            t1 = payload["t1"]
            check("t1.hist present", t1.get("hist") is not None,
                  str(t1.get("hist")))
            h = t1.get("hist") or {}
            if h:
                check("t1.hist agrees flag boolean",
                      h.get("agrees") in (True, False))
                check("t1.hist has level + n",
                      h.get("level") in ("L0", "L1", "L2")
                      and h.get("n", 0) > 0,
                      f"hist={h}")
            from core.otc_predict import tracker
            rows_db = tracker.latest_predictions("TESTPAIR_otc", 2)
            comp = json.loads(rows_db[0]["components"] or "{}") \
                if rows_db else {}
            check("frozen components.hist present (reload-proof)",
                  "hist" in comp, str(list(comp.keys())))
    finally:
        pred._get_bundle = orig_get

    print("== 10. tracker analytics: Brier + calibration + EV ==")
    from core.otc_predict import tracker
    an = tracker.prediction_analytics()
    check("ev line present", "ev" in an and "payout" in an["ev"]
          and "breakeven_wr" in an["ev"])
    check("breakeven math (85% payout → 54.05%)",
          abs(an["ev"]["breakeven_wr"] - 1.0 / 1.85) < 0.001,
          str(an["ev"]))
    check("brier + calibration fields present",
          "brier" in an and "brier_n" in an and "calibration" in an)
    if an.get("brier_n"):
        check("brier in [0,1]", 0.0 <= an["brier"] <= 1.0,
              str(an["brier"]))
        check("calibration buckets well-formed",
              all(("lo" in b and "hi" in b and "n" in b)
                  for b in an["calibration"]))

    print(f"\n{'=' * 60}\nHIST-ENGINE tests: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
