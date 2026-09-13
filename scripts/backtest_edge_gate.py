#!/usr/bin/env python3
"""scripts/backtest_edge_gate.py — EDGE-GUARD emission-rule verification
(2026-09-13).

USER COMPLAINT (verbatim): "কিন্তু প্রেডিকশন ক্যান্ডেল ভুল হয়, মানে
ডিরেকশন wrong দেখানো হয়। লস বেশি হচ্ছে, উইন কম হচ্ছে।"

THE QUESTION this backtest answers, with numbers:

  On data where the model has NO real edge (fair random walk — the honest
  stand-in for the measured real-OTC situation: 49.9% walk-forward), does
  the OLD emission rule still sell signals (→ guaranteed payout loss),
  and does the NEW edge-gated rule refuse?

  On data with a real persistence edge, how many signals does each rule
  emit and at what win rate? (The new rule should emit fewer but better.)

METHOD — faithful replay of the live scoring path:

  * Walk-forward (expanding folds, EMBARGO) trains logreg per fold and
    produces OUT-OF-SAMPLE calibrated probabilities — never in-sample.
  * Every out-of-sample row goes through the REAL
    core.otc_predict.signal_filter.score_signal() with the row's own
    frozen features (PA components read the row exactly like live reads
    the feature dict), the frozen strategy votes (sv_*) and the frozen
    hist engine values (hist_*).
  * OLD rule  = score_signal with the edge gates disabled via env
                (QX_PRED_REQUIRE_VERIFIED=0, QX_PRED_MIN_PROB=0.5,
                 QX_PRED_EMIT_T2=1, QX_PRED_SECOND_VOICE=0) — the exact
                behaviour that shipped before EDGE-GUARD.
  * NEW rule  = defaults (verified model, 0.60 band, T+2 off, second
                voice required) + the live circuit breaker simulated in
                sequence: a pair whose emitted Wilson LB drops below
                break-even − tol at n ≥ 40 stops emitting until its frozen
                directions beat coin-flip + margin again.
  * Grade: CALL wins iff y*_up, PUT wins iff not; EV per signal at the
    configured payout (default 0.85): EV = p_win × payout − (1 − p_win).

HONESTY: synthetic data only. The fair scenario's job is to measure the
RULES' behaviour on zero edge (the real-data situation), not to claim any
win rate on live OTC. Real-market quality is measured by the live guard
itself from the frozen otc_predictions rows.

Run:  python scripts/backtest_edge_gate.py
"""
import importlib
import json
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

N_PAIRS = 6
N_CANDLES = 2200
SEED = 20260913
PAYOUT = float(os.environ.get("QX_PAYOUT", "0.85"))
BREAKEVEN = 1.0 / (1.0 + PAYOUT)
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backtest_edge_gate_report.json")

from core.otc_dataset import build_dataset
from core.otc_predict.features_ext import (
    build_unified_row, UNIFIED_FEATURE_NAMES)
from core.otc_predict.hist_stats import enrich_rows
from core.otc_predict import fast_train
from core.otc_predict.walk_forward import _fit_predict_fold, EMBARGO
from scripts.synthetic_otc import gen_candles

PAIR_NAMES = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDCAD_otc",
              "NZDUSD_otc", "USDCHF_otc"]


def gen_pairs(edge):
    out = {}
    for i, a in enumerate(PAIR_NAMES[:N_PAIRS]):
        out[a] = gen_candles(a, N_CANDLES, seed=SEED + i, edge=edge,
                             phi=0.12)
    return out


def wf_out_of_sample(rows, feature_names, horizon_key="y1_up"):
    """Out-of-sample (asset, row, p) triples via expanding walk-forward."""
    by_asset = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(r)
    out = []
    for asset, arows in sorted(by_asset.items()):
        n = len(arows)
        if n < 550:
            continue
        cuts = fast_train._fast_folds(n)
        for (tr_end, te_end) in cuts:
            res = _fit_predict_fold(arows, feature_names, horizon_key,
                                    ["logreg"], tr_end, te_end)
            _, p, y, _ = res[0]
            for k, (pi, yi) in enumerate(zip(p, y)):
                r = arows[tr_end + EMBARGO + k]
                out.append((asset, r, float(pi), int(yi)))
    return out


def _regime_from_row(row):
    """Regime approximation from the row's frozen vol features (the window
    itself is not kept — this mirrors detect_regime's ratio rules)."""
    ratio = row.get("vol_expansion") or 1.0
    if ratio > 2.5:
        return {"regime": "HIGH_VOL", "vol_state": "high",
                "extreme_vol": True}
    if ratio < 0.55:
        return {"regime": "LOW_VOL", "vol_state": "low",
                "extreme_vol": False}
    return {"regime": "RANGING", "vol_state": "normal",
            "extreme_vol": False}


_DUMMY_WINDOW = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}] * 30


def _strategy_from_row(row):
    """strategy_bridge summary reconstructed from the frozen sv_* block."""
    voter_frac = row.get("sv_voter_frac") or 0.0
    voters = int(round(voter_frac * 13))
    if voters <= 0:
        return None
    net = row.get("sv_net") or 0.0
    agree = row.get("sv_agree_frac") or 0.0
    return {"direction": ("CALL" if net > 0 else
                          "PUT" if net < 0 else "NEUTRAL"),
            "net": net, "voters": voters,
            "agree_count": int(round(agree * voters)),
            "against_count": int(round((1.0 - agree) * voters))}


def _score_row(sf_mod, row, p, horizon, model_status):
    """Run the REAL score_signal for one out-of-sample row."""
    direction_up = p >= 0.5
    from core.otc_predict.price_action import price_action_confirm
    pa = price_action_confirm(_DUMMY_WINDOW, direction_up, features=row,
                              regime=_regime_from_row(row))
    quality = {"data_complete": True, "no_gap": True,
               "model_loaded": True, "vol_acceptable": True,
               "no_conflict": True, "live_edge_ok": True}
    hist_agrees = None
    hp = row.get("hist_p_up_t1") if horizon == 1 else row.get("hist_p_up_t2")
    if hp is not None and abs(float(hp) - 0.5) > 1e-9:
        hist_agrees = (float(hp) >= 0.5) == direction_up
    return sf_mod.score_signal(
        p, direction_up, pa, _regime_from_row(row), quality,
        strategy=_strategy_from_row(row), model_status=model_status,
        horizon=horizon, hist_agrees=hist_agrees)


def _wilson_lb(wins, n, z=1.96):
    if n <= 0:
        return 0.0
    ph = wins / n
    denom = 1 + z * z / n
    centre = ph + z * z / (2 * n)
    margin = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def _apply_rule(sf_mod, oos, horizon, model_status, use_guard):
    """Replay the emission rule over the out-of-sample stream.

    use_guard=True additionally simulates the live circuit breaker:
    per pair, emitted history is watched; at n >= 40 with Wilson LB below
    break-even − 1pp the pair suspends until its frozen directions
    (tracked rows count too) beat coin-flip + 1.5pp again.
    """
    per_pair_emit = {}      # asset → [wins, losses, n] EMITTED
    per_pair_all = {}       # asset → [wins, n] ALL frozen directions
    suspended = {}          # asset → bool
    stats = {"emitted": 0, "wins": 0, "losses": 0, "draws": 0,
             "suppressed_by_guard": 0}
    for asset, row, p, y in oos:
        filt = _score_row(sf_mod, row, p, horizon, model_status)
        pred_up = filt["prediction"] == "CALL"
        won = None if abs(p - 0.5) < 1e-9 else bool(pred_up) == bool(y)
        a_all = per_pair_all.setdefault(asset, [0, 0])
        if won is not None:
            a_all[0] += 1 if won else 0
            a_all[1] += 1

        emit = filt["emit"]
        if emit and use_guard:
            if suspended.get(asset):
                emit = False
                stats["suppressed_by_guard"] += 1
        if emit:
            a_e = per_pair_emit.setdefault(asset, [0, 0, 0])
            stats["emitted"] += 1
            if won is None:
                stats["draws"] += 1
                a_e[2] += 1
            elif won:
                stats["wins"] += 1
                a_e[0] += 1
            else:
                stats["losses"] += 1
                a_e[1] += 1

        if use_guard:
            w, l, d = per_pair_emit.get(asset, [0, 0, 0])
            n_e = w + l
            aw, an = per_pair_all.get(asset, [0, 0])
            if n_e >= 40:
                if suspended.get(asset):
                    if an >= 40 and (aw / an) >= 0.5 + 0.015:
                        suspended[asset] = False   # directions recovered
                elif _wilson_lb(w, n_e) < BREAKEVEN - 0.01:
                    suspended[asset] = True
    dec = stats["wins"] + stats["losses"]
    stats["win_rate_pct"] = round(100.0 * stats["wins"] / dec, 2) if dec \
        else None
    stats["ev_per_signal"] = (
        round(stats["win_rate_pct"] / 100.0 * PAYOUT
              - (1 - stats["win_rate_pct"] / 100.0), 4)
        if dec else None)
    stats["total_pnl_units"] = (
        round(stats["wins"] * PAYOUT - stats["losses"], 2)
        if dec else 0)
    return stats


def _configure_and_reload(mode):
    """Set the edge-gate env for OLD / NEW and reload signal_filter."""
    if mode == "old":
        os.environ["QX_PRED_REQUIRE_VERIFIED"] = "0"
        os.environ["QX_PRED_MIN_PROB"] = "0.5"
        os.environ["QX_PRED_EMIT_T2"] = "1"
        os.environ["QX_PRED_SECOND_VOICE"] = "0"
    else:   # new — the shipped defaults
        for k in ("QX_PRED_REQUIRE_VERIFIED", "QX_PRED_MIN_PROB",
                  "QX_PRED_EMIT_T2", "QX_PRED_SECOND_VOICE"):
            os.environ.pop(k, None)
    from core.otc_predict import signal_filter as sf
    return importlib.reload(sf)


def main():
    t0 = time.time()
    print("EDGE-GATE EMISSION BACKTEST (2026-09-13)")
    print("=" * 62)
    report = {"generated_at": time.time(), "payout": PAYOUT,
              "breakeven_pct": round(100 * BREAKEVEN, 2),
              "pairs": N_PAIRS, "candles_per_pair": N_CANDLES,
              "seed": SEED, "scenarios": {}}

    for edge, label in (("none", "fair_random_walk_zero_edge"),
                        ("persistence", "persistence_edge")):
        print(f"\n── scenario: {label} ──")
        candles = gen_pairs(edge)
        rows, dstats = build_dataset(candles, window=50, micro=False,
                                     feature_fn=build_unified_row)
        rows = enrich_rows(rows)
        oos1 = wf_out_of_sample(rows, UNIFIED_FEATURE_NAMES, "y1_up")
        oos2 = wf_out_of_sample(rows, UNIFIED_FEATURE_NAMES, "y2_up")
        print(f"dataset rows={dstats['rows']}  "
              f"out-of-sample t1={len(oos1)} t2={len(oos2)}")

        scen = {"out_of_sample_t1": len(oos1),
                "out_of_sample_t2": len(oos2), "modes": {}}

        for mode in ("old", "new"):
            sf = _configure_and_reload(mode)
            for horizon, oos, hh in ((1, oos1, "t1"), (2, oos2, "t2")):
                # model_status: "verified" for the rule comparison — the
                # gates' discriminating power is being measured, with the
                # verified bar exercised separately below.
                st = _apply_rule(sf, oos, horizon, "verified",
                                 use_guard=(mode == "new"))
                scen["modes"][f"{mode}_{hh}"] = st
                wr = st["win_rate_pct"]
                ev = st["ev_per_signal"]
                print(f"  [{mode:3}] {hh}: emitted={st['emitted']:5} "
                      f"wr={wr if wr is not None else '—'}% "
                      f"EV/sig={ev if ev is not None else '—'} "
                      f"guard_cut={st.get('suppressed_by_guard', 0)}")

        # provisional-model case: the NEW rule must refuse to emit at all
        sf = _configure_and_reload("new")
        st_prov = _apply_rule(sf, oos1, 1, "provisional", use_guard=False)
        scen["modes"]["new_t1_provisional_model"] = st_prov
        print(f"  [new] t1 PROVISIONAL model: "
              f"emitted={st_prov['emitted']} (must be 0)")

        report["scenarios"][label] = scen

    _configure_and_reload("new")     # leave env clean for the process

    # ── verdict ────────────────────────────────────────────────────────
    fair = report["scenarios"]["fair_random_walk_zero_edge"]["modes"]
    verdict = []
    if fair.get("new_t1", {}).get("emitted", 0) <= \
            0.05 * max(1, fair.get("old_t1", {}).get("emitted", 1)):
        verdict.append("PASS: zero-edge data — new rule stays silent "
                       "(old rule kept selling coin-flips)")
    else:
        verdict.append("CHECK: new rule still emits on zero-edge data")
    if fair.get("new_t1_provisional_model", {}).get("emitted") == 0:
        verdict.append("PASS: provisional models never emit")
    else:
        verdict.append("FAIL: provisional model emitted")
    old_t1 = fair.get("old_t1", {})
    if old_t1.get("win_rate_pct") is not None:
        verdict.append(
            f"old-rule fair WR={old_t1['win_rate_pct']}% vs break-even "
            f"{round(100 * BREAKEVEN, 2)}% — this is the measured shape of "
            f"the user's 'লস বেশি হচ্ছে'")
    report["verdict"] = verdict
    for v in verdict:
        print("  • " + v)

    report["secs"] = round(time.time() - t0, 1)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nreport → {REPORT_PATH} ({report['secs']}s)")


if __name__ == "__main__":
    main()
