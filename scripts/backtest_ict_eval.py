"""PHASE-L6 — ICT/SMC composite-arm evaluation on the PRE-COMMITTED holdout.

Protocol (frozen BEFORE this script ever ran — see
docs/smc/HOLDOUT_MANIFEST.json, committed at CP-L0):
  * data: the authorized 30-day Binance 1m dataset (8 assets);
  * the trailing PARTIAL candle of each file is dropped (fetch-time open
    candle — verified against the live API in Phase K);
  * holdout = the LAST 8,640 CLOSED candles (6 days) of each asset;
  * context warmup: features at holdout start use the full preceding
    series (>> the manifest's 600-candle warmup);
  * arms are PRE-SPECIFIED RULES (no fitting, no thresholds tuned on the
    holdout — every threshold below was written into this file before the
    first run);
  * grading: CALL correct iff target candle GREEN (close>open), PUT iff
    RED (close<open), FLAT excluded from accuracy (repo tie policy),
    directional emissions still count toward coverage;
  * acceptance rule (pre-committed): an arm is ACCEPTED iff
    Wilson-95%-lower(pooled holdout accuracy) > prev-candle baseline
    pooled accuracy AND coverage >= 0.5. Everything else is REJECTED and
    remains research-only.

Classification: HISTORICAL_EXCHANGE_DATA (public Binance 1m — authorized;
NOT OTC; NOT live-feed evidence; NOT a claim about Quotex OTC).

Run: python3 scripts/backtest_ict_eval.py
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.ict.features import build_feature_matrix  # noqa: E402
from core.ict.types import candle_from_row          # noqa: E402

DATA_DIR = os.path.join(_REPO, "scripts", "data", "binance_30d")
OUT_DIR = os.path.join(_REPO, "scripts", "out")
ASSETS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT",
          "SOLUSDT", "DOGEUSDT", "LTCUSDT"]
HOLDOUT_CANDLES = 8640          # pre-committed: last 6 days
MANIFEST = os.path.join(_REPO, "docs", "smc", "HOLDOUT_MANIFEST.json")


# ── PRE-SPECIFIED ARMS (frozen before the first run) ────────────────────
def arm_prev_candle(f, candles, t):
    c = candles[t - 1]
    if c.close > c.open:
        return "CALL"
    if c.close < c.open:
        return "PUT"
    return None


def arm_ict1_ob_retest(f, candles, t):
    """Zone re-test + recent-pattern agreement."""
    dem, sup = f.get("poi.nearest_demand"), f.get("poi.nearest_supply")
    bull = f.get("pattern.bull_count_5", 0) > f.get("pattern.bear_count_5", 0)
    bear = f.get("pattern.bear_count_5", 0) > f.get("pattern.bull_count_5", 0)
    if dem and dem["dist_atr"] >= -0.5 and bull:
        return "CALL"
    if sup and sup["dist_atr"] <= 0.5 and bear:
        return "PUT"
    return None


def arm_ict2_sweep_reversal(f, candles, t):
    """Liquidity sweep reversal (sell-side swept -> CALL and mirror)."""
    s, age = f.get("liq.last_sweep"), f.get("liq.sweep_age")
    if s == "sweep_sell_side" and age is not None and age <= 3:
        return "CALL"
    if s == "sweep_buy_side" and age is not None and age <= 3:
        return "PUT"
    return None


def arm_ict3_amd_continuation(f, candles, t):
    """AMD expansion continuation."""
    a, age = f.get("liq.last_amd"), f.get("liq.amd_age")
    if a == "amd_bullish" and age is not None and age <= 5:
        return "CALL"
    if a == "amd_bearish" and age is not None and age <= 5:
        return "PUT"
    return None


def arm_ict4_fvg_fill(f, candles, t):
    """Nearest FVG re-approach."""
    dem, sup = f.get("poi.nearest_demand"), f.get("poi.nearest_supply")
    if dem and dem["kind"] == "fvg_bullish" and dem["dist_atr"] >= -0.5:
        return "CALL"
    if sup and sup["kind"] == "fvg_bearish" and sup["dist_atr"] <= 0.5:
        return "PUT"
    return None


def arm_ict5_divergence(f, candles, t):
    """Momentum divergence."""
    d, age = f.get("mom.div_side"), f.get("mom.div_age")
    if d == "bullish" and age is not None and age <= 3:
        return "CALL"
    if d == "bearish" and age is not None and age <= 3:
        return "PUT"
    return None


def arm_ict6_kz_structure(f, candles, t):
    """Kill-zone + BOS continuation agreement."""
    if f.get("session.target_kill_zone") is None:
        return None
    br, age = f.get("structure.last_break"), f.get("structure.break_age")
    tr = f.get("structure.trend")
    if tr == "bullish" and br == "bos_bullish" and age is not None and age <= 5:
        return "CALL"
    if tr == "bearish" and br == "bos_bearish" and age is not None and age <= 5:
        return "PUT"
    return None


def arm_ict7_vote(f, candles, t):
    """Net vote across arms 1-6 (|net| >= 2 required)."""
    votes = 0
    for fn in (arm_ict1_ob_retest, arm_ict2_sweep_reversal,
               arm_ict3_amd_continuation, arm_ict4_fvg_fill,
               arm_ict5_divergence, arm_ict6_kz_structure):
        d = fn(f, candles, t)
        if d == "CALL":
            votes += 1
        elif d == "PUT":
            votes -= 1
    if votes >= 2:
        return "CALL"
    if votes <= -2:
        return "PUT"
    return None


ARMS = {
    "baseline_prev_candle": arm_prev_candle,
    "ict1_ob_retest": arm_ict1_ob_retest,
    "ict2_sweep_reversal": arm_ict2_sweep_reversal,
    "ict3_amd_continuation": arm_ict3_amd_continuation,
    "ict4_fvg_fill": arm_ict4_fvg_fill,
    "ict5_divergence": arm_ict5_divergence,
    "ict6_kz_structure": arm_ict6_kz_structure,
    "ict7_vote": arm_ict7_vote,
}

ARM_SPECS = {
    "baseline_prev_candle": "previous closed candle direction (reference)",
    "ict1_ob_retest": "nearest demand/supply zone within |dist_atr|<=0.5 AND "
                      "5-candle pattern count agreement",
    "ict2_sweep_reversal": "liquidity sweep (fractal levels) with age<=3, "
                           "reversal direction",
    "ict3_amd_continuation": "AMD expansion with age<=5, continuation "
                             "direction",
    "ict4_fvg_fill": "nearest FVG within |dist_atr|<=0.5, fill direction",
    "ict5_divergence": "RSI divergence with age<=3",
    "ict6_kz_structure": "kill-zone AND trend-aligned BOS with age<=5",
    "ict7_vote": "net >= 2 votes across ict1-ict6",
}


def wilson_ci(correct, n, z=1.96):
    if not n:
        return None
    p = correct / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return [(centre - adj) / denom, (centre + adj) / denom]


def process_asset(asset):
    """Compute per-arm counters for ONE asset over the pre-committed
    holdout. Pure function of (asset dataset, frozen arms)."""
    path = os.path.join(DATA_DIR, f"{asset}_1m.json")
    raw = json.load(open(path))
    closed = raw[:-1]                      # drop trailing PARTIAL candle
    candles = [candle_from_row(r) for r in closed]
    hold_lo = len(candles) - HOLDOUT_CANDLES
    t_lo = max(1, hold_lo)
    rows = build_feature_matrix(candles, start=t_lo, end=len(candles) - 1)
    info = {
        "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
        "rows_file": len(raw), "rows_closed": len(closed),
        "holdout_start_ts": dt.datetime.fromtimestamp(
            candles[t_lo].ts, dt.timezone.utc).isoformat(),
        "holdout_end_ts": dt.datetime.fromtimestamp(
            candles[-1].ts, dt.timezone.utc).isoformat(),
    }
    man = json.load(open(MANIFEST))
    mf = man["files"].get(f"{asset}_1m.json", {})
    info["manifest_match"] = mf.get("sha256") == info["sha256"]

    counters = {name: {"emitted": 0, "graded": 0, "correct": 0,
                       "n_total": 0, "flat_emitted": 0}
                for name in ARMS}
    for r in rows:
        t = r["t"]
        f = r["features"]
        target = candles[t]
        if target.close > target.open:
            outcome = "GREEN"
        elif target.close < target.open:
            outcome = "RED"
        else:
            outcome = "FLAT"
        for name, fn in ARMS.items():
            d = fn(f, candles, t)
            acc = counters[name]
            acc["n_total"] += 1
            if d is None:
                continue
            acc["emitted"] += 1
            if outcome == "FLAT":
                acc["flat_emitted"] += 1
                continue
            acc["graded"] += 1
            if (d == "CALL" and outcome == "GREEN") or \
               (d == "PUT" and outcome == "RED"):
                acc["correct"] += 1
    return info, counters, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--assets", default=None,
                    help="comma-separated subset to (re)compute; cached "
                         "assets are reused")
    ap.add_argument("--refresh", action="store_true",
                    help="recompute even when a cache file exists")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    cache_dir = "/tmp/l6_cache"
    os.makedirs(cache_dir, exist_ok=True)

    if args.assets:
        todo = [a.strip() for a in args.assets.split(",") if a.strip()]
    elif args.refresh:
        todo = list(ASSETS)
    else:
        todo = [a for a in ASSETS
                if not os.path.exists(os.path.join(cache_dir, f"{a}.json"))]
    for a in todo:
        info, counters, nrows = process_asset(a)
        json.dump({"asset": a, "info": info, "counters": counters,
                   "rows": nrows},
                  open(os.path.join(cache_dir, f"{a}.json"), "w"))
        print(f"[l6] {a}: {nrows} holdout rows computed+cached", flush=True)

    # aggregate over ALL assets (cached + freshly computed)
    man = json.load(open(MANIFEST))
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    dataset_info = {}
    per_arm = {name: {"emitted": 0, "graded": 0, "correct": 0,
                      "n_total": 0, "flat_emitted": 0, "per_asset": {}}
               for name in ARMS}
    for asset in ASSETS:
        cf = os.path.join(cache_dir, f"{asset}.json")
        if not os.path.exists(cf):
            print(f"[l6] WARNING: no cache for {asset} — run --assets {asset}")
            continue
        blob = json.load(open(cf))
        dataset_info[asset] = blob["info"]
        for name in ARMS:
            c = blob["counters"][name]
            acc = per_arm[name]
            for k in ("emitted", "graded", "correct", "n_total",
                      "flat_emitted"):
                acc[k] += c[k]
            acc["per_asset"][asset] = dict(c)

    results = {}
    for name, acc in per_arm.items():
        n, g, c = acc["n_total"], acc["graded"], acc["correct"]
        accuracy = (c / g) if g else None
        cov = acc["emitted"] / n if n else None
        ci = wilson_ci(c, g)
        results[name] = {
            "spec": ARM_SPECS[name],
            "n_holdout_rows": n,
            "emitted": acc["emitted"],
            "flat_outcome_emitted": acc["flat_emitted"],
            "graded": g,
            "correct": c,
            "accuracy": accuracy,
            "accuracy_ci95": ci,
            "coverage": cov,
            "per_asset": {
                a: {"accuracy": (p["correct"] / p["graded"]) if p["graded"] else None,
                    "graded": p["graded"],
                    "coverage": (p["emitted"] / p["n_total"]) if p["n_total"] else None}
                for a, p in acc["per_asset"].items()},
        }

    base_acc = results["baseline_prev_candle"]["accuracy"]
    verdicts = {}
    for name, r in results.items():
        if name == "baseline_prev_candle":
            continue
        wl = r["accuracy_ci95"][0] if r["accuracy_ci95"] else 0.0
        accepted = (wl > base_acc) and (r["coverage"] or 0.0) >= 0.5
        verdicts[name] = {
            "wilson_lower": wl,
            "baseline_accuracy": base_acc,
            "coverage": r["coverage"],
            "accepted": accepted,
            "decision": ("ACCEPTED (passes the pre-committed rule)" if accepted
                         else "REJECTED — research-only, weight stays 0"),
        }

    report = {
        "report_version": "phase_l_ict_eval_v1",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "protocol": {
            "holdout": "LAST 8640 CLOSED candles per asset (pre-committed "
                       "in docs/smc/HOLDOUT_MANIFEST.json at CP-L0, before "
                       "any ICT arm ran)",
            "trailing_partial_candle": "dropped (fetch-time open candle, "
                                       "verified vs live API in Phase K)",
            "grading": "CALL iff GREEN (close>open), PUT iff RED, FLAT "
                       "excluded from accuracy (tie policy), directional "
                       "emissions count toward coverage",
            "arms_fitted": "NONE — all arms are pre-specified rules; no "
                           "parameter was tuned on the holdout",
            "acceptance_rule": man["acceptance_rule"]["rule"],
        },
        "classification": ("HISTORICAL_EXCHANGE_DATA (public Binance 1m — "
                           "authorized; NOT OTC; NOT live-feed evidence; "
                           "NOT a claim about Quotex OTC)"),
        "datasets": dataset_info,
        "results": results,
        "verdicts": verdicts,
        "limitations": [
            "exchange crypto data, not OTC — transfer to Quotex OTC is "
            "unverified",
            "rule arms have fixed thresholds chosen a priori; no search "
            "was performed (and none is allowed on this holdout)",
            "no news-gate arm: no authorized economic calendar exists for "
            "this data (module unit-tested only)",
            "multiple arms share one holdout — the pre-committed rule is "
            "per-arm; family-wise error is not controlled (documented "
            "honestly; acceptance still requires CI-lower > baseline)",
        ],
    }
    out = os.path.join(args.out_dir, f"phase_l_ict_eval_{stamp}.json")
    with open(out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(f"[l6] report -> {out}")
    print(f"[l6] baseline_prev_candle accuracy: "
          f"{results['baseline_prev_candle']['accuracy']:.4f} "
          f"(n={results['baseline_prev_candle']['graded']})")
    for name, r in results.items():
        if name == "baseline_prev_candle":
            continue
        acc_s = f"{r['accuracy']:.4f}" if r["accuracy"] is not None else "n/a"
        print(f"[l6] {name}: acc={acc_s} cov={r['coverage']:.3f} "
              f"graded={r['graded']} -> {verdicts[name]['decision']}")


if __name__ == "__main__":
    main()
