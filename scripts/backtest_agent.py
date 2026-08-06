#!/usr/bin/env python3
"""
scripts/backtest_agent.py — walk-forward backtest of core/agent_brain.py.

WHY THIS EXISTS
===============
On 2026-08-06 the agent was running in production with QX_AGENT_FINAL=1, i.e.
it was allowed to FLIP and NEUTRALISE live signals, on a randomly-initialised
network that had seen ~20 samples per asset and was not persisted across
redeploys. Nothing in the repo measured whether its P(win) carried any
information. This script does.

WHAT IT MEASURES
================
Everything runs against real production data pulled from /api/db-download:
  candle_micro.ticks_json — the actual per-candle tick price series
  signal_log              — the engine's signal for that candle + its outcome

Timeline semantics (verified against feed.py):
  signal_log.ctime is the candle the prediction was FOR; the prediction is
  produced at the close of candle (ctime - period). So at decision time the
  agent may only see candles with ctime' <= ctime - period. The harness
  enforces that — there is no lookahead.

Reported:
  * baseline      — engine win rate with the agent doing nothing
  * traded win    — win rate on the signals the agent chose to trade
  * AUC           — does P(win) rank winners above losers? 0.50 = no signal
  * seed sweep    — same data, different RNG seed; a real edge is seed-stable

RESULTS ON 2,569 REAL GRADED SIGNALS (2026-08-06, ~6.5h of live data)
=====================================================================
  engine baseline                       48.19%
  AUC, tick feed as deployed            0.5015
  AUC, tick feed bug fixed              0.5087
  AUC across 8 seeds            0.4835 - 0.5165  (mean 0.5028, sd 0.0103)
  ungated traded win across 8 seeds  44.63 - 51.78%  (sd 2.24pp)
  ungated flip win across 8 seeds    43.72 - 56.89%  (sd 3.92pp)

Reading: AUC sits on 0.50 and the apparent out-performance moves by more than
10pp on the RNG seed alone. The network has no measured edge on this data, so
the per-asset authority gate in core/agent_brain.py holds it at OBSERVE.
Re-run this after collecting more data before granting it authority.

USAGE
=====
  # grab a fresh copy of the production DB
  curl -o /tmp/signals.db https://<app>/api/db-download
  python scripts/backtest_agent.py /tmp/signals.db
  python scripts/backtest_agent.py /tmp/signals.db --seeds 1,7,42,99
"""
import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TICK_HISTORY_CANDLES = 12   # prior candles of ticks kept in the agent's buffer


# ── Data ────────────────────────────────────────────────────────────────────

def load(db_path, period=60):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    candles = defaultdict(list)
    for r in conn.execute(
        "SELECT asset, ctime, open, high, low, close, ticks_json "
        "FROM candle_micro WHERE period=? ORDER BY asset, ctime", (period,)
    ):
        try:
            ticks = [float(t) for t in json.loads(r["ticks_json"] or "[]")
                     if isinstance(t, (int, float))]
        except Exception:
            ticks = []
        candles[r["asset"]].append({
            "time": r["ctime"], "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"], "ticks": ticks})
    signals = [dict(r) for r in conn.execute(
        "SELECT asset, ctime, signal, confidence, accuracy FROM signal_log "
        "WHERE period=? AND signal IN ('CALL','PUT') "
        "AND accuracy IN ('correct','wrong') ORDER BY ctime, asset", (period,))]
    conn.close()
    return candles, signals


def tick_stream(candle_list, upto_idx, n_candles, starved, period=60):
    """Rebuild the tick buffer the agent would hold at decision time.

    starved=True reproduces the pre-fix production feed, where only the
    candle-boundary batch reached the agent (~1 tick per candle instead of the
    ~90 Quotex actually sends).
    """
    out = []
    ms = period * 1000.0
    for i in range(max(0, upto_idx - n_candles + 1), upto_idx + 1):
        cd = candle_list[i]
        if not cd["ticks"]:
            continue
        t0 = cd["time"] * 1000.0
        if starved:
            out.append((t0 + ms - 1000.0, cd["ticks"][-1]))
        else:
            step = ms / len(cd["ticks"])
            out.extend((t0 + j * step, p) for j, p in enumerate(cd["ticks"]))
    return out


# ── Metrics ─────────────────────────────────────────────────────────────────

def auc(pairs):
    """Tie-aware Mann-Whitney AUC over (score, label) pairs."""
    n1 = sum(1 for _, y in pairs if y)
    n0 = len(pairs) - n1
    if n1 < 1 or n0 < 1:
        return float("nan")
    ordered = sorted(pairs, key=lambda x: x[0])
    rank_sum_pos, i = 0.0, 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        mid = (i + j) / 2.0 + 1.0
        rank_sum_pos += sum(mid for k in range(i, j + 1) if ordered[k][1])
        i = j + 1
    return (rank_sum_pos - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def pct(a, b):
    return (a / b * 100.0) if b else float("nan")


class _Mute:
    """The agent logs a line per decision; silence it during the sweep."""
    def __enter__(self):
        self._old = sys.stdout
        # utf-8 matters: the agent prints emoji, and a cp1252 sink would raise
        # inside evaluate()'s except-all and turn every decision into a no-op.
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        return self

    def __exit__(self, *a):
        sys.stdout.close()
        sys.stdout = self._old


# ── Driver ──────────────────────────────────────────────────────────────────

def run(db_path, gate_on, starved=False, seed=1234, period=60):
    import core.agent_brain as ab
    ab.AGENT_GATE_ENABLED = gate_on
    random.seed(seed)

    candles, signals = load(db_path, period)
    idx = {(a, c["time"]): i
           for a, lst in candles.items() for i, c in enumerate(lst)}

    brain = ab.AgentBrain()
    brain.models.clear()          # never inherit persisted production weights
    brain.final_mode = True
    brain.active_mode = True

    st = dict(n=0, base_correct=0, traded=0, traded_correct=0, flipped=0,
              flip_correct=0, neutralized=0, gated=0, live_feats=0)
    scored = []

    for s in signals:
        asset, ctime = s["asset"], s["ctime"]
        i = idx.get((asset, ctime - period))
        if i is None:
            continue
        lst = candles[asset]
        model = brain._get_model(asset)
        model.ticks.clear()
        for ts, px in tick_stream(lst, i, TICK_HISTORY_CANDLES, starved, period):
            model.add_tick(ts, px)
        closed = [{k: c[k] for k in ("time", "open", "high", "low", "close")}
                  for c in lst[max(0, i - 40):i + 1]]

        if not model.has_authority():
            st["gated"] += 1
        res = brain.evaluate({"signal": s["signal"], "confidence": s["confidence"]},
                             asset, closed, period=period, ctime=ctime)

        base_ok = (s["accuracy"] == "correct")
        actual = s["signal"] if base_ok else (
            "PUT" if s["signal"] == "CALL" else "CALL")
        st["n"] += 1
        st["base_correct"] += base_ok
        if any(abs(f) > 1e-12 for f in (res.get("features") or [])):
            st["live_feats"] += 1
        scored.append((res["p_win"], base_ok))

        final = res.get("final_signal", s["signal"])
        if res["verdict"] == "VETO" or final == "NEUTRAL":
            st["neutralized"] += 1
        else:
            st["traded"] += 1
            ok = (final == actual)
            st["traded_correct"] += ok
            if final != s["signal"]:
                st["flipped"] += 1
                st["flip_correct"] += ok

        if final in ("CALL", "PUT"):
            brain.learn(asset, final, final == actual, period=period, ctime=ctime)

    st["auc"] = auc(scored)
    st["scored"] = scored
    return st, brain


def show(label, st):
    print(f"\n--- {label} ---")
    print(f"  signals                 : {st['n']}")
    print(f"  engine baseline win     : {pct(st['base_correct'], st['n']):.2f}%")
    print(f"  candles with live feats : {pct(st['live_feats'], st['n']):.1f}%")
    print(f"  gate held at OBSERVE    : {st['gated']} "
          f"({pct(st['gated'], st['n']):.1f}%)")
    print(f"  agent traded            : {st['traded']} "
          f"({pct(st['traded'], st['n']):.1f}%)   win "
          f"{pct(st['traded_correct'], st['traded']):.2f}%")
    print(f"    flipped engine dir    : {st['flipped']}   win "
          f"{pct(st['flip_correct'], st['flipped']):.2f}%")
    print(f"  agent neutralised       : {st['neutralized']}")
    print(f"  P(win) AUC (oos)        : {st['auc']:.4f}   "
          f"[0.50 = no information]")


def calibration_table(scored):
    base = pct(sum(1 for _, ok in scored if ok), len(scored))
    print(f"\n--- P(win) calibration (n={len(scored)}, baseline {base:.2f}%) ---")
    print(f"  {'bucket':>16} {'n':>6} {'actual win':>11} {'lift':>8}")
    edges = [0.0, .40, .45, .48, .50, .52, .55, .60, 1.01]
    for lo, hi in zip(edges, edges[1:]):
        b = [ok for p, ok in scored if lo <= p < hi]
        if not b:
            continue
        w = pct(sum(b), len(b))
        print(f"  {f'[{lo:.2f},{hi:.2f})':>16} {len(b):>6} {w:>10.2f}% "
              f"{w - base:>+7.2f}")
    print("  (a model with an edge is MONOTONIC here: higher bucket, higher win)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="path to a signals.db copy (see /api/db-download)")
    ap.add_argument("--period", type=int, default=60)
    ap.add_argument("--seeds", default="1,7,42,99,123,1234,2026,31337",
                    help="comma-separated RNG seeds for the stability sweep")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no such DB: {args.db}")
    # Keep the agent's own persistence away from the DB under test.
    os.environ["DB_PATH"] = args.db + ".agentscratch"

    print("=" * 74)
    print("AGENT BACKTEST — walk-forward, no lookahead, real production data")
    print("=" * 74)

    with _Mute():
        st_starved, _ = run(args.db, gate_on=False, starved=True, period=args.period)
    show("tick feed STARVED (pre-fix production), gate OFF", st_starved)

    with _Mute():
        st_fed, brain = run(args.db, gate_on=False, starved=False, period=args.period)
    show("tick feed FED (post-fix), gate OFF", st_fed)
    calibration_table(st_fed["scored"])

    with _Mute():
        st_gate, brain = run(args.db, gate_on=True, starved=False, period=args.period)
    show("tick feed FED, authority gate ON (shipped default)", st_gate)

    print("\n--- authority gate outcome ---")
    for k, v in brain.authority_summary().items():
        print(f"  {k:22}: {v}")
    print(f"\n  {'asset':16} {'graded':>7} {'AUC':>8} {'lower95':>8}  verdict")
    for m in sorted(brain.models.values(),
                    key=lambda m: -len(m.recent_predictions))[:15]:
        a, lo = m.edge_auc(), m.edge_auc_lower()
        print(f"  {m.asset:16} {len(m.recent_predictions):>7} "
              f"{(f'{a:.4f}' if a is not None else 'n/a'):>8} "
              f"{(f'{lo:.4f}' if lo is not None else 'n/a'):>8}  "
              f"{'AUTHORISED' if m.has_authority() else 'gated'}")

    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    if len(seeds) > 1:
        print(f"\n--- seed stability, gate OFF ({len(seeds)} seeds, same data) ---")
        print(f"  {'seed':>7} {'traded':>7} {'traded win%':>12} "
              f"{'flips':>6} {'flip win%':>10} {'AUC':>8}")
        wins, aucs, flips = [], [], []
        for sd in seeds:
            with _Mute():
                st, _ = run(args.db, gate_on=False, seed=sd, period=args.period)
            w, f = (pct(st["traded_correct"], st["traded"]),
                    pct(st["flip_correct"], st["flipped"]))
            wins.append(w); flips.append(f); aucs.append(st["auc"])
            print(f"  {sd:>7} {st['traded']:>7} {w:>11.2f}% "
                  f"{st['flipped']:>6} {f:>9.2f}% {st['auc']:>8.4f}")
        # FIX (BACKTEST-CRASH-2026-08-07): statistics.pstdev crashes on
        # lists containing NaN/None (float has no .numerator). Filter those
        # out before computing mean/sd. Also guard against empty lists.
        wins_f  = [w for w in wins  if w is not None and not (isinstance(w, float) and math.isnan(w))]
        flips_f = [f for f in flips if f is not None and not (isinstance(f, float) and math.isnan(f))]
        aucs_f  = [a for a in aucs  if a is not None and not (isinstance(a, float) and math.isnan(a))]
        if wins_f:
            print(f"\n  traded win  mean/sd : {statistics.mean(wins_f):.2f}% / "
                  f"{statistics.pstdev(wins_f):.2f}pp  "
                  f"range {min(wins_f):.2f}-{max(wins_f):.2f}")
        else:
            print("\n  traded win  mean/sd : (no data)")
        if flips_f:
            print(f"  flip win    mean/sd : {statistics.mean(flips_f):.2f}% / "
                  f"{statistics.pstdev(flips_f):.2f}pp  "
                  f"range {min(flips_f):.2f}-{max(flips_f):.2f}")
        else:
            print("  flip win    mean/sd : (no data)")
        if aucs_f:
            print(f"  AUC         mean/sd : {statistics.mean(aucs_f):.4f} / "
                  f"{statistics.pstdev(aucs_f):.4f}  "
                  f"range {min(aucs_f):.4f}-{max(aucs_f):.4f}")
        else:
            print("  AUC         mean/sd : (no data)")
        print("\n  A real edge is stable across seeds. If traded-win swings by "
              "more than\n  a couple of points here, you are looking at noise, "
              "not skill.")
    print()


if __name__ == "__main__":
    main()
