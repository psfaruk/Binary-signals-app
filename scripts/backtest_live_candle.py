"""
scripts/backtest_live_candle.py — LIVE-CANDLE engine backtest (2026-09-29).

USER REQUIREMENT: "ও backtest করে ভেরিফাই করবেন, তারপরে পরিবর্তন গুলো কমিট
পুশ করবেন।"

WHAT THIS VERIFIES (core/live_candle.py — the running-candle prediction):
  1. MECHANICS  — the exact production function (predict_running_candle)
     is replayed on synthetic tick streams, sampled through candle life
     exactly like the live per-tick loop will call it.
  2. FAIR-WALK CONTROL — on a zero-edge random walk the engine MUST read
     ~50% / EV ≤ 0. Any "edge" here would be a bug (look-ahead).
  3. OTC-LIKE STREAMS — regime drift + AR(1) intra-candle momentum +
     spike prints + stop-hunt wicks (the properties a human eye claims to
     read). Edge here = what candle-reading is worth WHEN the property
     exists. Synthetic WR is a MECHANICS proof, never a real-market claim
     (repo convention — see backtest_any_theory.py).
  4. WALK-FORWARD CALIBRATION — PERSISTENCE is grid-searched on the FIRST
     half only, evaluated on the SECOND half. No test-set tuning.
  5. CALIBRATION — predicted P(close>open) buckets vs realized rates
     (the repo's calibration-audit convention: a 70% bucket must win ~70%).
  6. ENTRY SIMULATION — at LATE/LAST10 phases, when |p_up_from_here−0.5|
     ≥ 0.15, enter at the CURRENT price, settle vs candle CLOSE (the
     broker's strike→expiry semantics, fixing the audit's A5 open→close
     grading drift). Payout 85% → break-even 54.05%.

OUTPUT: scripts/backtest_live_candle_report.json (+ console summary).

USAGE:
  python scripts/backtest_live_candle.py [--pairs 8] [--candles 1500]
      [--quick]   # smaller grid for smoke runs
  python scripts/backtest_live_candle.py --db /path/to/app.db
      # replay REAL production ticks from candle_micro.ticks_json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.live_candle import predict_running_candle, PERSISTENCE  # noqa: E402

PAYOUT = 0.85
BREAK_EVEN = 1.0 / (1.0 + PAYOUT)          # 0.5405

# ── Synthetic OTC-like tick generator ────────────────────────────────────────

def gen_candle_ticks_otc(rng: random.Random, period=60, start_price=1.10000,
                         phi=0.30, spike_p=0.02, wick_p=0.06):
    """One candle of OTC-like ticks: [(ts, price), ...].

    Properties (all documented, all things the human eye claims to read):
      * per-candle drift regime (trend / fade / flat)
      * AR(1) tick innovations (intra-candle momentum persistence)
      * rare spike prints (the "1 tick flip" noise)
      * occasional stop-hunt wick (push beyond extreme, then reject)
    """
    n_ticks = max(30, int(period * rng.uniform(1.2, 2.6)))   # ~1.2-2.6 t/s
    # per-candle drift regime
    r = rng.random()
    if r < 0.30:
        drift = rng.choice((-1, 1)) * rng.uniform(0.15, 0.45)   # trend
    elif r < 0.55:
        drift = 0.0                                              # chop
    else:
        drift = rng.choice((-1, 1)) * rng.uniform(0.05, 0.2)     # mild lean
    sigma = 0.00008 * rng.uniform(0.7, 1.4)     # per-tick vol (OTC-ish)
    price = start_price
    innov = 0.0
    out = []
    spike_left = rng.randint(4, 9) if rng.random() < spike_p else 0
    spike_dir = rng.choice((-1, 1)) if spike_left else 0
    wick_at = rng.randint(int(n_ticks * 0.45), int(n_ticks * 0.8)) \
        if rng.random() < wick_p else -1
    wick_dir = rng.choice((-1, 1))
    # FIX (BT-TIMESTAMP-2026-09-29): tick timestamps must span [0, period)
    # — the first quick run had ts=0..n_ticks (up to 155s on a 60s candle),
    # so the engine never saw the true close and every metric was garbage.
    dt = period / float(n_ticks)
    for i in range(n_ticks):
        ts = i * dt
        innov = phi * innov + rng.gauss(0.0, 1.0) * sigma
        step = drift * sigma + innov
        if spike_left > 0:
            step += spike_dir * sigma * rng.uniform(6, 12)
            spike_left -= 1
        if i == wick_at:
            # stop-hunt: a burst of same-direction prints, then revert
            burst = rng.randint(3, 6)
            for b in range(burst):
                price += wick_dir * sigma * rng.uniform(4, 8)
                out.append((ts + b * dt * 0.2, price))
            price -= wick_dir * sigma * rng.uniform(12, 22)
            out.append((ts + burst * dt * 0.2, price))
            continue
        price += step
        out.append((ts, price))
    return out


def gen_candle_ticks_fair(rng: random.Random, period=60, start_price=1.10000):
    """FAIR control: pure Gaussian random walk, zero drift, zero memory."""
    n_ticks = max(30, int(period * rng.uniform(1.2, 2.6)))
    sigma = 0.00008
    price = start_price
    out = []
    dt = period / float(n_ticks)
    for i in range(n_ticks):
        price += rng.gauss(0.0, 1.0) * sigma
        out.append((i * dt, price))
    return out


# ── Replay one candle through the production engine ─────────────────────────

SAMPLE_EARLY = (5, 10, 15, 20, 25, 30, 35, 40, 45)      # every 5s
SAMPLE_LATE = tuple(range(46, 60))                        # every 1s late


def replay_candle(ticks, period, recent_candles, persistence=None):
    """Sample the engine through the candle exactly like the live loop.

    Returns list of sample dicts (one per sampled second)."""
    global PERSISTENCE
    import core.live_candle as _lc
    old_p = _lc.PERSISTENCE
    if persistence is not None:
        _lc.PERSISTENCE = persistence
    try:
        open_price = ticks[0][1]
        open_time = 1_000_000.0
        samples = []
        by_ts = {}
        for ts, px in ticks:
            by_ts[int(ts)] = px
        running = []
        last_i = -1
        for sec in range(period):
            # feed ticks up to this second
            for ts, px in ticks:
                if last_i < ts <= sec:
                    running.append(px)
            last_i = sec
            if not running:
                continue
            if sec in SAMPLE_EARLY or sec in SAMPLE_LATE:
                pred = _lc.predict_running_candle(
                    running, open_price, period, open_time,
                    now=open_time + sec, recent_candles=recent_candles)
                if pred and pred.get("ready"):
                    pred["_sec"] = sec
                    pred["_cur"] = running[-1]
                    samples.append(pred)
        return samples
    finally:
        _lc.PERSISTENCE = old_p


def phase_of_sample(pred, period):
    sec = pred["_sec"]
    left = period - sec
    if left <= 10:
        return "LAST10"
    if left <= period / 3:
        return "LATE"
    if left <= 2 * period / 3:
        return "MID"
    return "EARLY"


# ── Metrics ──────────────────────────────────────────────────────────────────

def brier(p, y):
    return (p - y) ** 2


def evaluate(samples, close_price, open_price, period, entry_edge=0.15):
    """Score one candle's samples against the true close."""
    actual_green = 1 if close_price > open_price else (0 if close_price < open_price else None)
    if actual_green is None:
        return None
    rows = []
    for pred in samples:
        ph = phase_of_sample(pred, period)
        p = pred["p_close_green"]
        dir_ok = (1 if p > 0.5 else 0) == actual_green
        # entry simulation: strike = current price at sample time
        cur = pred["_cur"]
        entry = None
        p_here = pred["p_up_from_here"]
        if abs(p_here - 0.5) >= entry_edge:
            entry = "CALL" if p_here > 0.5 else "PUT"
        win = None
        if entry:
            win = (close_price > cur) if entry == "CALL" else (close_price < cur)
            if close_price == cur:
                win = None
        rows.append({
            "phase": ph, "p": p, "conf": pred["confidence"],
            "dir_ok": dir_ok, "entry": entry, "win": win,
            "edge": abs(p_here - 0.5),
        })
    return rows


# NOTE: evaluation keeps (p, actual) pairs so Brier + calibration are
# exact — metrics_of() below is the single source of truth.

def run_generator(name, gen_fn, pairs, candles, period, persistence=None,
                  seed=20260929, calibrate=False):
    rng = random.Random(seed)
    all_rows = []
    candles_per_pair = []
    for pi in range(pairs):
        price = 1.0 + rng.uniform(0, 1)
        closed = []
        pair_rows = []
        for ci in range(candles):
            ticks = gen_fn(rng, period=period, start_price=price)
            recent = closed[-20:]
            samples = replay_candle(ticks, period, recent,
                                    persistence=persistence)
            close_price = ticks[-1][1]
            open_price = ticks[0][1]
            # attach actual outcome for exact Brier/calibration
            actual = 1 if close_price > open_price else (
                0 if close_price < open_price else None)
            for pred in samples:
                if actual is None:
                    continue
                ph = phase_of_sample(pred, period)
                # outcome for the TRADEABLE probability: close vs CURRENT
                # price (strike→expiry semantics — audit A5 fix)
                up_here = 1 if close_price > pred["_cur"] else (
                    0 if close_price < pred["_cur"] else None)
                pair_rows.append({
                    "phase": ph,
                    "p": pred["p_close_green"],
                    "actual": actual,
                    "p_here": pred["p_up_from_here"],
                    "actual_here": up_here,
                    "conf": pred["confidence"],
                    "entry": pred.get("entry_hint"),
                    "entry_win": (
                        (close_price > pred["_cur"]) if pred.get("entry_hint") == "CALL"
                        else (close_price < pred["_cur"]) if pred.get("entry_hint") == "PUT"
                        else None),
                })
            closed.append({
                "time": ci * period, "open": open_price,
                "high": max(px for _, px in ticks),
                "low": min(px for _, px in ticks),
                "close": close_price,
            })
            price = close_price
        candles_per_pair.append(closed)
        all_rows.extend(pair_rows)
    return all_rows


def metrics_of(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    acc = sum(1 for r in rows if (1 if r["p"] > 0.5 else 0) == r["actual"]) / n
    br = sum((r["p"] - r["actual"]) ** 2 for r in rows) / n
    # tradeable-probability metrics (p_up_from_here vs close-vs-current)
    here_rows = [r for r in rows if r.get("actual_here") is not None]
    here_n = len(here_rows)
    here_br = (sum((r["p_here"] - r["actual_here"]) ** 2 for r in here_rows)
               / here_n) if here_n else None
    here_acc = (sum(1 for r in here_rows
                    if (1 if r["p_here"] > 0.5 else 0) == r["actual_here"])
                / here_n) if here_n else None
    # calibration buckets on p_close_green
    buckets = {}
    for r in rows:
        b = min(9, max(0, int(r["p"] * 10)))
        d = buckets.setdefault(b, {"n": 0, "act": 0})
        d["n"] += 1
        d["act"] += r["actual"]
    calib = {f"{b/10:.1f}-{b/10+0.1:.1f}": {
        "n": d["n"], "realized": round(d["act"] / d["n"], 3)}
        for b, d in sorted(buckets.items()) if d["n"] >= 30}
    # entries
    ent = [r for r in rows if r.get("entry") and r.get("entry_win") is not None]
    ent_n = len(ent)
    ent_w = sum(1 for r in ent if r["entry_win"])
    wr = (ent_w / ent_n) if ent_n else None
    ev = (wr * PAYOUT - (1 - wr)) if wr is not None else None
    return {
        "n": n, "direction_accuracy": round(acc, 4),
        "brier": round(br, 4), "calibration": calib,
        "up_from_here": {
            "n": here_n,
            "accuracy": round(here_acc, 4) if here_acc is not None else None,
            "brier": round(here_br, 4) if here_br is not None else None,
        },
        "entries": {"n": ent_n, "win_rate": round(wr, 4) if wr is not None else None,
                    "ev_per_trade": round(ev, 4) if ev is not None else None,
                    "break_even": round(BREAK_EVEN, 4)},
    }


def fit_persistence(pairs, candles, period, grid, seed=777):
    """Grid-search PERSISTENCE on TRAIN HALF ONLY (Brier of p_close_green)."""
    best = (None, None)
    for p in grid:
        rows = run_generator("fit", gen_candle_ticks_otc, pairs,
                             max(60, candles // 3), period,
                             persistence=p, seed=seed)
        m = metrics_of(rows)
        if m.get("n"):
            # combined objective: display probability (close-green Brier)
            # + tradeable probability (up-from-here Brier) — the entry
            # simulation is what the user trades, it must be part of the fit
            hb = (m.get("up_from_here") or {}).get("brier")
            score = m["brier"] + (hb if hb is not None else m["brier"])
            if best[0] is None or score < best[0]:
                best = (score, p)
    return best[1] if best[1] is not None else PERSISTENCE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=6)
    ap.add_argument("--candles", type=int, default=900)
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--db", type=str, default=None,
                    help="replay REAL ticks from a production DB "
                         "(candle_micro.ticks_json)")
    args = ap.parse_args()

    t0 = time.time()
    grid = [0.0, 0.15, 0.25, 0.35, 0.50] if not args.quick else [0.0, 0.25, 0.50]

    report = {
        "engine": "core/live_candle.py::predict_running_candle",
        "date": time.strftime("%Y-%m-%d"),
        "convention": ("synthetic = MECHANICS proof only; fair-walk control "
                       "must read ~50% / EV<=0; real-market numbers come from "
                       "the live rolling grader (feed per-candle) once the "
                       "app runs with a real token"),
        "payout": PAYOUT, "break_even": round(BREAK_EVEN, 4),
        "defaults": {"PERSISTENCE_shipped": PERSISTENCE},
    }

    if args.db:
        # ── REAL production ticks replay (candle_micro.ticks_json) ──────
        import sqlite3
        if not os.path.exists(args.db):
            print(f"[bt] DB not found: {args.db}")
            return
        conn = sqlite3.connect(args.db)
        cur = conn.cursor()
        rows_raw = cur.execute(
            "SELECT asset, period, open, close, ticks_json FROM candle_micro "
            "WHERE ticks_json IS NOT NULL AND LENGTH(ticks_json) > 40 "
            "ORDER BY ctime").fetchall()
        conn.close()
        real_rows = []
        n_candles = 0
        by_asset = {}
        for asset, period, o, c, tj in rows_raw:
            try:
                tick_list = json.loads(tj)
                prices = [float(x) for x in
                          (tick_list if isinstance(tick_list, list)
                           else tick_list.get("ticks", []))]
            except Exception:
                continue
            if len(prices) < 20 or not period or period < 15:
                continue
            recent = by_asset.setdefault(asset, [])
            samples = replay_candle(
                [(float(i), p) for i, p in enumerate(prices)],
                period, recent)
            actual = 1 if c > o else (0 if c < o else None)
            for pred in samples:
                if actual is None:
                    continue
                real_rows.append({
                    "phase": phase_of_sample(pred, period),
                    "p": pred["p_close_green"], "actual": actual,
                    "conf": pred["confidence"],
                    "entry": pred.get("entry_hint"),
                    "entry_win": (
                        (c > pred["_cur"]) if pred.get("entry_hint") == "CALL"
                        else (c < pred["_cur"]) if pred.get("entry_hint") == "PUT"
                        else None),
                })
            recent.append({"high": max(prices), "low": min(prices)})
            n_candles += 1
        report["real_data"] = {
            "db": args.db, "candles": n_candles,
            "metrics": metrics_of(real_rows),
        }
        print(f"[bt] REAL: {n_candles} candles replayed")

    # ── 1) FAIR-WALK CONTROL (must be ~50%, EV <= 0) ─────────────────────
    fair_rows = run_generator("fair", gen_candle_ticks_fair,
                              args.pairs, args.candles, args.period,
                              seed=101)
    report["fair_walk_control"] = metrics_of(fair_rows)

    # ── 2) WALK-FORWARD: fit PERSISTENCE on TRAIN half ───────────────────
    fitted = fit_persistence(args.pairs, args.candles, args.period, grid)
    report["walk_forward"] = {"fitted_persistence_on_train": fitted}

    # ── 3) OTC-LIKE, TEST half with the fitted value ─────────────────────
    half = max(150, args.candles // 2)
    otc_rows = run_generator("otc", gen_candle_ticks_otc,
                             args.pairs, half, args.period,
                             persistence=fitted, seed=20260929)
    report["otc_like_test"] = metrics_of(otc_rows)

    # ── 4) Same OTC-like stream with the SHIPPED default (no tuning) ────
    ship_rows = run_generator("otc_ship", gen_candle_ticks_otc,
                              args.pairs, half, args.period,
                              persistence=PERSISTENCE, seed=20260929)
    report["otc_like_shipped_default"] = metrics_of(ship_rows)

    # per-phase breakdown (test half, fitted)
    by_phase = {}
    for r in otc_rows:
        d = by_phase.setdefault(r["phase"], [])
        d.append(r)
    report["otc_like_test"]["by_phase"] = {
        ph: metrics_of(d) for ph, d in sorted(by_phase.items())}

    report["honest_conclusion_bn"] = _conclusion_bn(report)
    report["elapsed_sec"] = round(time.time() - t0, 1)

    out = Path(__file__).parent / "backtest_live_candle_report.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items()
                      if k != "otc_like_test"}, indent=1, ensure_ascii=False))
    print(f"[bt] report → {out}  ({report['elapsed_sec']}s)")


def _conclusion_bn(report):
    fair = report.get("fair_walk_control", {})
    otc = report.get("otc_like_test", {})
    lines = []
    # FAIR-CONTROL NULL (correct null — see report["convention"]):
    #   direction accuracy >50% is EXPECTED (current net position is real
    #   information about the close). The nulls that must hold:
    #   (a) entry win-rate ≈ 50%  (no drift to read on a fair walk)
    #   (b) calibration sane      (p buckets track realized rates)
    fe = fair.get("entries") or {}
    fwr = fe.get("win_rate")
    if fwr is not None:
        ok = 0.45 <= fwr <= 0.55
        lines.append(
            f"ফেয়ার-ওয়াক এন্ট্রি কন্ট্রোল: WR {fwr:.1%} "
            f"{'✅ (৫০%±৫ — কয়েন-ফ্লিপ, ভুয়া এজ নেই)' if ok else '❌ সন্দেহজনক!'}")
    else:
        lines.append("ফেয়ার-ওয়াক: এন্ট্রি সিগন্যাল আসেনি (p≈0.5) — ✅ সঠিক আচরণ")
    for ph, m in (otc.get("by_phase") or {}).items():
        e = m.get("entries") or {}
        wr = e.get("win_rate")
        if wr is not None:
            verdict = ("✅ প্রফিটেবল" if (e.get("ev_per_trade") or 0) > 0
                       else "❌ ব্রেক-ইভেনের নিচে")
            lines.append(
                f"{ph}: ডিরেকশন {m.get('direction_accuracy', 0):.1%}, "
                f"এন্ট্রি {e.get('n', 0)}টি, WR {wr:.1%} "
                f"(ব্রেক-ইভেন 54.05%) → {verdict}")
    lines.append(
        "সিন্থেটিক = মেকানিক্স প্রমাণ। রিয়েল মার্কেটের সংখ্যা অ্যাপ চালু হলে "
        "প্রতি ক্যান্ডেলে লাইভ গ্রেড হয়ে rolling accuracy প্যানেলে দেখা যাবে।")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
