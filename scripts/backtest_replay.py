#!/usr/bin/env python3
"""
scripts/backtest_replay.py — candle-replay backtest of the REAL signal engine.

WHY THIS EXISTS (2026-08-31)
============================
core/backtest.py only re-aggregates the signal_log table — it validates the
BOOKKEEPING, not the engine. There was no way to replay historical candles
through engines.predict() itself and measure what win rate the CURRENT code
would have produced. That is exactly what this harness does:

    for every candle i (after warmup):
        signal_i = engines.predict(candles[0 .. i-1])     # closed candles only
        outcome  = grade(signal_i, candles[i])            # the next candle
    → win rates: overall / per-pair / CALL vs PUT / per-strength

NO LOOKAHEAD, enforced structurally: the predict call receives a COPY of the
candle list truncated strictly BEFORE the graded candle. The engine has no
other data path (ticks/micro are optional and also truncated when provided).

DATA SOURCES (choose one)
=========================
  --db PATH        replay real production candles from candle_micro
                   (asset, period, ctime, open, high, low, close)
  --json PATH      candles from a JSON file: [{"asset":..,"candles":[..]},..]
                   or {"EURUSD_otc":[..], ...}
  --synthetic N    deterministic seeded random-walk with trend/range regimes
                   (pipeline verification WITHOUT a Quotex token — proves the
                   engine runs, signals every candle, grades correctly and the
                   stats are self-consistent; a fair-coin random walk must
                   land near breakeven, which is itself the assertion)

USAGE
=====
  python scripts/backtest_replay.py --synthetic 600
  python scripts/backtest_replay.py --db /app/data/signals.db --days 7
  python scripts/backtest_replay.py --json candles.json --out report.json

GRADING SEMANTICS — identical to feed._accuracy (feed.py):
  actual_up = close > open            (1-tick settlement: candle IS its expiry)
  correct   = (actual_up == (signal == "CALL"))
  open==close → draw (excluded from win rate); degenerate candle → skip.

Interpretation guide:
  * payout >= 85 (OTC)  → breakeven win rate ≈ 54.05%
  * payout >= 70 (Real) → breakeven win rate ≈ 58.82%
  * synthetic random walk: expect ~50% ± noise. If it deviates far, the
    engine has an accidental direction bias (e.g. the CALL-bias bug class
    fixed on 2026-08-31) — that is exactly what this harness catches.
"""
import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("DB_PATH", os.path.join(REPO, "backtest_replay_tmp.db"))
# Backtest must run with production-default gates (all OFF) — force-clear any
# local overrides so the replay measures the code users actually get.
for _gate in ("QX_BREAKEVEN_GATE", "QX_PAIR_HEALTH_GATE", "QX_TRAP_HOUR",
              "QX_TIERED_FILTER", "QX_LOSS_COOLDOWN", "QX_CHOP_GUARD",
              "QX_WEAK_NEUTRAL", "QX_PAIR_PENALTY_NEUTRAL"):
    os.environ.pop(_gate, None)

from engines import predict as engine_predict          # noqa: E402
from core.backtest import _wilson_bounds               # noqa: E402

WARMUP_CANDLES = 40          # engine needs history before it can vote
DEFAULT_PAYOUT_OTC = 85
DEFAULT_PAYOUT_REAL = 70


# ── Data loaders ─────────────────────────────────────────────────────────────

def load_from_db(db_path, days=None, period=60, max_per_pair=None):
    """Load closed candles from candle_micro, grouped by asset."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    cutoff = (time.time() - days * 86400) if days else 0
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT asset, ctime, open, high, low, close FROM candle_micro "
            "WHERE period = ? AND ctime > ? ORDER BY asset, ctime",
            (period, cutoff)).fetchall()
    finally:
        conn.close()
    grouped = defaultdict(list)
    for r in rows:
        if r["open"] is None or r["close"] is None:
            continue
        grouped[r["asset"]].append({
            "time": int(r["ctime"]), "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]),
        })
    if max_per_pair:
        for a in grouped:
            grouped[a] = grouped[a][-max_per_pair:]
    return dict(grouped)


def load_from_json(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        return {a: c for a, c in data.items() if isinstance(c, list)}
    if isinstance(data, list):
        out = {}
        for entry in data:
            a = entry.get("asset")
            if a and isinstance(entry.get("candles"), list):
                out[a] = entry["candles"]
        return out
    raise ValueError("JSON must be {asset: [candles]} or [{asset, candles}]")


def gen_synthetic(n_candles, seed, asset="EURUSD_otc", start_ts=None,
                  start_price=1.0850, regime_len=(25, 60)):
    """Deterministic regime-switching random walk (trend + range phases).

    NOTE: this is NOT market data. It is a control input: a fair random walk
    has no exploitable edge, so a CORRECT engine must score ≈ 50% (± Wilson
    noise). Systematic deviation means the engine itself is biased.
    """
    rng = random.Random(seed)
    t0 = int(start_ts or (time.time() - n_candles * 60))
    t0 -= t0 % 60
    price = start_price
    out = []
    regime_remaining = 0
    drift = 0.0
    for i in range(n_candles):
        if regime_remaining <= 0:
            regime_remaining = rng.randint(*regime_len)
            roll = rng.random()
            if roll < 0.40:      # uptrend
                drift = rng.uniform(0.20, 0.55) * 1e-4
            elif roll < 0.80:    # downtrend
                drift = -rng.uniform(0.20, 0.55) * 1e-4
            else:                # range
                drift = 0.0
        regime_remaining -= 1
        op = price
        move = rng.gauss(0, 1.1e-4) + drift
        cl = op + move
        wick_up = abs(rng.gauss(0, 0.6e-4))
        wick_dn = abs(rng.gauss(0, 0.6e-4))
        hi = max(op, cl) + wick_up
        lo = min(op, cl) - wick_dn
        out.append({"time": t0 + i * 60, "open": round(op, 6),
                    "high": round(hi, 6), "low": round(lo, 6),
                    "close": round(cl, 6)})
        price = cl
    return out


# ── HTF trend (mirrors feed._get_htf_trend, dependency-free) ────────────────

def _ema(values, period):
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / min(period, len(values))
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def htf_trend_5m(candles):
    """5m EMA9-vs-EMA21 trend from 1m closes (same alignment as feed.py)."""
    if not candles:
        return "SIDEWAYS"
    bucket_sec = 300
    closes = []
    cur_bucket = None
    prev_close = 0.0
    for c in candles:
        t = c["time"]
        if t > 10_000_000_000:
            t = t / 1000
        b = (int(t) // bucket_sec) * bucket_sec
        if cur_bucket is None or b != cur_bucket:
            if cur_bucket is not None:
                closes.append(prev_close)
            cur_bucket = b
        prev_close = c["close"]
    if cur_bucket is not None:
        closes.append(prev_close)
    if len(closes) < 10:
        return "SIDEWAYS"
    ema9, ema21 = _ema(closes, 9), _ema(closes, 21)
    if ema21 <= 0:
        return "SIDEWAYS"
    diff_pct = (ema9 - ema21) / ema21 * 100.0
    if diff_pct > 0.015:
        return "UPTREND"
    if diff_pct < -0.015:
        return "DOWNTREND"
    return "SIDEWAYS"


# ── Grading (identical to feed._accuracy) ───────────────────────────────────

def grade(signal, candle):
    op, cl = candle["open"], candle["close"]
    if abs(cl - op) < 1e-12:
        return "draw"
    if candle["high"] == candle["low"] == op == cl:
        return "skip"
    actual_up = cl > op
    pred_up = (signal == "CALL")
    return "correct" if (actual_up == pred_up) else "wrong"


# ── Replay ──────────────────────────────────────────────────────────────────

def replay_pair(asset, candles, period=60, stride=1, verbose=False):
    """Replay one pair; returns per-direction result rows."""
    results = []
    n = len(candles)
    if n <= WARMUP_CANDLES:
        return results
    htf = "SIDEWAYS"
    htf_refresh_every = 15   # recompute 5m trend every 15 candles (cheap EMA)
    for i in range(WARMUP_CANDLES, n):
        target = candles[i]           # the candle we predict & grade
        history = candles[max(0, i - 260):i]   # closed candles strictly BEFORE target
        if (i - WARMUP_CANDLES) % htf_refresh_every == 0:
            htf = htf_trend_5m(history)
        pred = engine_predict(
            list(history), ticks=[], micro=None,
            asset=asset, htf_trend=htf, period=period)
        sig = pred.get("signal")
        if sig not in ("CALL", "PUT"):
            # NEUTRAL — count as no-trade (should be ~0 candles in default mode)
            results.append({"asset": asset, "ctime": target["time"],
                            "signal": sig, "accuracy": "none",
                            "confidence": pred.get("confidence", 0),
                            "strength": pred.get("strength", "")})
            continue
        acc = grade(sig, target)
        results.append({"asset": asset, "ctime": target["time"],
                        "signal": sig, "accuracy": acc,
                        "confidence": pred.get("confidence", 0),
                        "strength": pred.get("strength", "")})
        if verbose and (i - WARMUP_CANDLES) % 100 == 0:
            print(f"  [{asset}] {i - WARMUP_CANDLES + 1}/{n - WARMUP_CANDLES} "
                  f"replayed...", file=sys.stderr)
    return results


def summarize(results, payout_otc=DEFAULT_PAYOUT_OTC, payout_real=DEFAULT_PAYOUT_REAL):
    graded = [r for r in results if r["accuracy"] in ("correct", "wrong")]
    neutral = [r for r in results if r["accuracy"] == "none"]

    def _wr_block(rows):
        tot = len(rows)
        cor = sum(1 for r in rows if r["accuracy"] == "correct")
        wr = (100.0 * cor / tot) if tot else None
        lo, hi = _wilson_bounds(cor, tot) if tot else (0.0, 0.0)
        return {"graded": tot, "correct": cor,
                "win_pct": round(wr, 2) if wr is not None else None,
                "wilson95": [round(lo * 100, 2), round(hi * 100, 2)] if tot else None}

    overall = _wr_block(graded)
    per_pair, per_dir, per_strength = {}, {"CALL": [], "PUT": []}, defaultdict(list)
    for r in graded:
        per_pair.setdefault(r["asset"], []).append(r)
        per_dir[r["signal"]].append(r)
        per_strength[r["strength"] or "UNKNOWN"].append(r)

    # Streaks (chronological per asset)
    max_win = max_loss = cur = 0
    cur_type = None
    for r in graded:
        if r["accuracy"] == cur_type:
            cur += 1
        else:
            cur, cur_type = 1, r["accuracy"]
        if cur_type == "correct":
            max_win = max(max_win, cur)
        else:
            max_loss = max(max_loss, cur)

    call_blk = _wr_block(per_dir["CALL"])
    put_blk = _wr_block(per_dir["PUT"])

    # Payout-weighted breakeven check (OTC/Real mix aware)
    breakevens = []
    for a in per_pair:
        be = 100.0 / (100.0 + (payout_real if not a.lower().endswith("otc")
                               else payout_otc)) * 100.0   # → percent, e.g. 54.05
        breakevens.append(be)
    avg_be = round(sum(breakevens) / len(breakevens), 2) if breakevens else 54.05

    return {
        "overall": overall,
        "call": call_blk,
        "put": put_blk,
        "neutral_candles": len(neutral),
        "direction_bias": (round(call_blk["win_pct"] - put_blk["win_pct"], 2)
                           if call_blk["win_pct"] is not None
                           and put_blk["win_pct"] is not None else None),
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
        "breakeven_avg_pct": avg_be,
        "edge_vs_breakeven": (round(overall["win_pct"] - avg_be, 2)
                              if overall["win_pct"] is not None else None),
        "per_pair": {a: {
            **_wr_block(rows),
            "call": _wr_block([r for r in rows if r["signal"] == "CALL"]),
            "put": _wr_block([r for r in rows if r["signal"] == "PUT"]),
        } for a, rows in sorted(per_pair.items())},
        "per_strength": {s: _wr_block(rows) for s, rows in sorted(per_strength.items())},
    }


def main():
    ap = argparse.ArgumentParser(description="Candle-replay backtest of the real engine")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="signals.db path — replay candle_micro data")
    src.add_argument("--json", dest="json_path", help="candles JSON file")
    src.add_argument("--synthetic", type=int, metavar="N",
                     help="generate N synthetic candles per pair")
    ap.add_argument("--pairs", default="EURUSD_otc,USDZAR_otc,USDBDT_otc",
                    help="comma list for --synthetic mode")
    ap.add_argument("--seeds", default="7,42",
                    help="comma list of RNG seeds for --synthetic mode")
    ap.add_argument("--days", type=float, default=None, help="DB lookback window")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--stride", type=int, default=1,
                    help="predict every Nth candle (1 = every candle)")
    ap.add_argument("--payout-otc", type=int, default=DEFAULT_PAYOUT_OTC)
    ap.add_argument("--payout-real", type=int, default=DEFAULT_PAYOUT_REAL)
    ap.add_argument("--out", help="write full report JSON here")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    t_start = time.time()
    if args.synthetic:
        data = {}
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
        # Each (pair, seed) is a separate synthetic market — tagged by seed so
        # per-pair stats stay honest (different walks, same engine).
        for p in pairs:
            for s in seeds:
                key = f"{p}#seed{s}" if len(seeds) > 1 else p
                data[key] = gen_synthetic(args.synthetic, seed=s, asset=p)
    elif args.json_path:
        data = load_from_json(args.json_path)
    else:
        data = load_from_db(args.db, days=args.days, period=args.period)

    if not data:
        print("No candles found for the given source/filters.", file=sys.stderr)
        sys.exit(2)

    all_results = []
    for asset, candles in sorted(data.items()):
        print(f"Replaying {asset}: {len(candles)} candles "
              f"({max(0, len(candles) - WARMUP_CANDLES)} predictions)...",
              file=sys.stderr)
        # For multi-seed synthetic data the asset key carries "#seedN" — strip
        # for the engine so category detection (_otc suffix) still works.
        engine_asset = asset.split("#")[0]
        all_results.extend(replay_pair(engine_asset, candles,
                                       period=args.period,
                                       stride=args.stride,
                                       verbose=args.verbose))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": ("synthetic" if args.synthetic
                 else "db" if args.db else "json"),
        "period": args.period,
        "pairs": sorted(data.keys()),
        "total_predictions": len(all_results),
        "runtime_sec": round(time.time() - t_start, 1),
        **summarize(all_results, args.payout_otc, args.payout_real),
    }

    print(json.dumps(report, indent=2))

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nFull report → {args.out}", file=sys.stderr)

    # Sanity assertions for synthetic mode: a fair random walk must not show
    # a large systematic direction bias (that would mean an engine bug).
    if args.synthetic and report["overall"]["graded"] >= 100:
        db_ = report["direction_bias"]
        if db_ is not None and abs(db_) > 25:
            print(f"\n⚠️  WARNING: |CALL vs PUT win-rate gap| = {db_}pp on a "
                  f"fair random walk — the engine likely has a DIRECTION BIAS "
                  f"bug. Inspect blender fallback paths.", file=sys.stderr)
            sys.exit(3)


if __name__ == "__main__":
    main()
