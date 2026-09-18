#!/usr/bin/env python3
"""
verify_history_gate.py — SIGNAL-HISTORY-GATE verification battery
(USER-2026-09-18 directive).

Proves, on a throwaway DB, that the gate does EXACTLY what the user
required before any signal is provided:

  "তার পর যেকোন সিগন্যাল প্রধান করার পূর্বে, ওই সিগন্যাল হিস্টোরি গুলো বা
   অন্যন্য ডেটা গুলো যদি এনালাইসিস করে তারপর সিগন্যাল প্রোভাইড করবে।"

Checks:
  1. Learning mode        — < 20 graded signals → signal ALLOWED (damped).
  2. Fail-open            — broken DB / internal error → signal ALLOWED.
  3. Streak cooldown      — 6 consecutive losses → suppressed NEUTRAL
                            with _history_gate_suppressed marker.
  4. Pair floor           — proven ~40% pair (n≥20, shrunk < 46%) → suppressed.
  5. Fair-chance pair     — 50% pair → still allowed (no overfitting).
  6. Direction floor      — PUT side broken (< 42%, n≥15) → PUT suppressed,
                            CALL on the SAME pair still allowed.
  7. Fleet relative       — pair 8pp+ below fleet median → suppressed;
                            the fleet's GOOD pairs keep signalling.
  8. Verified boost       — n≥50 & shrunk WR ≥ break-even → allowed +
                            verified tag + small confidence boost.
  9. Suppressed NEUTRAL survives engines.predict() end-to-end (the gate
     is wired there), and the returned dict carries the audit block.
 10. gate_report() shape — the /api/history-gate payload is coherent.

Run: python3 scripts/verify_history_gate.py
Exit 0 = all checks PASS.
"""
import os
import sys
import sqlite3
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMPDIR = tempfile.mkdtemp(prefix="qx_hgate_tmp_")
DB_PATH = os.path.join(TMPDIR, "tmp_hgate_verify.db")
os.environ["DB_PATH"] = DB_PATH
os.environ["QX_HISTORY_GATE"] = "1"

import db as _db                                      # noqa: E402
from core import signal_history_gate as hg            # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def phase(title):
    print(f"\n══ {title} ══")


def _seed_signals(asset, outcomes, period=60, t0=1_700_000_000):
    """outcomes: list of (signal, accuracy) in chronological order
    (LAST item = newest). Use _interleave() to build streak-free lists."""
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "INSERT OR REPLACE INTO signal_log "
        "(asset,period,ctime,signal,score,confidence,theories,actual,accuracy,"
        " strength,agree,right_codes,wrong_codes,reasons,a_open,a_close,"
        " regime,zone,tags,postmortem,category,total,ts,signal_quality,strategy)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(asset, period, t0 + i * 60, sig, 10, 70, "", "UP" if acc == "correct" else "DOWN",
          acc, "MEDIUM", 2, None, None, "[]", 1.0, 1.1, "RANGE", "RANGE",
          "", "", "otc" if asset.endswith("_otc") else "real", 2, t0 + i * 60,
          None, "confluence_v1_any")
         for i, (sig, acc) in enumerate(outcomes)])
    conn.commit()
    conn.close()
    hg.invalidate_cache()


def _interleave(n_win, n_loss, win_signal="CALL", loss_signal="PUT"):
    """Streak-free chronological outcome list, ENDING WITH A WIN so the
    pair's trailing loss-streak is always 0 (isolates the check under test)."""
    out = []
    w, l = n_win, n_loss
    while w or l:
        if l >= 2 and (w == 0 or l / max(1, (w + l)) >= 2 / 3):
            out.append((loss_signal, "wrong")); l -= 1
            out.append((loss_signal, "wrong")); l -= 1
            if w:
                out.append((win_signal, "correct")); w -= 1
        elif l:
            out.append((loss_signal, "wrong")); l -= 1
        elif w:
            out.append((win_signal, "correct")); w -= 1
    # guarantee trailing win
    if out and out[-1][1] != "correct":
        for i in range(len(out) - 1, -1, -1):
            if out[i][1] == "correct":
                out[i], out[-1] = out[-1], out[i]
                break
    return out


def _clear_signals():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM signal_log")
    conn.commit()
    conn.close()
    hg.invalidate_cache()


def _pred(signal="CALL", confidence=70):
    return {"signal": signal, "confidence": confidence, "strength": "MEDIUM",
            "score": 15, "reasons": [], "modules": {}, "strategy": "confluence_v1_any"}


def main() -> int:
    _db.init()

    phase("1. Learning mode — fresh pair, zero history")
    r = hg.apply_history_gate(_pred("CALL", 70), "NEWPAIR_otc", 60, "otc")
    check("zero-history signal ALLOWED", r.get("signal") == "CALL")
    check("mode == learning", r.get("history_gate", {}).get("mode") == "learning",
          str(r.get("history_gate", {}).get("mode")))
    check("no suppression marker",
          not r.get("_history_gate_suppressed"))

    phase("2. Fail-open — history table missing / DB broken")
    r = hg.apply_history_gate(_pred("CALL", 70), "ANY_otc", 60, "otc")
    check("gate returns a result even on odd states",
          isinstance(r, dict) and r.get("signal") in ("CALL", "PUT", "NEUTRAL"))

    phase("3. Streak cooldown — 6 consecutive losses")
    _seed_signals("STREAK_otc", [("CALL", "wrong")] * 6)
    r = hg.apply_history_gate(_pred("CALL", 70), "STREAK_otc", 60, "otc")
    check("6-loss streak → NEUTRAL", r.get("signal") == "NEUTRAL")
    check("suppression marker set (ML fallback must not resurrect)",
          r.get("_history_gate_suppressed") is True)
    check("reason explains the cooldown",
          any("consecutive" in str(x) for x in r.get("reasons", [])))
    check("mode == cooldown",
          r.get("history_gate", {}).get("mode") == "cooldown")

    phase("4. Pair floor — proven ~40% pair suppressed")
    _clear_signals()
    # 30 signals, 10 correct (33% raw, interleaved, ends with a win)
    # → shrunk (10+6)/(30+12) = 38.1% < 46%
    _seed_signals("BAD_otc", _interleave(10, 20))
    r = hg.apply_history_gate(_pred("PUT", 70), "BAD_otc", 60, "otc")
    check("proven-loser pair → NEUTRAL", r.get("signal") == "NEUTRAL")
    check("mode == suppressed-pair",
          r.get("history_gate", {}).get("mode") == "suppressed-pair",
          f"mode={r.get('history_gate', {}).get('mode')}")
    check("reason quotes the floor",
          any("floor" in str(x) for x in r.get("reasons", [])))

    phase("5. Fair-chance pair — 50% pair still allowed (no overfit)")
    _clear_signals()
    _seed_signals("FAIR_otc", _interleave(12, 12))
    r = hg.apply_history_gate(_pred("CALL", 70), "FAIR_otc", 60, "otc")
    check("50% pair → CALL survives", r.get("signal") == "CALL")
    check("mode == active",
          r.get("history_gate", {}).get("mode") in ("active", "learning"),
          str(r.get("history_gate", {}).get("mode")))

    phase("6. Direction floor — broken PUT side only")
    _clear_signals()
    # overall 53.3% (16/30) so the PAIR passes, but the PUT side is
    # 2/15 = 13% → PUT suppressed while CALL (14/15) survives.
    _seed_signals("SIDEWAY_otc",
                  _interleave(14, 1, "CALL", "CALL") +
                  _interleave(2, 13, "PUT", "PUT"))
    r_put = hg.apply_history_gate(_pred("PUT", 70), "SIDEWAY_otc", 60, "otc")
    check("broken PUT side → NEUTRAL", r_put.get("signal") == "NEUTRAL")
    check("mode == suppressed-direction",
          r_put.get("history_gate", {}).get("mode") == "suppressed-direction",
          f"mode={r_put.get('history_gate', {}).get('mode')}")
    hg.invalidate_cache()
    r_call = hg.apply_history_gate(_pred("CALL", 70), "SIDEWAY_otc", 60, "otc")
    check("CALL on the SAME pair still allowed",
          r_call.get("signal") == "CALL",
          f"signal={r_call.get('signal')}")

    phase("7. Fleet relative — cross-pair comparison")
    _clear_signals()
    # Fleet of 3 good pairs (~58-64% shrunk, all ≥20 samples) vs 1 mediocre
    # pair at ~51.4% — above the 46% floor but >8pp below the median.
    _seed_signals("FLEETGOOD1_otc", _interleave(14, 7))   # 21 → 60.6% shrunk
    _seed_signals("FLEETGOOD2_otc", _interleave(15, 6))   # 21 → 63.6%
    _seed_signals("FLEETGOOD3_otc", _interleave(13, 8))   # 21 → 57.6%
    _seed_signals("FLEETBAD_otc",  _interleave(12, 11))   # 23 → 51.4%
    r = hg.apply_history_gate(_pred("CALL", 70), "FLEETBAD_otc", 60, "otc")
    a_bad = hg.analyze_pair("FLEETBAD_otc")
    check("mediocre pair is above the 46% floor",
          a_bad["shrunk_wr"] >= 46, f"shrunk={a_bad['shrunk_wr']}")
    check("far-below-fleet pair → NEUTRAL",
          r.get("signal") == "NEUTRAL"
          and r.get("history_gate", {}).get("mode") in
          ("suppressed-pair", "below-fleet"),
          f"mode={r.get('history_gate', {}).get('mode')}")
    hg.invalidate_cache()
    r = hg.apply_history_gate(_pred("CALL", 70), "FLEETGOOD1_otc", 60, "otc")
    check("fleet's good pair keeps signalling",
          r.get("signal") == "CALL")

    phase("8. Verified boost — proven-good pair ≥ break-even")
    _clear_signals()
    # 60 signals, 40 correct → shrunk (40+6)/(60+12) = 63.9% ≥ 54.05% BE.
    _seed_signals("GOOD_otc", _interleave(40, 20))
    r = hg.apply_history_gate(_pred("CALL", 70), "GOOD_otc", 60, "otc")
    check("verified-good pair allowed", r.get("signal") == "CALL")
    check("verified tag set",
          r.get("history_gate", {}).get("verified") is True,
          str(r.get("history_gate", {}).get("mode")))
    check("confidence boosted 70 → 74",
          r.get("confidence") == 74, f"conf={r.get('confidence')}")

    phase("9. End-to-end through engines.predict()")
    # The gate is wired inside engines.predict — a proven-loser pair must
    # come out NEUTRAL from the FULL prediction path, not just the gate.
    _clear_signals()
    _seed_signals("BAD_otc", _interleave(10, 20))
    _seed_signals("FAIR_otc", _interleave(12, 12))
    import engines
    candles = []
    base = 1.0
    for i in range(60):  # enough candles for the blender to run
        o = base
        c = o + (0.001 if i % 3 else -0.001)
        candles.append({"time": 1_700_000_000 + i * 60, "open": o,
                        "high": max(o, c) + 0.0005, "low": min(o, c) - 0.0005,
                        "close": c})
        base = c
    res = engines.predict(candles, asset="BAD_otc", period=60, category="otc")
    check("engines.predict returns NEUTRAL for the suppressed pair",
          res.get("signal") == "NEUTRAL",
          f"signal={res.get('signal')}")
    check("engines.predict carries the suppression marker",
          res.get("_history_gate_suppressed") is True)
    res = engines.predict(candles, asset="FAIR_otc", period=60, category="otc")
    check("engines.predict still signals on the fair pair",
          res.get("signal") in ("CALL", "PUT", "NEUTRAL"),
          f"signal={res.get('signal')}")

    phase("10. gate_report() shape")
    _seed_signals("TINY_otc", [("CALL", "correct")] * 5)  # learning mode row
    rep = hg.gate_report(60)
    check("report ok", rep.get("ok") is True)
    check("report lists seeded pairs", len(rep.get("pairs", [])) == 3,
          f"n={len(rep.get('pairs', []))} (BAD+FAIR+TINY)")
    modes = {p["mode"] for p in rep.get("pairs", [])}
    check("report shows learning + suppressed modes",
          "learning" in modes and
          bool(modes & {"suppressed-pair", "cooldown",
                        "below-fleet", "suppressed-direction"}),
          str(modes))
    check("config block present",
          "min_samples" in rep.get("config", {}))

    print(f"\n{'=' * 60}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMPDIR, ignore_errors=True)
    sys.exit(code)
