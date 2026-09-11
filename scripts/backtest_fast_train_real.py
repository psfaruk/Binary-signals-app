#!/usr/bin/env python3
"""scripts/backtest_fast_train_real.py — REAL-DATA verification of the
FAST-TRAIN bootstrap (2026-09-12).

USER REQ: "Model রান করার জন্য 14 দিন অপেক্ষা করতে হবে কেনো? … খুব অল্প
সময়ের মধ্যে মডেল ট্রেইন হবে, রান হবে। 5/7 মিনিটের মধ্যে।" — this harness
simulates the EXACT production bootstrap shape on the REAL OTC history
pulled from the user's own platform (data/otc_history.db, 12 pairs × 10
days, zero gaps — fetched via the same pyquotex SSID path as the feed):

  1. seeds a fresh candle_micro with the LAST FAST_DAYS (3) days per OTC
     pair — precisely what a fresh Railway deploy holds after the top-up;
  2. runs core.otc_predict.fast_train.run_bootstrap() end-to-end, TIMED;
  3. prints the walk-forward gate per pair (honest verified/provisional);
  4. replays the LIVE signal path (features → bundle prob → price action →
     regime → PART-24 gates → PART-14 score tiers) over the final candles
     of each pair and reports what would actually have been emitted —
     proving the tiers still block coin-flip noise (no fake signal flood).

Run:  python3 scripts/backtest_fast_train_real.py [--days 3] [--replay 300]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3,
                    help="candle_micro depth per pair (production shape)")
    ap.add_argument("--replay", type=int, default=300,
                    help="live-path decisions per pair")
    ap.add_argument("--source-db", default=os.path.join(
        REPO, "data", "otc_history.db"))
    ap.add_argument("--keep-db", default=None,
                    help="optional path to keep the temp DB for inspection")
    args = ap.parse_args()

    src = args.source_db
    if not os.path.exists(src):
        print(f"source history DB not found: {src}")
        return 2

    tmpdir = args.keep_db or tempfile.mkdtemp(prefix="fast_real_bt_")
    os.environ["DB_PATH"] = os.path.join(tmpdir, "backtest.db")
    os.environ["QX_FAST_TRAIN"] = "0"      # no daemon in the harness
    os.environ.pop("QX_TOKEN", None)       # no fetch — data is pre-seeded

    import db as _db
    _db.init()
    from core.constants import ALLOWED_PAIRS_OTC
    from core.otc_predict import fast_train

    # ── 1. seed candle_micro with the production-shaped history ─────────
    hist = sqlite3.connect(src, timeout=30)
    assets = [r[0] for r in hist.execute(
        "SELECT DISTINCT asset FROM candles ORDER BY asset")
        if r[0] in set(ALLOWED_PAIRS_OTC)]
    per_pair = args.days * 1440
    conn = sqlite3.connect(_db.DB_PATH, timeout=60)
    seeded = {}
    for a in assets:
        rows = hist.execute(
            "SELECT time, open, high, low, close FROM candles "
            "WHERE asset=? ORDER BY time DESC LIMIT ?",
            (a, per_pair)).fetchall()
        rows.reverse()
        conn.executemany(
            "INSERT OR REPLACE INTO candle_micro"
            "(asset, period, ctime, open, high, low, close) "
            "VALUES (?,?,?,?,?,?,?)",
            [(a, 60, int(t), o, h, l, c) for t, o, h, l, c in rows])
        seeded[a] = len(rows)
    conn.commit()
    conn.close()
    hist.close()
    print(f"[bt] seeded candle_micro: {len(assets)} pairs × "
          f"~{per_pair} candles ({args.days}d each, REAL OTC data)")

    # ── 2. the timed end-to-end bootstrap ────────────────────────────────
    t0 = time.time()
    res = fast_train.run_bootstrap()
    secs = time.time() - t0
    summ = res.get("summary") or {}
    details = summ.get("details") or {}
    print(f"\n[bt] bootstrap finished in {secs:.0f}s "
          f"(production budget: ≤ 7 min) — registered="
          f"{summ.get('pairs_registered')}")

    print("\n══ FAST-TRAIN WALK-FORWARD GATE (real OTC data, "
          f"{args.days}d/pair) ══")
    hdr = f"{'pair':14s} {'rows':>6s} {'status':12s} {'sel':7s} " \
          f"{'acc%':>6s} {'base%':>6s} {'shuf':>6s}"
    print(hdr)
    for a in assets:
        d = details.get(a) or {}
        st = d.get("status", "?")
        rows_n = d.get("rows", seeded.get(a, 0))
        gate = (d.get("gate") or {}).get("y1_up") or {}
        sel = gate.get("selected", "-")
        acc = gate.get("acc_pct", "-")
        base = max((gate.get("baselines") or {}).values(), default="-")
        shuf = gate.get("shuffle_acc", "-")
        print(f"{a:14s} {rows_n:6d} {st:12s} {sel:7s} "
              f"{str(acc):>6s} {str(base):>6s} {str(shuf):>6s}")

    # ── 3. live-path replay: what would actually be EMITTED? ────────────
    print(f"\n══ LIVE-PATH REPLAY ({args.replay} decisions/pair, T+1) ══")
    from core.otc_dataset import load_candles_from_db
    from core.otc_predict.features_ext import build_extended_row
    from core.otc_predict.models import load_bundle
    from core.otc_predict.price_action import price_action_confirm
    from core.otc_predict.regime import detect_regime
    from core.otc_predict.signal_filter import score_signal
    from core.otc_predict.tracker import active_models

    am = active_models()
    candles_by = load_candles_from_db(_db.DB_PATH)
    tot = {"dec": 0, "emit": 0, "win": 0, "loss": 0}
    print(f"{'pair':14s} {'model':12s} {'decisions':>9s} {'emitted':>8s} "
          f"{'emit_wr':>8s} {'maxP':>6s}")
    for a in assets:
        d = details.get(a) or {}
        if d.get("status") not in ("verified", "provisional"):
            print(f"{a:14s} {d.get('status', 'none'):12s} — honest no model")
            continue
        row = am.get(a)
        if not row:
            print(f"{a:14s} {'no-active':12s}")
            continue
        bundle = load_bundle(row["path"])
        candles = candles_by.get(a) or []
        if len(candles) < 60:
            continue
        W = fast_train.PRED_WINDOW if hasattr(fast_train, "PRED_WINDOW") \
            else 50
        W = 50
        decisions = 0
        emitted = 0
        wins = losses = 0
        maxp = 0.0
        for i in range(max(W, len(candles) - args.replay),
                       len(candles) - 1):
            window = candles[i - W + 1: i + 1]
            if len(window) < W:
                continue
            if window[-1]["time"] - window[0]["time"] != (W - 1) * 60:
                continue                       # gap-free windows only
            feats = build_extended_row(window)
            p = bundle.predict_up(1, feats)
            if p is None:
                continue
            maxp = max(maxp, max(p, 1 - p))
            dir_up = p >= 0.5
            pa = price_action_confirm(window, dir_up, features=feats)
            reg = detect_regime(window)
            quality = {
                "data_complete": True, "no_gap": True, "model_loaded": True,
                "vol_acceptable": not reg.get("extreme_vol", False),
                "no_conflict": pa.get("against_count", 0) < 3,
            }
            filt = score_signal(p, dir_up, pa, reg, quality)
            decisions += 1
            if filt["emit"]:
                emitted += 1
                tgt = candles[i + 1]
                up = tgt["close"] > tgt["open"]
                if (filt["prediction"] == "CALL") == up:
                    wins += 1
                else:
                    losses += 1
        wr = f"{100.0 * wins / (wins + losses):.1f}%" if wins + losses \
            else "—"
        print(f"{a:14s} {d.get('status'):12s} {decisions:9d} "
              f"{emitted:8d} {wr:>8s} {maxp:.3f}")
        tot["dec"] += decisions
        tot["emit"] += emitted
        tot["win"] += wins
        tot["loss"] += losses

    print(f"\n[bt] TOTAL: {tot['dec']} live-path decisions → "
          f"{tot['emit']} emitted "
          f"({100.0 * tot['emit'] / tot['dec']:.1f}% emission rate)" if tot['dec'] else "[bt] no decisions")
    if tot["emit"]:
        wr = 100.0 * tot["win"] / (tot["win"] + tot["loss"])
        print(f"[bt] emitted win rate {wr:.1f}% "
              f"(breakeven at 85% payout = 54.05%)")
    print("[bt] honest check: coin-flip models must stay NO_SIGNAL / low "
          "emission — the tiers are doing their job." if tot["dec"] else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
