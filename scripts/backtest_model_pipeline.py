#!/usr/bin/env python3
"""scripts/backtest_model_pipeline.py — PROPER END-TO-END BACKTEST
(2026-09-14 — user: "মডেল গুলো তে ডেটা আসছে না ও ট্রেইন হচ্ছে না, need
proper backtest এন্ড সোলিশন").

Verifies the COMPLETE offline-bootstrap fix on an ISOLATED temp DB
(DB_PATH + QX_PREDICT_MODELS_DIR env — nothing touches signals.db):

  S1 SEED     empty DB → synth_seed seeds every OTC pair
              ✗→✓ checks: contiguity (minute grid), OHLC validity,
              zero dojis, microstructure columns, provenance in _meta

  S2 DATASET  build_dataset over the seeded candle_micro
              checks: row volume, gap-free runs, PERTURBATION LOCK
              (mutating future candles must not move ANY feature)

  S3 TRAIN    fast_train_one per pair → register into the temp registry
              checks: walk-forward gates, mean-of-5 shuffle probe < 0.53,
              SYNTH HONESTY CAP (never "verified", data_source tagged),
              honest accuracy band |acc−50| ≤ 4pp (no fabricated edge)

  S4 REPLAY   the LIVE predictor path over the last N candles per pair —
              predictor.on_candle_closed() EXACTLY as feed.py calls it
              (settle older targets → predict T+1/T+2 → freeze rows)
              checks: 100% payload coverage after warm-up, frozen rows,
              settled rows, honest win-rate band, telemetry consistency

  S5 REPORT   console verdict (Bengali) + JSON artefact in scripts/out/

Exit code 0 = every stage passed. Any failure prints the exact stage,
check and numbers so the regression is diagnosable in seconds.
"""

import json
import os
import shutil
import sys
import sqlite3
import time

# ── isolate BEFORE any repo import reads DB_PATH / model dirs ─────────────
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

RUN_DIR = os.path.join(HERE, "out", "backtest_model_pipeline")
shutil.rmtree(RUN_DIR, ignore_errors=True)
os.makedirs(RUN_DIR, exist_ok=True)
os.environ["DB_PATH"] = os.path.join(RUN_DIR, "backtest.db")
os.environ["QX_PREDICT_MODELS_DIR"] = os.path.join(RUN_DIR, "models")
os.environ.setdefault("QX_GUARD", "1")

from core.constants import ALLOWED_PAIRS_OTC            # noqa: E402
import db as _db                                        # noqa: E402
_db.init()            # create the temp DB schema (tables above)

DB = os.environ["DB_PATH"]

# replay sizing: last N candles per pair through the LIVE path
REPLAY_CANDLES = int(os.environ.get("BT_REPLAY_CANDLES", "240"))
TRAIN_PAIRS = sys.argv[1:] or None        # optional subset, default all

FAILURES = []
CHECKS = [0]


def check(stage, name, ok, detail=""):
    CHECKS[0] += 1
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {stage} · {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{stage} · {name} — {detail}")
    return ok


def section(title):
    print(f"\n══ {title} " + "═" * max(0, 66 - len(title)))


# ═════════════════════════════ S1 — SEED ═════════════════════════════════

section("S1 · SYNTHETIC SEED (empty DB → সব পেয়ারে ডেটা)")
t0 = time.time()
from core.otc_predict import synth_seed as ss            # noqa: E402

pairs = sorted(ALLOWED_PAIRS_OTC)
if TRAIN_PAIRS:
    pairs = [p for p in pairs if p in set(TRAIN_PAIRS)]

seeded = ss.seed_synthetic_history(pairs, log=lambda m: None)
seed_secs = round(time.time() - t0, 1)

conn = sqlite3.connect(DB)
counts = {a: n for a, n in conn.execute(
    "SELECT asset, COUNT(*) FROM candle_micro WHERE period=60 "
    "GROUP BY asset")}
conn.close()

check("S1", "সব পেয়ার seed হয়েছে", len(seeded) == len(pairs),
      f"{len(seeded)}/{len(pairs)} pairs, {seed_secs}s")
for a in pairs:
    n = counts.get(a, 0)
    check("S1", f"{a} ক্যান্ডেল সংখ্যা", n >= 2800, f"{n} rows (2d expected)")

# deep validation on every pair: minute grid, OHLC, no doji, micro
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
bad_grid = bad_ohlc = doji = no_micro = 0
for a in pairs:
    rows = conn.execute(
        "SELECT ctime, open, high, low, close, buy_pct, sell_pct, "
        "tick_count, is_fight FROM candle_micro "
        "WHERE asset=? AND period=60 ORDER BY ctime", (a,)).fetchall()
    prev_t = None
    for r in rows:
        if prev_t is not None and r["ctime"] - prev_t != 60:
            bad_grid += 1
        prev_t = r["ctime"]
        if not (r["high"] >= max(r["open"], r["close"]) and
                r["low"] <= min(r["open"], r["close"]) and r["low"] > 0):
            bad_ohlc += 1
        if r["close"] == r["open"]:
            doji += 1
        if r["tick_count"] is None or r["buy_pct"] is None:
            no_micro += 1
conn.close()
check("S1", "মিনিট-গ্রিড contiguous (গ্যাপ নেই)", bad_grid == 0,
      f"violations={bad_grid}")
check("S1", "OHLC বৈধ (high≥body≥low>0)", bad_ohlc == 0, f"violations={bad_ohlc}")
check("S1", "দোজি নেই (target সবসময় directional)", doji == 0, f"dojis={doji}")
check("S1", "microstructure কলাম ভরা", no_micro == 0, f"missing={no_micro}")
_ranges = ss.synth_ranges()
check("S1", "provenance _meta এ রেকর্ডেড", set(_ranges) == set(pairs),
      f"{len(_ranges)} ranges")

# ═════════════════════════ S2 — DATASET + LOCK ═══════════════════════════

section("S2 · DATASET + LEAKAGE LOCK")
from core.otc_dataset import (build_dataset, load_candles_from_db,  # noqa: E402
                              verify_lock)
from core.otc_predict.features_ext import build_unified_row  # noqa: E402
from core.otc_predict.hist_stats import enrich_rows          # noqa: E402

candles_by_asset = load_candles_from_db(DB)
t0 = time.time()
rows, dstats = build_dataset(candles_by_asset, window=50, micro=True,
                             feature_fn=build_unified_row)
rows = enrich_rows(rows)
ds_secs = round(time.time() - t0, 1)

expected_rows = sum(max(0, len(cs) - 52) for cs in candles_by_asset.values())
check("S2", "ডেটাসেট row volume", dstats["rows"] >= int(expected_rows * 0.97),
      f"{dstats['rows']} rows (expected≈{expected_rows}), {ds_secs}s")
check("S2", "gap-blind row বাদ", dstats["rows_dropped_by_gaps"] == 0,
      "synthetic grid এ গ্যাপ নেই")
check("S2", "দোজি-drop নেই", dstats["dropped_doji_t1"] == 0 and
      dstats["dropped_doji_t2"] == 0,
      f"t1={dstats['dropped_doji_t1']} t2={dstats['dropped_doji_t2']}")

# perturbation lock on one pair (features see ONLY the past)
probe_asset = pairs[0]
n_chk, fut_hits, self_hits = verify_lock(
    candles_by_asset[probe_asset], n_checks=25, window=50)
check("S2", "PERTURBATION LOCK: ভবিষ্যৎ mutate → feature অপরিবর্তিত",
      fut_hits == 0, f"future_hits={fut_hits}/{n_chk}")
check("S2", "lock non-vacuous (নিজ candle mutate → feature বদলায়)",
      self_hits > 0, f"self_hits={self_hits}/{n_chk}")

# ═══════════════════════════ S3 — TRAIN + GATES ══════════════════════════

section("S3 · WALK-FORWARD TRAIN (প্রতি পেয়ারে গেট-সহ)")
from core.otc_predict import fast_train as ft          # noqa: E402
from core.otc_predict.tracker import register_model    # noqa: E402
from core.otc_predict.models import save_bundle        # noqa: E402

by_asset = {}
for r in rows:
    by_asset.setdefault(r["asset"], []).append(r)

trained = {}
t0 = time.time()
for asset in pairs:
    arows = by_asset.get(asset) or []
    if len(arows) < ft.FAST_MIN_PAIR_ROWS:
        check("S3", f"{asset} train", False, f"rows {len(arows)} কম")
        continue
    report, bundle, status = ft.fast_train_one(arows, data_source="synthetic")
    gate = report.get("gate", {})
    trained[asset] = (report, bundle, status)
    t1g, t2g = gate.get("y1_up", {}), gate.get("y2_up", {})
    check("S3", f"{asset} মডেল registered", bundle is not None,
          f"status={status}")
    if bundle is None:
        continue
    check("S3", f"{asset} honesty cap (synthetic→provisional)",
          status in ("provisional",),
          f"status={status} cap={report.get('synthetic_cap_applied')}")
    for h, g in (("t1", t1g), ("t2", t2g)):
        check("S3", f"{asset} {h} shuffle probe (leakage)",
              g.get("shuffle_acc", 1.0) < ft.FAST_SHUFFLE_MAX,
              f"shuffle={g.get('shuffle_acc')} < {ft.FAST_SHUFFLE_MAX}")
        acc = g.get("acc_pct") or 0.0
        check("S3", f"{asset} {h} honest accuracy band",
              abs(acc - 50.0) <= 4.0,
              f"acc={acc}% (|acc−50| ≤ 4pp — ভাঁড়ামি এজ নেই)")
    # register into the temp registry so the LIVE path can pick it up
    path = save_bundle(bundle)
    register_model(asset, bundle.version, "pair", asset,
                   {"status": status, "rows": len(arows),
                    "data_source": "synthetic",
                    "walk_forward": report.get("gate", {})},
                   path, activate=True)
train_secs = round(time.time() - t0, 1)
reg_n = len(trained)
check("S3", "সব পেয়ারে বান্ডেল", reg_n == len(pairs),
      f"{reg_n}/{len(pairs)}, {train_secs}s total")

# ═══════════════════════ S4 — LIVE PATH REPLAY ═══════════════════════════

section("S4 · LIVE PREDICTOR REPLAY (settle→predict→freeze)")
from core.otc_predict import predictor as pred        # noqa: E402

replay_stats = {}
t0 = time.time()
for asset in pairs:
    cs = candles_by_asset.get(asset) or []
    start_i = max(50, len(cs) - REPLAY_CANDLES)
    payload_ok = 0
    for i in range(start_i, len(cs)):
        closed = cs[i]
        window = cs[max(0, i - 500):i + 1]
        micro = {k: closed.get(k) for k in
                 ("buy_pct", "sell_pct", "tick_count", "is_fight")}
        payload = pred.on_candle_closed(asset, 60, window, closed, micro)
        if payload and payload.get("status") == "ok":
            payload_ok += 1
    replay_stats[asset] = {"closes": len(cs) - start_i, "payloads": payload_ok}
replay_secs = round(time.time() - t0, 1)

total_closes = sum(s["closes"] for s in replay_stats.values())
total_payloads = sum(s["payloads"] for s in replay_stats.values())
check("S4", "প্রতি ক্যান্ডেল-ক্লোজে প্রেডিকশন payload (coverage)",
      total_payloads == total_closes,
      f"{total_payloads}/{total_closes} ({replay_secs}s)")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
frozen = conn.execute(
    "SELECT COUNT(*) FROM otc_predictions").fetchone()[0]
settled = conn.execute(
    "SELECT COUNT(*) FROM otc_predictions "
    "WHERE win_loss IN ('win','loss')").fetchone()[0]
wins = conn.execute(
    "SELECT COUNT(*) FROM otc_predictions WHERE win_loss='win'").fetchone()[0]
emitted = conn.execute(
    "SELECT COUNT(*) FROM otc_predictions WHERE emit=1").fetchone()[0]
# fallback-source audit: every frozen row must carry a REAL model version
no_model_rows = conn.execute(
    "SELECT COUNT(*) FROM otc_predictions "
    "WHERE model_version IS NULL OR model_version=''").fetchone()[0]
conn.close()

check("S4", "প্রেডিকশন freeze হয়েছে (otc_predictions)", frozen >= total_closes,
      f"{frozen} rows")
check("S4", "settlement হয়েছে (target ক্যান্ডেল এসে গ্রেড)", settled > 0,
      f"{settled} settled / {frozen} frozen")
check("S4", "fallback নেই — সব রো-এ আসল মডেল ভার্সন",
      no_model_rows == 0, f"rows_without_model={no_model_rows}")

if settled:
    wr = 100.0 * wins / settled
    # honest band: 95% CI for p=0.5 at n=settled → ±1.96·σ
    sigma = (0.25 / settled) ** 0.5
    band = 1.96 * sigma * 100
    check("S4", "win-rate সৎ ব্যান্ড (~50%)",
          abs(wr - 50.0) <= max(band, 1.0),
          f"WR={wr:.2f}% (n={settled}, band ±{band:.2f}pp)")
    print(f"        emit-rate: {100.0 * emitted / max(1, frozen):.1f}% "
          f"({emitted}/{frozen} frozen rows endorsed for trading)")

rt = pred.runtime_status()
check("S4", "predictor telemetry consistent",
      rt["closes_seen"] == total_closes and rt["predicted"] >= total_payloads,
      f"closes={rt['closes_seen']} predicted={rt['predicted']} "
      f"frozen={rt['frozen']} errors={rt['errors']}")

# ═════════════════════════════ S5 — REPORT ═══════════════════════════════

section("S5 · VERDICT")
report = {
    "generated_at": time.time(),
    "db": DB,
    "pairs": pairs,
    "seed_secs": seed_secs, "dataset_secs": ds_secs,
    "train_secs": train_secs, "replay_secs": replay_secs,
    "dataset_rows": dstats["rows"],
    "models_registered": reg_n,
    "replay": {"closes": total_closes, "payloads": total_payloads,
               "frozen": frozen, "settled": settled, "wins": wins,
               "emitted": emitted},
    "per_pair_replay": replay_stats,
    "checks_total": CHECKS[0],
    "failures": FAILURES,
}
with open(os.path.join(RUN_DIR, "report.json"), "w") as f:
    json.dump(report, f, indent=2, default=str)

if FAILURES:
    print(f"✗ FAILED — {len(FAILURES)}/{CHECKS[0]} চেক ফেল:")
    for x in FAILURES:
        print(f"    • {x}")
    sys.exit(1)

print(f"✓ PASSED — {CHECKS[0]}/{CHECKS[0]} চেক সবুজ।")
print(f"  ডেটা এসেছে: {len(pairs)} পেয়ার × ২ দিন | ট্রেইন হয়েছে: {reg_n} মডেল "
      f"({train_secs}s) | লাইভ-পাথ রিপ্লে: {total_closes} ক্যান্ডেল।")
print(f"  রিপোর্ট: {os.path.join(RUN_DIR, 'report.json')}")
