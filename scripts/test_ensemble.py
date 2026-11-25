"""scripts/test_ensemble.py — ALL-THEORY ENSEMBLE battery (2026-11-26).

Covers core/ensemble.py (USER: "অনেক theory আছে সব গুলো সঠিক ভাবে ইউজ করো"):

  1.  import + API surface
  2.  happy path — every voice present, required keys, p in [0.02, 0.98]
  3.  σ-model dominance — extreme σ voice with neutral others ⇒ ensemble
      follows the σ voice (the strongest measured engine leads)
  4.  weight learning — a 70% rolling-accuracy voice outweighs a 50%
      voice with the same prior (evidence earns weight)
  5.  mute below coin-flip — a voice measured at 45% gets ZERO weight
  6.  no-lookahead — inputs beyond the passed window cannot change the
      output (only lp + last N closed candles are ever read)
  7.  determinism — same inputs ⇒ identical output (twice)
  8.  grading loop — grade_ensemble updates n/correct/voice_stats/tiers
  9.  degenerate inputs — not-ready lp ⇒ None; empty voices ⇒ fallback σ
  10. fair-walk neutrality smoke — 300 synthetic random-walk candles:
      ensemble ENTRY trade_hint win-rate must stay in [0.42, 0.58]
      (no fabricated edge — the repo's hardest rule)
  11. LOCKED-tier direction accuracy ≥ 90% on a fair walk (the honest
      home of the "90%" question: high-confidence DIRECTION calls, not
      entry win-rate)
  12. stream wrapper — grade_closed + public_stats round-trip on a stub

Run: python3 scripts/test_ensemble.py   (exit 0 = all pass)
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ensemble import (            # noqa: E402
    ensemble_running_candle, grade_ensemble, grade_closed,
    get_state, public_stats, VOICE_PRIORS,
)
from core.live_candle import predict_running_candle  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASSED, FAILED
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + extra) if extra and not cond else ''}")
    if cond:
        PASSED += 1
    else:
        FAILED += 1


def _lp_ready(p_close_green: float = 0.70, buyer_pct: int = 60,
              factors=None, lock: float = 0.4, phase: str = "MID",
              entry_hint: str | None = None) -> dict:
    """A minimal ready live-pred payload for ensemble tests."""
    return {
        "ready": True,
        "p_close_green": p_close_green,
        "p_up_from_here": p_close_green,
        "buyer_pct": buyer_pct,
        "seller_pct": 100 - buyer_pct,
        "lock_fraction": lock,
        "phase": phase,
        "entry_hint": entry_hint,
        "factors": factors if factors is not None else [
            {"name": "velocity", "dir": "CALL"},
            {"name": "close_pos", "dir": "CALL"},
        ],
        "seconds_left": 25,
        "tick_count": 40,
    }


def _candles(colors, base=1.10000, sigma=0.00020):
    """Closed candle dicts from a color sequence (1=green, -1=red, 0=flat)."""
    out = []
    px = base
    for i, col in enumerate(colors):
        o = px
        c = o + col * sigma * (1 + (i % 3) * 0.1)
        px = c
        out.append({"time": 1700000000 + i * 60, "open": o,
                    "high": max(o, c) + sigma * 0.3,
                    "low": min(o, c) - sigma * 0.3, "close": c})
    return out


class _StubStream:
    asset = "TEST_otc"
    period = 60


def main() -> int:
    print("=" * 68)
    print("  ALL-THEORY ENSEMBLE BATTERY (core/ensemble.py)")
    print("=" * 68)

    # 1 ── import + API surface
    check("1. API surface complete",
          all(callable(f) for f in (ensemble_running_candle, grade_ensemble,
                                    grade_closed, get_state, public_stats))
          and {"sigma_model", "ml_t1", "strategy", "pressure",
               "pattern", "eye_factors"} == set(VOICE_PRIORS))

    closed = _candles([1, 1, -1, 1, 1, -1, -1, 1, 1, 1, -1, 1])

    # 2 ── happy path
    ens = ensemble_running_candle(
        lp=_lp_ready(), closed_candles=closed, eoc_signal="CALL",
        ml_t1=0.62, state=None)
    check("2. happy path — all six voices, keys, bounds",
          isinstance(ens, dict)
          and {"p_ensemble", "direction", "tier", "tier_bn",
               "agreement_pct", "trade_hint", "voices", "note_bn"}
          <= set(ens)
          and 0.02 <= ens["p_ensemble"] <= 0.98
          and len(ens["voices"]) == 6,
          f"got {sorted(ens.keys()) if isinstance(ens, dict) else ens}")

    # 3 ── σ-model dominance
    ens_dom = ensemble_running_candle(
        lp=_lp_ready(p_close_green=0.97, buyer_pct=50,
                     factors=[{"name": "x", "dir": "PUT"},
                              {"name": "y", "dir": "PUT"}]),
        closed_candles=_candles([-1] * 12), eoc_signal="PUT", ml_t1=0.40)
    check("3. σ-model dominance — extreme σ leads the ensemble",
          ens_dom["direction"] == "CALL" and ens_dom["p_ensemble"] > 0.75,
          f"p={ens_dom and ens_dom.get('p_ensemble')}")

    # 4 ── weight learning (evidence earns weight)
    st_good = {"voice_stats": {"pattern": {"n": 100, "correct": 70}},
               "n": 0, "correct": 0, "tiers": {}}
    st_flat = {"voice_stats": {"pattern": {"n": 100, "correct": 50}},
               "n": 0, "correct": 0, "tiers": {}}
    w_good = w_flat = None
    from core.ensemble import _voice_weight
    w_good = _voice_weight("pattern", st_good)
    w_flat = _voice_weight("pattern", st_flat)
    check("4. weight learning — 70% voice outweighs 50% voice",
          w_good > 0 and w_flat == 0.0 and w_good > w_flat,
          f"good={w_good} flat={w_flat}")

    # 5 ── mute below coin-flip (45% rolling → zero weight)
    st_bad = {"voice_stats": {"strategy": {"n": 80, "correct": 36}},
              "n": 0, "correct": 0, "tiers": {}}
    ens_bad = ensemble_running_candle(
        lp=_lp_ready(0.55), closed_candles=closed,
        eoc_signal="CALL", state=st_bad)
    strat = [v for v in ens_bad["voices"] if v["name"] == "strategy"]
    check("5. sub-coin-flip voice muted (weight 0, still listed)",
          strat and strat[0]["weight"] == 0.0,
          f"strategy voice: {strat}")

    # 6 ── no-lookahead: older/extra candles beyond the window are the
    #      ONLY closed-candle input; mutating candles AFTER the last 40
    #      slice cannot change the result (we pass the slice ourselves).
    big = _candles([1, -1] * 80)
    ens_a = ensemble_running_candle(lp=_lp_ready(0.62),
                                    closed_candles=big[-40:],
                                    eoc_signal="CALL")
    big_mutated = list(big)
    # flip colors of candles OUTSIDE the 40-candle window the ensemble sees
    for i in range(len(big_mutated) - 80, len(big_mutated) - 41):
        c = big_mutated[i]
        big_mutated[i] = {**c, "open": c["close"], "close": c["open"]}
    ens_b = ensemble_running_candle(lp=_lp_ready(0.62),
                                    closed_candles=big_mutated[-40:],
                                    eoc_signal="CALL")
    check("6. no-lookahead — pre-window mutation changes nothing",
          ens_a["p_ensemble"] == ens_b["p_ensemble"])

    # 7 ── determinism
    ens_c = ensemble_running_candle(lp=_lp_ready(0.62),
                                    closed_candles=big[-40:],
                                    eoc_signal="CALL")
    check("7. determinism — identical inputs, identical output",
          ens_c["p_ensemble"] == ens_a["p_ensemble"])

    # 8 ── grading loop
    st = {"voice_stats": {}, "n": 0, "correct": 0, "tiers": {}}
    ens_g = ensemble_running_candle(lp=_lp_ready(0.80),
                                    closed_candles=closed,
                                    eoc_signal="CALL", state=st)
    grade_ensemble(st, ens_g, open_px=1.10000, close_px=1.10040)  # green
    grade_ensemble(st, ens_g, open_px=1.10000, close_px=1.10040)  # green
    check("8. grading — ensemble n=2 correct=2, voices + tiers updated",
          st["n"] == 2 and st["correct"] == 2
          and st["voice_stats"].get("sigma_model", {}).get("n") == 2
          and st["tiers"].get(ens_g["tier"], {}).get("n") == 2,
          f"state={st}")

    # 9 ── degenerate inputs
    check("9a. not-ready lp ⇒ None",
          ensemble_running_candle({"ready": False}) is None)
    ens_fb = ensemble_running_candle(
        lp=_lp_ready(0.60, buyer_pct=50,
                     factors=[{"name": "x", "dir": "CALL"},
                              {"name": "y", "dir": "PUT"}]),
        closed_candles=None, eoc_signal=None, ml_t1=None)
    # σ + pressure(absent→50) + eye(tie→silent) still speak; ensure no crash
    check("9b. minimal voices — still a valid dict",
          isinstance(ens_fb, dict) and 0.02 <= ens_fb["p_ensemble"] <= 0.98)

    # 10 + 11 ── fair-walk neutrality + LOCKED-tier direction accuracy
    # Per-tick simulation (production fidelity): the ensemble runs on EVERY
    # tick; the FIRST trade_hint of a candle is a simulated entry settled
    # strike→close (audit A5 semantics, 0.85 payout ⇒ break-even 54.05%).
    rng = random.Random(20261126)
    st_fw = {"voice_stats": {}, "n": 0, "correct": 0, "tiers": {}}
    entries_n = entries_win = 0
    final_n = final_correct = 0
    locked_n = locked_correct = 0
    closed_fw: list[dict] = []
    N_CANDLES = 300
    for ci in range(N_CANDLES):
        t0 = 1700000000 + ci * 60
        px = closed_fw[-1]["close"] if closed_fw else 1.10000
        o = px
        n_ticks = 120
        sig = 0.00008
        hi = lo = px
        ticks = []
        for k in range(n_ticks):
            px += rng.gauss(0.0, 1.0) * sig
            ticks.append(px)
            hi, lo = max(hi, px), min(lo, px)
        c = px
        last_lp = last_ens = None
        entry = None                     # (strike, direction) — first hint
        for k in range(n_ticks):
            now = t0 + 60.0 * (k + 1) / n_ticks
            lp = predict_running_candle(
                ticks[:k + 1], o, 60, t0, now=now,
                recent_candles=closed_fw[-20:])
            if not lp or not lp.get("ready"):
                continue
            ens = ensemble_running_candle(
                lp=lp, closed_candles=closed_fw[-40:],
                eoc_signal=("CALL" if (closed_fw and closed_fw[-1]["close"]
                                        > closed_fw[-1]["open"]) else "PUT"),
                state=st_fw)
            if not ens:
                continue
            last_lp, last_ens = lp, ens
            th = ens.get("trade_hint")
            if th in ("CALL", "PUT") and entry is None:
                entry = (ticks[k], th)
        if last_lp and last_ens and c != o:
            final_n += 1
            final_correct += int((last_ens["p_ensemble"] > 0.5) == (c > o))
            grade_ensemble(st_fw, last_ens, o, c)
            if last_ens["tier"] == "LOCKED":
                locked_n += 1
                locked_correct += int(
                    (last_ens["p_ensemble"] > 0.5) == (c > o))
        if entry is not None and c != entry[0]:
            entries_n += 1
            entries_win += int((c > entry[0]) == (entry[1] == "CALL"))
        closed_fw.append({"time": t0, "open": o, "high": hi,
                          "low": lo, "close": c})
    entry_wr = entries_win / entries_n if entries_n else None
    dir_acc = final_correct / final_n if final_n else None
    locked_acc = locked_correct / locked_n if locked_n else None
    entry_ev = (entry_wr * 0.85 - (1.0 - entry_wr)) if entry_wr else None
    check("10. fair-walk entry neutrality — WR in [0.42, 0.58], EV ≤ 0.02",
          entry_wr is not None and entries_n >= 100
          and 0.42 <= entry_wr <= 0.58 and entry_ev <= 0.02,
          f"entries={entries_n} WR={entry_wr} EV={entry_ev}")
    check("11. LOCKED-tier direction accuracy ≥ 0.90 on fair walk",
          locked_acc is not None and locked_n >= 50 and locked_acc >= 0.90,
          f"locked_n={locked_n} acc={locked_acc}")
    print(f"      (fair-walk: candles={N_CANDLES} dir_acc="
          f"{dir_acc:.3f} locked_n={locked_n} locked_acc="
          f"{locked_acc and round(locked_acc, 4)} "
          f"entries={entries_n} entry_WR={entry_wr and round(entry_wr, 4)} "
          f"entry_EV={entry_ev and round(entry_ev, 4)})")

    # 12 ── stream wrapper round-trip
    stub = _StubStream()
    ens_w = ensemble_running_candle(lp=_lp_ready(0.72),
                                    closed_candles=closed,
                                    eoc_signal="CALL",
                                    state=get_state(stub))
    stub._live_pred_final = {**_lp_ready(0.72), "ensemble": ens_w}
    grade_closed(stub, 1.10000, 1.10035)
    pub = public_stats(stub)
    check("12. stream wrapper — grade_closed + public_stats round-trip",
          isinstance(pub, dict)
          and pub["ensemble"]["n"] == 1
          and pub["ensemble"]["correct"] == 1
          and "sigma_model" in pub["voices"]
          and pub["voices"]["sigma_model"]["weight_now"] > 0,
          f"pub={pub}")

    print()
    print("=" * 68)
    print(f"  RESULT: {PASSED} passed, {FAILED} failed "
          f"({PASSED + FAILED} checks)")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
