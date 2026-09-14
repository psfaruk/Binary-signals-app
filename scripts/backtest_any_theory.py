#!/usr/bin/env python3
"""
scripts/backtest_any_theory.py — ANY-THEORY + ML-HAND-OFF backtest (2026-09-14).

USER REQUIREMENTS VERIFIED HERE (verbatim):
  1. "প্রত্যেক ক্যান্ডেল এ সিগন্যাল প্রধান করতে হবে"    → coverage ≈ 100%
     (every candle where ANY theory voted OR the ML model had a prediction)
  2. "fallback signals দেওয়া যাবে না"                   → ZERO heuristic
     fallback signals (no strategy "confluence_v1_fallback", no
     fallback=True, no persistence/htf_fade/body_fade bases)
  3. "যে কোনো একটি পাস হলেই সিগন্যাল দিবে"              → ANY ONE module
     vote emits a real "confluence_v1_any" signal
  4. "মডিউল ইঞ্জিন থেকে সিগন্যাল আসলো না — ML model
     থেকে সিগন্যাল টি আসবে"                              → zero-theory
     candles take the ML model's frozen T+1 prediction (walk-forward
     trained, strictly causal)

Method — walk-forward replay, MIRRORS the live pipeline:
  * At candle i (>= warmup) the strategy engine sees candles[:i] ONLY
    (feed.py closes candle i-1, then predicts candle i from CLOSED candles).
  * The ML model is trained on rows built from candles[:i] with labels of
    already-CLOSED candles (candles j < i), retrained every RETRAIN_EVERY
    candles — no look-ahead by construction.
  * Signal source selection mirrors feed.py::_run_eoc:
      strategy engine CALL/PUT  → source 'strategy' (strict or any-theory)
      strategy engine NEUTRAL   → ML T+1 prediction → source 'ml'
      both silent               → honest no-signal (counted, reported)
  * Grading: candle i's open→close (1-minute expiry), identical to
    feed.py::_accuracy(). Draws excluded from WR.

HONESTY NOTE: the synthetic generator's "persistence" edge (mean-reversion)
is learnable by design — absolute WR is INFLATED vs real markets. The
"none" edge is a FAIR random walk where an honest pipeline MUST show ~50%.
This harness verifies MECHANICS + the source hierarchy contract, not
real-market edge (that is tracked live via /api/winrate).

Run:
    python scripts/backtest_any_theory.py
    python scripts/backtest_any_theory.py --candles 600 --edge none
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# ANY-THEORY mode is the new production default; make it explicit here.
os.environ["QX_SIGNAL_MODE"] = "any_theory"
os.environ["QX_TARGET_GATE"] = "0"
os.environ["QX_BREAKEVEN_GATE"] = "0"
os.environ["QX_PAIR_HEALTH_GATE"] = "0"
os.environ["QX_TRAP_HOUR"] = "0"

from engines import predict                      # noqa: E402
from scripts.synthetic_otc import gen_candles    # noqa: E402

WARMUP = 35           # first candle index that gets a signal
WINDOW = 300          # engine window cap (matches feed.py snapshot cap)
ML_WINDOW = 50        # ML feature window (matches predictor.PRED_WINDOW)
ML_MIN_TRAIN = 120    # min training rows before the ML voice may speak
RETRAIN_EVERY = 25    # ML retrain cadence (candles)


# ── ML walk-forward model (mirrors core.otc_predict fast_train) ────────────
def ml_features(window):
    """Extended feature row for the CLOSED-candle window (predictor.py path)."""
    from core.otc_predict.features_ext import build_extended_row
    return build_extended_row(window)


class WalkForwardML:
    """Strictly-causal sklearn model replicating the ML source voice."""

    def __init__(self):
        self._model = None
        self._rows = []      # (feat_dict, label)
        self._trained_at = -1

    def observe(self, window):
        """Called AFTER candle i closes: window[-1] is candle i — its own
        up/down label becomes a training row for predicting i+1."""
        try:
            feats = ml_features(window[-ML_WINDOW:])
            label = int(window[-1]["close"] > window[-1]["open"])
            self._rows.append((feats, label))
        except Exception:
            pass

    def _train(self):
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        if len(self._rows) < ML_MIN_TRAIN:
            return False
        X = [list(r[0].values()) for r in self._rows]
        y = [r[1] for r in self._rows]
        self._model = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=400, C=1.0))
        self._model.fit(X, y)
        return True

    def predict(self, window):
        """P(up) for the NEXT candle from the CLOSED window; None = abstain."""
        if self._model is None or len(self._rows) < ML_MIN_TRAIN:
            return None
        try:
            feats = ml_features(window[-ML_WINDOW:])
            X = [list(feats.values())]
            return float(self._model.predict_proba(X)[0][1])
        except Exception:
            return None


def grade(candle):
    """feed.py _accuracy() equivalent: close>open ⇒ UP. Returns 'draw' on
    zero-move candles (excluded from WR)."""
    o, c = candle["open"], candle["close"]
    if abs(c - o) < 1e-9:
        return "draw"
    return "UP" if c > o else "DOWN"


def run_pair(asset, candles, verbose=True):
    from core.otc_predict.features_ext import EXTENDED_FEATURE_NAMES  # noqa: F401
    ml = WalkForwardML()
    stats = defaultdict(int)
    src_stats = defaultdict(lambda: defaultdict(int))  # src → {correct,wrong,draw}
    per_pair = {"asset": asset, "total": 0, "covered": 0}
    fallback_violations = []
    t_start = time.time()

    # prime the ML trainer with the warmup history (rows for closed candles
    # before the first prediction point — strictly past data)
    for i in range(WARMUP):
        ml.observe(candles[: i + 1])

    for i in range(WARMUP, len(candles)):
        past = candles[max(0, i - WINDOW):i]      # CLOSED candles only
        target = candles[i]                        # the candle being predicted

        # ── 1. strategy engine (module engine) ─────────────────────────────
        try:
            res = predict(list(past), ticks=[], micro=None, asset=asset,
                          htf_trend="SIDEWAYS", period=60)
        except Exception as exc:
            stats["engine_errors"] += 1
            res = None

        signal, source, strategy = None, None, None
        if res is not None:
            # NO-FALLBACK assertion: the banned labels must never appear
            if (res.get("signal") in ("CALL", "PUT")
                    and (res.get("fallback")
                         or res.get("strategy") == "confluence_v1_fallback"
                         or res.get("signal_quality") == "FALLBACK")):
                fallback_violations.append(
                    {"i": i, "strategy": res.get("strategy"),
                     "basis": res.get("fallback_basis")})
            if res.get("signal") in ("CALL", "PUT"):
                signal = res["signal"]
                strategy = res.get("strategy")
                source = ("strategy_strict"
                          if strategy == "confluence_v1"
                          else "strategy_any")

        # ── 2. ML hand-off: module engine silent → ML model speaks ────────
        if signal is None:
            # retrain on a fixed cadence (rows observed so far are all past)
            if i - ml._trained_at >= RETRAIN_EVERY:
                if ml._train():
                    ml._trained_at = i
            p_up = ml.predict(past)
            if p_up is not None:
                signal = "CALL" if p_up >= 0.5 else "PUT"
                source = "ml_model"
                strategy = "ml_model_t1"

        # ── 3. grade ───────────────────────────────────────────────────────
        stats["candles"] += 1
        if signal is None:
            stats["no_signal"] += 1
        else:
            stats["covered"] += 1
            g = grade(target)
            if g == "draw":
                stats["draws"] += 1
                src_stats[source]["draw"] += 1
            else:
                correct = (signal == "CALL" and g == "UP") or \
                          (signal == "PUT" and g == "DOWN")
                stats["correct" if correct else "wrong"] += 1
                src_stats[source]["correct" if correct else "wrong"] += 1

        # ML observes the NOW-closed candle for future training
        ml.observe(candles[: i + 1])

    per_pair["total"] = stats["candles"]
    per_pair["covered"] = stats["covered"]
    per_pair["coverage_pct"] = round(100.0 * stats["covered"] /
                                     max(1, stats["candles"]), 1)
    graded = stats["correct"] + stats["wrong"]
    per_pair["win_rate"] = round(100.0 * stats["correct"] / graded, 1) \
        if graded else None
    per_pair["graded"] = graded
    per_pair["stats"] = dict(stats)
    per_pair["by_source"] = {
        s: {"correct": v["correct"], "wrong": v["wrong"],
            "draw": v["draw"],
            "win_rate": round(100.0 * v["correct"] /
                              max(1, v["correct"] + v["wrong"]), 1)}
        for s, v in src_stats.items()}
    per_pair["fallback_violations"] = fallback_violations
    per_pair["secs"] = round(time.time() - t_start, 1)
    if verbose:
        print(f"  {asset:<12} candles={stats['candles']:<5} "
              f"coverage={per_pair['coverage_pct']:<6} "
              f"WR={per_pair['win_rate']}%  "
              f"(strict={per_pair['by_source'].get('strategy_strict', {}).get('win_rate', '—')}% "
              f"any={per_pair['by_source'].get('strategy_any', {}).get('win_rate', '—')}% "
              f"ml={per_pair['by_source'].get('ml_model', {}).get('win_rate', '—')}%) "
              f"[{per_pair['secs']}s]")
    return per_pair


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=500)
    ap.add_argument("--edge", choices=["persistence", "none"],
                    default="persistence")
    ap.add_argument("--pairs", type=str,
                    default="USDZAR_otc,AUDJPY_otc,NZDJPY_otc,NZDCAD_otc,"
                            "USDNGN_otc,GBPNZD_otc,EURNZD_otc")
    ap.add_argument("--json", type=str,
                    default="scripts/backtest_any_theory_results.json")
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    print(f"ANY-THEORY + ML-HAND-OFF BACKTEST  (edge={args.edge}, "
          f"candles={args.candles}, pairs={len(pairs)})")
    print(f"QX_SIGNAL_MODE={os.environ['QX_SIGNAL_MODE']} — "
          f"requirements: 100% coverage, ZERO fallback signals, "
          f"any-one-theory → signal, module-silent → ML signal\n")

    results = []
    for k, asset in enumerate(pairs):
        candles = gen_candles(asset, args.candles, seed=101 + k,
                              edge=args.edge, start_price=1.1000 + 0.05 * k)
        results.append(run_pair(asset, candles))

    # ── aggregate verdict ──────────────────────────────────────────────────
    tot = sum(r["stats"]["candles"] for r in results)
    cov = sum(r["stats"]["covered"] for r in results)
    cor = sum(r["stats"]["correct"] for r in results)
    wr = sum(r["stats"]["wrong"] for r in results)
    agg_wr = round(100.0 * cor / max(1, cor + wr), 2)
    coverage = round(100.0 * cov / max(1, tot), 2)
    viol = sum(len(r["fallback_violations"]) for r in results)
    src_agg = defaultdict(lambda: [0, 0])
    for r in results:
        for s, v in r["by_source"].items():
            src_agg[s][0] += v["correct"]
            src_agg[s][1] += v["wrong"]

    print("\n──────── VERDICT ────────")
    print(f"candles evaluated : {tot}")
    print(f"signal coverage   : {coverage}%   "
          f"{'✅ PASS (every candle signaled)' if coverage >= 99.0 else '⚠ check'}")
    print(f"fallback signals  : {viol}   "
          f"{'✅ PASS (zero fallback — directive honored)' if viol == 0 else '❌ FAIL'}")
    print(f"aggregate WR      : {agg_wr}% (graded {cor + wr})")
    for s, (c, w) in sorted(src_agg.items()):
        print(f"  source {s:<15} WR {round(100.0 * c / max(1, c + w), 1)}% "
              f"({c}W/{w}L)")
    if args.edge == "none":
        honest = abs(agg_wr - 50.0) <= 4.0
        print(f"fair-walk honesty : {'✅ PASS' if honest else '❌ FAIL'} "
              f"(WR {agg_wr}% vs expected ~50%)")

    out = {"edge": args.edge, "candles": args.candles,
           "signal_mode": os.environ["QX_SIGNAL_MODE"],
           "aggregate": {"candles": tot, "coverage_pct": coverage,
                         "win_rate": agg_wr, "fallback_violations": viol,
                         "by_source": {s: {"correct": c, "wrong": w}
                                       for s, (c, w) in src_agg.items()}},
           "pairs": [{k2: v for k2, v in r.items() if k2 != "stats"}
                     for r in results]}
    with open(os.path.join(REPO, args.json), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"\nresults → {args.json}")
    return 0 if (viol == 0 and coverage >= 99.0) else 1


if __name__ == "__main__":
    sys.exit(main())
