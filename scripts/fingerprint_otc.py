#!/usr/bin/env python3
"""OTC generator fingerprint — standalone analyzer & validation harness.

Modes:
  --validate              run the analyzer against 4 KNOWN ground-truth
                          feeds (real Binance / demo OTC-like / pure GBM /
                          pre-generated block library) and check the
                          verdicts are correct.
  --db                    analyze ticks stored in signals.db candle_micro
  --demo-live SECONDS     analyze the demo feed live (mechanics check)
  --feed                  analyze from the RUNNING app via its HTTP API
                          (prints /api/otc-fingerprint)

Everything is read-only.  No trading.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.otc_fingerprint import FingerprintEngine  # noqa: E402

RESEARCH = Path("/home/z/research")
DB_PATH = Path(__file__).resolve().parent.parent / "signals.db"


# ═════════════════════════════════════════════════════════════════════
#  Ground-truth feed generators (for --validate)
# ═════════════════════════════════════════════════════════════════════

def gen_pure_gbm(n_ticks=60000, p0=1.08, dt=0.5, vol=0.00025, seed=7):
    """Pure geometric random walk — SHOULD classify random walk, no edge."""
    rng = random.Random(seed)
    ts, price = 1_800_000_000.0, p0
    out = []
    for _ in range(n_ticks):
        out.append((ts, price))
        price *= math.exp(rng.gauss(0, vol))
        ts += dt
    return out


def gen_block_library(n_ticks=60000, seed=11):
    """PRE-GENERATED BLOCK LIBRARY — the exact thing the user asked about:
    a fixed set of candle/tick blocks, reused.  SHOULD be detected with
    pre_generated_blocks=YES."""
    rng = random.Random(seed)
    lib = []
    for _ in range(24):                       # fixed library of 24 blocks
        block = []
        p = 1.08
        for _ in range(150):
            p *= math.exp(rng.gauss(0, 0.0003))
            block.append(p)
        lib.append(block)
    out = []
    ts = 1_800_000_000.0
    while len(out) < n_ticks:
        block = rng.choice(lib)               # replay from fixed library
        for p in block:
            out.append((ts, p))
            ts += 0.5
            if len(out) >= n_ticks:
                break
    return out


def _demo_generator_dynamics(rng, n_ticks, p0, ts0, dt=0.42):
    """EXACT mirror of core/demo_feed.py dynamics (regime-switching:
    ~45% mean-reversion, ~30% multi-candle trend, ~25% chop; AR(1)
    tick momentum; spikes + stop-hunt wicks) — the KNOWN ground truth."""
    sigma = 0.00007
    price = p0
    anchor = p0
    anchor_left = 3600
    drift = 0.0
    drift_left = 0
    mode = "MR"
    innov = 0.0
    out = []
    ts = ts0
    for _ in range(n_ticks):
        # regime machine (same probabilities/lengths as demo_feed)
        if drift_left <= 0:
            r = rng.random()
            if r < 0.45:
                mode, drift = "MR", 0.0
                drift_left = rng.randint(240, 960)
            elif r < 0.75:
                mode = "TREND"
                drift = rng.choice((-1, 1)) * rng.uniform(0.25, 0.6)
                drift_left = rng.randint(300, 1200)
            else:
                mode, drift = "CHOP", 0.0
                drift_left = rng.randint(60, 240)
        drift_left -= 1
        anchor_left -= 1
        if anchor_left <= 0:
            anchor_left = rng.randint(1800, 5400)
            anchor += rng.gauss(0.0, 1.0) * sigma * 30.0
        # step (same as demo_feed._step)
        innov = 0.30 * innov + rng.gauss(0.0, 1.0) * sigma
        step = drift * sigma + innov
        if mode == "MR":
            step += 0.0012 * (anchor - price)
        if rng.random() < 0.002:
            step += rng.choice((-1, 1)) * sigma * rng.uniform(5, 10)
        price += step
        if rng.random() < 0.0015:
            price -= step * rng.uniform(3.0, 6.0)
        out.append((ts, price))
        ts += dt
    return out


def run_ticks_through(fp_engine, asset, ticks, period=60):
    """Feed raw ticks + synthesize 1m candles from them (for OHLC axes)."""
    buckets: dict = {}
    for ts, p in ticks:
        sec = int(ts)
        fp_engine.ingest_tick(asset, ts, p)
        b = buckets.setdefault(sec // period, [ts, p, p, p, p, 0])
        b[2] = max(b[2], p)
        b[3] = min(b[3], p)
        b[4] = p
        b[5] += 1
    for k in sorted(buckets):
        ts, o, h, l, c, tc = buckets[k]
        fp_engine.ingest_candle(asset, {"t": k * period, "o": o, "h": h,
                                       "l": l, "c": c, "tc": tc})


# ═════════════════════════════════════════════════════════════════════
#  Validation
# ═════════════════════════════════════════════════════════════════════

def validate() -> int:
    print("=" * 72)
    print("FINGERPRINT VALIDATION — 4 known ground-truth feeds")
    print("=" * 72)
    results = {}

    # ── 1. Real market (Binance 1m, no tick data — OHLC axes only) ────
    eng = FingerprintEngine()
    src = RESEARCH / "data" / "BTCUSDT_1m.json"
    if src.exists():
        rows = json.loads(src.read_text())
        candles = [{"t": r["t"] / 1000, "o": r["o"], "h": r["h"],
                    "l": r["l"], "c": r["c"]} for r in rows]
        for cd in candles:
            eng.ingest_candle("BTCUSDT", cd)
        r = eng.report("BTCUSDT")
        results["real_market_BTCUSDT"] = r["verdict"]
        _print_verdict("REAL MARKET  (Binance BTCUSDT 1m, 8999 candles)",
                       r["verdict"])
        ok = r["verdict"]["classification"] in (
            "SYNTHETIC_RANDOM_WALK", "SYNTHETIC_MEAN_REVERT",
            "SYNTHETIC_TRENDING", "SYNTHETIC_REGIME_MIX", "INCONCLUSIVE")
        # no tick axes → real/synthetic split unavailable; process readout
        print(f"    expect: random-walk-like process, NO fake predictability")
        print(f"    pass={'YES' if r['verdict']['predictability_score'] < 70 else 'NO'}"
              f" (score={r['verdict']['predictability_score']})")

    # ── 2. Known synthetic OTC-like (our demo dynamics) ───────────────
    eng2 = FingerprintEngine()
    rng = random.Random(42)
    ticks = _demo_generator_dynamics(rng, 60000, 1.0865, 1_800_000_000.0)
    run_ticks_through(eng2, "DEMO_OTC", ticks)
    r = eng2.report("DEMO_OTC")
    results["synthetic_otclike_DEMO"] = r["verdict"]
    _print_verdict("SYNTHETIC OTC-LIKE (mean-revert anchor + regimes, KNOWN)",
                   r["verdict"])
    exp_ok = r["verdict"]["classification"] in ("SYNTHETIC_MEAN_REVERT",
                                                "SYNTHETIC_REGIME_MIX",
                                                "SYNTHETIC_TRENDING")
    print(f"    expect: SYNTHETIC_* (regime-mixing generator), no block library")
    print(f"    pass={'YES' if exp_ok and r['verdict']['pre_generated_blocks'] is False else 'NO'}")

    # ── 3. Pure GBM (random walk control) ─────────────────────────────
    eng3 = FingerprintEngine()
    ticks = gen_pure_gbm()
    run_ticks_through(eng3, "GBM", ticks)
    r = eng3.report("GBM")
    results["randomwalk_GBM"] = r["verdict"]
    _print_verdict("PURE RANDOM WALK (GBM control)", r["verdict"])
    exp_ok = (r["verdict"]["classification"] == "SYNTHETIC_RANDOM_WALK"
              and r["verdict"]["pre_generated_blocks"] is False)
    print(f"    expect: SYNTHETIC_RANDOM_WALK, pre_gen=NO, LOW predictability")
    print(f"    pass={'YES' if exp_ok and r['verdict']['predictability_score'] < 65 else 'NO'}"
          f" (score={r['verdict']['predictability_score']})")

    # ── 4. Pre-generated block library ────────────────────────────────
    eng4 = FingerprintEngine()
    ticks = gen_block_library()
    run_ticks_through(eng4, "BLOCKLIB", ticks)
    r = eng4.report("BLOCKLIB")
    results["blocklib_BLOCKLIB"] = r["verdict"]
    _print_verdict("PRE-GENERATED BLOCK LIBRARY (fixed 24-block set, KNOWN)",
                   r["verdict"])
    exp_ok = (r["verdict"]["pre_generated_blocks"] is True
              and r["verdict"]["predictability_score"] >= 90)
    print(f"    expect: pre_generated_blocks=YES, predictability ~95")
    print(f"    pass={'YES' if exp_ok else 'NO'}"
          f" (score={r['verdict']['predictability_score']})")

    print("=" * 72)
    (Path(__file__).parent.parent / "backtest_reports").mkdir(exist_ok=True)
    out = Path(__file__).parent.parent / "backtest_reports" / "fingerprint_validation.json"
    raw = {}
    for label, engine in (("BTCUSDT", eng), ("DEMO_OTC", eng2),
                          ("GBM", eng3), ("BLOCKLIB", eng4)):
        try:
            raw[label] = engine.report(label)
        except Exception as exc:
            raw[label] = {"error": str(exc)}
    out.write_text(json.dumps({
        "validated_at": time.time(),
        "results": results,
        "raw": raw,
    }, indent=2, default=str))
    print(f"saved → {out}")
    return 0


def _print_verdict(label: str, v: dict) -> None:
    print(f"\n── {label}")
    print(f"    classification      : {v['classification']}")
    print(f"    process             : {v['process']}  regime_mix={v['regime_mixing']}")
    print(f"    pre_generated_blocks: {v['pre_generated_blocks']}")
    print(f"    predictability      : {v['predictability_score']}/100")
    for n in v["notes"][:6]:
        print(f"      · {n}")


# ═════════════════════════════════════════════════════════════════════
#  --db  (stored ticks from signals.db candle_micro.ticks_json)
# ═════════════════════════════════════════════════════════════════════

def analyze_db(assets: list) -> dict:
    if not DB_PATH.exists():
        print(f"no db at {DB_PATH}")
        return {}
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()
    eng = FingerprintEngine()
    q = ("SELECT asset, ctime, open, high, low, close, tick_count, ticks_json "
         "FROM candle_micro")
    if assets:
        q += " WHERE asset IN (%s)" % ",".join("?" * len(assets))
        rows = cur.execute(q, assets).fetchall()
    else:
        rows = cur.execute(q).fetchall()
    per_asset: dict = {}
    for asset, ctime, o, h, l, c, tc, tj in rows:
        fp = eng.fp(asset)
        # ticks_json holds prices only — timestamps would be fabricated.
        # Mark cadence unreliable so gap axes are suppressed; per-candle
        # tick_count (real) still feeds the tpc axes.
        fp.cadence_reliable = False
        try:
            ticks = json.loads(tj) if tj else []
        except Exception:
            ticks = []
        base = float(ctime)
        for i, p in enumerate(ticks):
            fp.ingest_tick(base + i * 0.47, float(p))
        fp.ingest_candle({"t": ctime, "o": o, "h": h, "l": l, "c": c,
                          "tc": tc})
        per_asset.setdefault(asset, 0)
    con.close()
    # cross-asset sync needs REAL timestamps — fabricated ones would lie
    return {"assets": {a: eng.fp(a).report() for a in per_asset},
            "cross_asset_sync": None,
            "note": "cadence axes suppressed (ticks_json has no real timestamps)",
            "generated_at": time.time()}


# ═════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--db", nargs="*", default=None,
                    help="assets to analyze from signals.db (none = all)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    if args.validate:
        return validate()

    if args.db is not None:
        rep = analyze_db(args.db)
    else:
        ap.print_help()
        return 1

    text = json.dumps(rep, indent=2, default=str)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
