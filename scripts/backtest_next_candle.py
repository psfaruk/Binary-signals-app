#!/usr/bin/env python3
"""BACKTEST — Next-Candle Engine (CSE v1, core/next_candle.py).

Backtests the SHIPPED engine itself (not a copy): feeds each dataset
candle-by-candle through core.next_candle.get_engine().ingest() exactly
like the live feed does (refit every 50 candles, global-prior blend),
grades every 0-second prediction at candle close, and writes
scripts/backtest_next_candle_report.json.

Datasets:
  real    — Binance 1m klines (scripts/data/binance/*.json)
            [BTC, ETH, XRP, BNB] — recent ~9000 candles each
  otc-sim — synthetic OTC-like feed families (seeded, deterministic):
            the honest robustness battery.  GBM = pure random walk (the
            null: a fair engine must score ~50% here — a fake-edge
            engine would show 60%+), OU = mean-reverting, REGIME =
            switching trend, BROKER = composite OTC-like.

No look-ahead: every prediction uses only candles that already closed
(engine design guarantees this; the harness adds its own verification).

Usage:
  python3 scripts/backtest_next_candle.py [--seeds 11 22 33]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from core.next_candle import get_engine  # noqa: E402

DATA_DIR = os.path.join(REPO, "scripts", "data", "binance")
REPORT = os.path.join(REPO, "scripts", "backtest_next_candle_report.json")
WARMUP = 2400  # candles before the first graded prediction (research parity)


# ────────────────────────── datasets ──────────────────────────
def load_real(name: str):
    path = os.path.join(DATA_DIR, f"{name}_1m.json")
    with open(path) as f:
        rows = json.load(f)
    # Binance klines are epoch-MILLIS; the engine speaks epoch-SECONDS
    # (repo/Quotex convention) → convert.
    return [{"time": int(r["t"]) // 1000, "open": float(r["o"]),
             "high": float(r["h"]), "low": float(r["l"]),
             "close": float(r["c"])} for r in rows]


def _ticks_to_candles(ticks, start_s=1_700_000_000, period_s=60):
    n = len(ticks) // period_s
    out = []
    for i in range(n):
        seg = ticks[i * period_s:(i + 1) * period_s]
        out.append({"time": start_s + i * period_s,
                    "open": float(seg[0]), "high": float(seg.max()),
                    "low": float(seg.min()), "close": float(seg[-1])})
    return out


def gen_gbm(n, seed, p0=1.1000):
    import numpy as np
    rs = np.random.RandomState(seed)
    steps = rs.normal(0.0, 0.00022, n * 60)
    return _ticks_to_candles(p0 * np.exp(np.cumsum(steps)))


def gen_ou(n, seed, kappa, p0=1.1000):
    import numpy as np
    rs = np.random.RandomState(seed)
    px = np.empty(n * 60)
    px[0] = p0
    for i in range(1, n * 60):
        px[i] = px[i - 1] + kappa * (p0 - px[i - 1]) + rs.normal(0, 0.00022)
    return _ticks_to_candles(px)


def gen_regime(n, seed, p0=1.1000):
    import numpy as np
    rs = np.random.RandomState(seed)
    px = np.empty(n * 60)
    px[0] = p0
    drift = 0.0
    for i in range(1, n * 60):
        if rs.rand() < 1.0 / 1800.0:
            drift = rs.choice([-1, 1]) * rs.uniform(5e-6, 3e-5)
        px[i] = px[i - 1] + drift + rs.normal(0, 0.00020)
    return _ticks_to_candles(px)


def gen_broker_otc(n, seed, p0=1.1000):
    import numpy as np
    rs = np.random.RandomState(seed)
    px = np.empty(n * 60)
    px[0] = p0
    anchor = p0
    drift = 0.0
    for i in range(1, n * 60):
        if rs.rand() < 1.0 / 2400.0:
            drift = rs.choice([-1, 1]) * rs.uniform(3e-6, 2.5e-5)
        if rs.rand() < 1.0 / 7200.0:
            anchor = p0 * rs.uniform(0.998, 1.002)
        step = drift + 0.0008 * (anchor - px[i - 1]) + rs.normal(0, 0.00021)
        if rs.rand() < 0.0004:
            step += rs.choice([-1, 1]) * rs.uniform(2, 6) * 0.00021
        px[i] = px[i - 1] + step
    return _ticks_to_candles(px)


SYN_FAMILIES = {
    "otc-sim:random-walk(GBM)": gen_gbm,
    "otc-sim:mean-revert(OU k=0.10)": lambda n, s: gen_ou(n, s, 0.10),
    "otc-sim:mean-revert(OU k=0.30)": lambda n, s: gen_ou(n, s, 0.30),
    "otc-sim:regime-trend": gen_regime,
    "otc-sim:broker-composite": gen_broker_otc,
}


# ────────────────────────── run + grade ──────────────────────────
def binom_p(correct, total):
    if total == 0:
        return 1.0
    z = (correct - total * 0.5) / math.sqrt(total * 0.25)
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))


def run_engine(name, candles, warmup=WARMUP):
    """Feed the SHIPPED engine exactly like the live feed does."""
    eng = get_engine(f"BT:{name}", 60)
    eng.__init__(f"BT:{name}", 60)  # fresh engine per dataset
    t0 = time.time()
    n_pred = n_ok = 0
    max_wrong = cur_wrong = 0
    conf_bands = {"55+": [0, 0], "58+": [0, 0]}
    for i, c in enumerate(candles):
        payload = eng.ingest(c, bulk=False)
        if payload is None or i + 1 >= len(candles) or i < warmup:
            continue
        nxt = candles[i + 1]
        o, cl = nxt["open"], nxt["close"]
        if cl == o:
            continue  # doji — binary trade loses/refunds; not a direction hit
        ok = (payload["direction"] == "CALL") == (cl > o)
        n_pred += 1
        n_ok += 1 if ok else 0
        cur_wrong = 0 if ok else cur_wrong + 1
        max_wrong = max(max_wrong, cur_wrong)
        # confidence bands (calibrated probability of the predicted side)
        p_side = payload["p_green"] if payload["direction"] == "CALL" \
            else 1.0 - payload["p_green"]
        if p_side >= 0.55:
            conf_bands["55+"][0] += 1
            conf_bands["55+"][1] += 1 if ok else 0
        if p_side >= 0.58:
            conf_bands["58+"][0] += 1
            conf_bands["58+"][1] += 1 if ok else 0
    acc = n_ok / max(1, n_pred)
    return {
        "n": n_pred,
        "accuracy": round(acc, 4),
        "p_value_vs_coinflip": round(binom_p(n_ok, n_pred), 6),
        "max_wrong_streak": max_wrong,
        "conf_bands": {
            k: {"n": v[0], "accuracy": round(v[1] / max(1, v[0]), 4)}
            for k, v in conf_bands.items() if v[0] >= 30},
        "n_train": eng.n_train,
        "sec": round(time.time() - t0, 1),
    }


def baselines(candles, warmup=WARMUP):
    """Sanity baselines on the same data (post-warmup)."""
    n = len(candles)
    cont_ok = rev_ok = tot = 0
    for i in range(warmup, n - 1):
        o, c = candles[i]["open"], candles[i]["close"]
        no, nc = candles[i + 1]["open"], candles[i + 1]["close"]
        if nc == no or c == o:
            continue
        tot += 1
        green = c > o
        nxt_green = nc > no
        if green == nxt_green:
            cont_ok += 1
        else:
            rev_ok += 1
    return {"n": tot,
            "continuation": round(cont_ok / max(1, tot), 4),
            "reversal": round(rev_ok / max(1, tot), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[11, 22, 33])
    args = ap.parse_args()

    report = {"engine": "core/next_candle.py (CSE v1)",
              "protocol": ("walk-forward — the shipped engine ingests every "
                           "candle in order (online refit every 50, global-"
                           "prior blend), predictions graded at close; "
                           f"first {WARMUP} candles are warmup"),
              "real": {}, "real_baselines": {}, "otc_sim": {}, "seeds": args.seeds}

    print("═" * 72)
    print("NEXT-CANDLE ENGINE (CSE v1) — shipped-artifact backtest")
    print("═" * 72)

    for sym in ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT"):
        try:
            candles = load_real(sym)
        except FileNotFoundError:
            print(f"[real] {sym}: dataset missing — skipped")
            continue
        r = run_engine(sym, candles)
        b = baselines(candles)
        report["real"][sym] = r
        report["real_baselines"][sym] = b
        print(f"[real] {sym:8s} acc={r['accuracy']:.4f} n={r['n']} "
              f"p={r['p_value_vs_coinflip']} maxwrong={r['max_wrong_streak']} "
              f"| cont={b['continuation']} rev={b['reversal']}")

    if report["real"]:
        pooled_n = sum(r["n"] for r in report["real"].values())
        pooled_ok = sum(round(r["accuracy"] * r["n"]) for r in report["real"].values())
        report["real_pooled"] = {
            "n": pooled_n,
            "accuracy": round(pooled_ok / max(1, pooled_n), 4),
            "p_value_vs_coinflip": round(binom_p(pooled_ok, pooled_n), 8)}
        print(f"[real] POOLED  acc={report['real_pooled']['accuracy']:.4f} "
              f"n={pooled_n} p={report['real_pooled']['p_value_vs_coinflip']}")

    for fam, fn in SYN_FAMILIES.items():
        accs = []
        detail = {}
        for seed in args.seeds:
            candles = fn(9000, seed)
            r = run_engine(f"{fam}#{seed}", candles)
            accs.append(r["accuracy"])
            detail[str(seed)] = r
        report["otc_sim"][fam] = {"mean_accuracy": round(sum(accs) / len(accs), 4),
                                  "detail": detail}
        print(f"[sim ] {fam:34s} mean_acc={report['otc_sim'][fam]['mean_accuracy']:.4f} "
              f"per-seed={[round(a, 4) for a in accs]}")

    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)
    print("─" * 72)
    print(f"report → {REPORT}")


if __name__ == "__main__":
    main()
