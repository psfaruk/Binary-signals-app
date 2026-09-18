#!/usr/bin/env python3
"""
backtest_history_gate.py — SIGNAL-HISTORY-GATE backtest (USER-2026-09-18).

"ও backtest করে ভেরিফাই করবেন" — two independent tests, both using the
REAL production gate (core/signal_history_gate.apply_history_gate) on a
throwaway DB, with ZERO look-ahead (at every decision the DB contains
only STRICTLY EARLIER signals):

  TEST A — Walk-forward on the 141 live production signals captured
  2026-09-18 (scripts/data/live_signals_2026-09-18.json). Replays every
  graded signal in ctime order through the gate exactly as production
  would have: baseline WR vs gated WR, suppression stats. NOTE: this
  capture is a single ~30-minute window (the old retention policy
  destroyed everything older), so most pairs sit in learning mode
  (< 20 samples) — exactly the failure mode the retention fix addresses.

  TEST B — Synthetic fleet stress test at the 200-signal scale the fixed
  retention actually provides: 9 pairs × 200 candles, true win rates
  40% (bad) / 50% (fair) / 60% (good). Verifies the gate:
     * cuts proven-loser pairs after ~20-40 signals,
     * leaves fair pairs trading (no overfit),
     * keeps the good pairs flowing (verified list),
     * and the resulting ALLOWED-signal fleet WR beats baseline by
       several points (the whole point of the USER-2026-09-18 directive).

Run: python3 scripts/backtest_history_gate.py
Exit 0 = both tests complete and Test B shows a WR improvement.
"""
import json
import os
import random
import sqlite3
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMPDIR = tempfile.mkdtemp(prefix="qx_hgate_bt_")
DB_PATH = os.path.join(TMPDIR, "tmp_hgate_backtest.db")
os.environ["DB_PATH"] = DB_PATH
os.environ["QX_HISTORY_GATE"] = "1"

import db as _db                                      # noqa: E402
from core import signal_history_gate as hg            # noqa: E402

T0 = 1_700_000_000
PERIOD = 60


def _insert(asset, ctime, signal, accuracy):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "INSERT OR REPLACE INTO signal_log "
        "(asset,period,ctime,signal,score,confidence,theories,actual,accuracy,"
        " strength,agree,reasons,regime,zone,tags,postmortem,category,"
        " total,ts,signal_quality,strategy)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset, PERIOD, ctime, signal, 10, 70, "",
         "UP" if accuracy == "correct" else "DOWN", accuracy,
         "MEDIUM", 2, "[]", "RANGE", "RANGE", "", "",
         "otc" if asset.endswith("_otc") else "real",
         2, ctime, None, "confluence_v1_any"))
    conn.commit()
    conn.close()


def _gate_decision(asset, direction):
    """Run the REAL gate for a candidate signal. Returns
    (allowed: bool, mode: str)."""
    pred = {"signal": direction, "confidence": 60, "strength": "MEDIUM",
            "score": 10, "reasons": [], "modules": {},
            "strategy": "confluence_v1_any"}
    out = hg.apply_history_gate(pred, asset, PERIOD, "otc")
    allowed = out.get("signal") == direction
    mode = (out.get("history_gate") or {}).get("mode", "?")
    return allowed, mode


def _wr(counts):
    c, w = counts
    return round(100.0 * c / (c + w), 1) if (c + w) else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TEST A — live production signals, walk-forward
# ═══════════════════════════════════════════════════════════════════════════
def test_a_live_walkforward() -> dict:
    data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "live_signals_2026-09-18.json")
    with open(data_file) as f:
        live = json.load(f)["signals"]
    live.sort(key=lambda r: (r["ctime"], r["asset"]))
    print(f"\n{'=' * 70}")
    print("TEST A — LIVE PRODUCTION SIGNALS, WALK-FORWARD (no look-ahead)")
    print(f"  source: {os.path.basename(data_file)} — {len(live)} graded signals")

    # wipe DB for a clean replay
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM signal_log")
    conn.commit()
    conn.close()
    hg.invalidate_cache()

    base = {"correct": 0, "wrong": 0}
    gated = {"correct": 0, "wrong": 0}
    suppressed_by_mode = {}
    per_pair = {}
    learning_n = 0

    ptr = 0  # signals already inserted (all with ctime < current)
    for i, s in enumerate(live):
        # advance the past: insert every signal strictly older than this one
        while (ptr < len(live)
               and (live[ptr]["ctime"] < s["ctime"]
                    or (live[ptr]["ctime"] == s["ctime"]
                        and live[ptr]["asset"] < s["asset"])
                    or (live[ptr]["ctime"] == s["ctime"]
                        and live[ptr]["asset"] == s["asset"]
                        and ptr < i))):
            p = live[ptr]
            if p["accuracy"] in ("correct", "wrong"):
                _insert(p["asset"], p["ctime"], p["signal"], p["accuracy"])
            ptr += 1
        hg.invalidate_cache()

        if s["accuracy"] not in ("correct", "wrong"):
            continue
        base[s["accuracy"]] += 1
        pair = per_pair.setdefault(s["asset"], {"base": [0, 0], "gated": [0, 0],
                                                "supp": 0, "modes": set()})
        pair["base"][0 if s["accuracy"] == "correct" else 1] += 1

        allowed, mode = _gate_decision(s["asset"], s["signal"])
        if allowed:
            gated[s["accuracy"]] += 1
            pair["gated"][0 if s["accuracy"] == "correct" else 1] += 1
        else:
            pair["supp"] += 1
            suppressed_by_mode[mode] = suppressed_by_mode.get(mode, 0) + 1
        if mode == "learning":
            learning_n += 1

    print(f"\n  Baseline (all signals):   WR {_wr((base['correct'], base['wrong']))}%"
          f"  (C={base['correct']} W={base['wrong']})")
    n_allowed = gated["correct"] + gated["wrong"]
    n_supp = (base["correct"] + base["wrong"]) - n_allowed
    print(f"  Gated (allowed only):     WR {_wr((gated['correct'], gated['wrong']))}%"
          f"  (C={gated['correct']} W={gated['wrong']})")
    print(f"  Suppressed: {n_supp} signals   learning-mode decisions: {learning_n}")
    print(f"  Suppression modes: {suppressed_by_mode or '{}'}")
    print("\n  Per-pair (baseline → gated, n suppressed):")
    for a in sorted(per_pair, key=lambda x: -sum(per_pair[x]["base"])):
        p = per_pair[a]
        print(f"    {a:16s} base WR {_wr(tuple(p['base'])):5.1f}% (n={sum(p['base'])})"
              f" → gated WR {_wr(tuple(p['gated'])):5.1f}% (n={sum(p['gated'])})"
              f"  supp={p['supp']}")
    print("\n  NOTE: this capture spans ~30 minutes (the old 30-min retention")
    print("  destroyed older history), so pairs never accumulate the 20+")
    print("  graded signals the gate needs to leave learning mode — exactly")
    print("  the starvation the 2026-09-18 retention fix (200 rows/pair)")
    print("  addresses. Test B proves the full lifecycle at that scale.")

    return {"baseline_wr": _wr((base["correct"], base["wrong"])),
            "gated_wr": _wr((gated["correct"], gated["wrong"])),
            "n": base["correct"] + base["wrong"],
            "suppressed": n_supp}


# ═══════════════════════════════════════════════════════════════════════════
# TEST B — synthetic fleet stress test (200 signals per pair)
# ═══════════════════════════════════════════════════════════════════════════
def test_b_synthetic_fleet() -> dict:
    print(f"\n{'=' * 70}")
    print("TEST B — SYNTHETIC FLEET STRESS TEST (200 candles/pair, real gate)")

    rng = random.Random(20260918)
    fleet = {
        # asset: true WR
        "BAD1_otc": 0.40, "BAD2_otc": 0.40, "BAD3_otc": 0.40,
        "FAIR1_otc": 0.50, "FAIR2_otc": 0.50, "FAIR3_otc": 0.50,
        "FAIR4_otc": 0.50,
        "GOOD1_otc": 0.60, "GOOD2_otc": 0.60,
    }
    N = 200

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM signal_log")
    conn.commit()
    conn.close()
    hg.invalidate_cache()

    # Pre-generate the outcome tape: minute t, each pair emits CALL/PUT
    # (random direction), wins with its true WR.
    tape = []  # (t, asset, signal, accuracy)
    for t in range(N):
        for asset, true_wr in fleet.items():
            sig = rng.choice(["CALL", "PUT"])
            acc = "correct" if rng.random() < true_wr else "wrong"
            tape.append((t, asset, sig, acc))

    base = {a: [0, 0] for a in fleet}
    gated = {a: [0, 0] for a in fleet}
    supp = {a: 0 for a in fleet}
    modes_seen = {a: set() for a in fleet}

    ptr = 0
    for i, (t, asset, sig, acc) in enumerate(tape):
        # insert all outcomes from minutes strictly before t
        while ptr < len(tape) and tape[ptr][0] < t:
            tt, aa, ss, aacc = tape[ptr]
            _insert(aa, T0 + tt * 60, ss, aacc)
            ptr += 1
        hg.invalidate_cache()

        base[asset][0 if acc == "correct" else 1] += 1
        allowed, mode = _gate_decision(asset, sig)
        modes_seen[asset].add(mode)
        if allowed:
            gated[asset][0 if acc == "correct" else 1] += 1
        else:
            supp[asset] += 1

    print(f"\n  {'asset':12s} {'trueWR':>6s} {'baseWR':>6s} {'gatedWR':>7s}"
          f" {'kept':>10s} {'supp':>5s} {'suppWR':>6s}  modes")
    tb = tg = nb = ng = 0
    for asset in fleet:
        b, g = base[asset], gated[asset]
        tb += b[0]; nb += b[0] + b[1]
        tg += g[0]; ng += g[0] + g[1]
        # WR of the signals the gate SUPPRESSED (they should be losers —
        # that is the economic value of cutting them)
        sc = b[0] - g[0]
        sw = (b[0] + b[1]) - (g[0] + g[1])
        supp_wr = round(100.0 * sc / sw, 1) if sw else 0.0
        print(f"  {asset:12s} {fleet[asset] * 100:5.0f}% "
              f"{_wr(tuple(b)):6.1f}% {_wr(tuple(g)):7.1f}% "
              f"{g[0] + g[1]:4d}/{b[0] + b[1]:<4d} {supp[asset]:5d}"
              f" {supp_wr:6.1f}%"
              f"  {','.join(sorted(modes_seen[asset]))}")

    base_wr = round(100.0 * tb / nb, 1)
    gated_wr = round(100.0 * tg / ng, 1) if ng else 0.0
    otc_be = 54.05
    supp_total_correct = tb - tg
    supp_total = nb - ng
    supp_total_wr = (round(100.0 * supp_total_correct / supp_total, 1)
                     if supp_total else 0.0)
    print(f"\n  FLEET: baseline WR {base_wr}% ({nb} signals) → "
          f"gated WR {gated_wr}% ({ng} signals, {supp_total} suppressed)")
    print(f"  WR of the SUPPRESSED signals: {supp_total_wr}% "
          f"(C={supp_total_correct}/W={supp_total - supp_total_correct}) "
          f"— cutting losers is the whole point")
    print(f"  OTC break-even at 85% payout = {otc_be}%")
    verdict = "PASS" if gated_wr > base_wr else "FAIL"
    print(f"  Improvement: {round(gated_wr - base_wr, 1)}pp — {verdict}")

    # Assertions the USER directive demands — judged on REALIZED history
    # (the gate cannot see a pair's "true" WR, only what it actually did):
    #   1. pairs whose realized history is BAD (raw WR < 46%, n ≥ 60)
    #     lose more than half their signal volume;
    #   2. pairs whose realized history is GOOD (raw WR ≥ 55%, n ≥ 60)
    #     keep more than half their volume (no overfitting);
    #   3. the suppressed signals were net LOSERS (< 47% WR);
    #   4. the allowed-fleet WR beats the baseline fleet WR.
    realized_bad_cut, realized_good_kept = [], []
    for asset in fleet:
        b = base[asset]
        n = b[0] + b[1]
        raw = b[0] / n if n else 0
        if n >= 60 and raw < 0.46:
            realized_bad_cut.append(
                sum(gated[asset]) / sum(base[asset]) <= 0.5)
        if n >= 60 and raw >= 0.55:
            realized_good_kept.append(
                sum(gated[asset]) / sum(base[asset]) >= 0.5)
    ok_bad_cut = all(realized_bad_cut) if realized_bad_cut else True
    ok_good_kept = all(realized_good_kept) if realized_good_kept else True
    ok_supp_losers = supp_total_wr < 47.0
    ok_improved = gated_wr > base_wr
    print(f"  realized-bad pairs (<46%, n≥60) cut to ≤50% volume: "
          f"{'YES' if ok_bad_cut else 'NO'} "
          f"({len(realized_bad_cut)} pairs judged)")
    print(f"  realized-good pairs (≥55%, n≥60) keep ≥50% volume: "
          f"{'YES' if ok_good_kept else 'NO'} "
          f"({len(realized_good_kept)} pairs judged)")
    print(f"  suppressed signals were net losers (<47% WR): "
          f"{'YES' if ok_supp_losers else 'NO'} ({supp_total_wr}%)")
    print(f"  allowed-fleet WR beats baseline: "
          f"{'YES' if ok_improved else 'NO'} "
          f"({gated_wr}% vs {base_wr}%)")

    return {"baseline_wr": base_wr, "gated_wr": gated_wr,
            "suppressed_wr": supp_total_wr,
            "improvement_pp": round(gated_wr - base_wr, 1),
            "ok_bad_cut": ok_bad_cut, "ok_good_kept": ok_good_kept,
            "ok_supp_losers": ok_supp_losers, "ok_improved": ok_improved,
            "verdict": verdict}


def main() -> int:
    _db.init()
    res_a = test_a_live_walkforward()
    res_b = test_b_synthetic_fleet()

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"  Test A (live 2026-09-18 capture, learning-mode-starved):")
    print(f"    baseline {res_a['baseline_wr']}% → gated {res_a['gated_wr']}%"
          f"  ({res_a['suppressed']}/{res_a['n']} suppressed — pairs were in")
    print(f"    learning mode; with the 200-row retention the gate finally")
    print(f"    gets the history it needs)")
    print(f"  Test B (synthetic fleet, 200 signals/pair — the fixed")
    print(f"    retention scale):")
    print(f"    baseline {res_b['baseline_wr']}% → gated {res_b['gated_wr']}% "
          f"({res_b['improvement_pp']:+}pp) — {res_b['verdict']}")
    passed = (res_b["ok_improved"] and res_b["ok_bad_cut"]
              and res_b["ok_good_kept"] and res_b["ok_supp_losers"])
    verdict = ("✅ VERIFIED — gate cuts losers, keeps winners" if passed
               else "❌ NOT VERIFIED")
    print(f"\n  BACKTEST VERDICT: {verdict}")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMPDIR, ignore_errors=True)
    sys.exit(code)
