"""core/otc_predict/guard.py — LIVE EDGE CIRCUIT BREAKER (EDGE-GUARD 2026-09-13).

USER COMPLAINT (verbatim): "কিন্তু প্রেডিকশন ক্যান্ডেল ভুল হয়, মানে
ডিরেকশন wrong দেখানো হয়। লস বেশি হচ্ছে, উইন কম হচ্ছে।"

ROOT CAUSE (measured, not guessed — scripts/otc_predictor_backtest_report.json,
14 days of REAL candle_micro data, 12 pairs, ~139k rows):
    y1 ML accuracy = 49.92%, logloss 0.6943 (> ln 2 — worse than coin-flip)
    y2 ML accuracy ≈ 50.2%
    calibration bucket 0.55–0.65: predicted 56.4% → actual UP 48.6%
With an 85% binary payout the break-even win rate is 54.05% — a coin-flip
signal generator is a GUARANTEED slow loss. Yet the old emit path kept
signalling because:
    * provisional models (failed the VERIFIED bar) still emitted;
    * the 100-point score's non-ML components (60 pts) could lift a
      weak-probability signal into the GOOD tier;
    * NOTHING watched the LIVE win rate — a pair bleeding losses kept
      emitting forever.

THIS MODULE is the missing PART 29 enforcement, in live time:

    After every settlement the guard recomputes, per (asset, horizon),
    the EMITTED-signal win rate over a rolling window and its Wilson 95%
    lower bound. Emission stays allowed only while

        wilson_lb(wins, n) >= BREAK_EVEN - TOL

    (TOL keeps a borderline-but-honest model alive instead of flapping).
    Below the minimum sample the pair is in "learning" state — allowed but
    flagged, because PART 20's 100→500→1000 ladder starts at zero too.

    Suspension is PER (asset, horizon): a broken T+2 does not silence a
    healthy T+1, and a broken pair does not silence its neighbours.
    Auto-resume: the window keeps filling as (tracked, non-emitted)
    predictions settle — wait, no: settled EMITTED rows are what count, and
    a suspended pair stops emitting... so the window would freeze. Instead
    the guard uses ALL settled predictions of the model's DIRECTION
    (emit or not — the direction was frozen either way) for the resume
    test, while the SUSPEND test uses emitted rows only. A model whose
    frozen directions keep hitting will re-earn emission rights; one that
    keeps missing stays silent. Honest in both directions.

DESIGN NOTES
    * Reads the FROZEN otc_predictions table only — the same rows PART 16
      locked; the guard can never be fooled by a recomputed history.
    * Cached per (asset, horizon) with a short TTL; settle_target
      invalidates instantly so the very next candle close sees the new
      loss. Reads never raise into the predictor path (fail-open with a
      logged reason — a broken guard must not kill the feed).
    * Payout is env-tunable (QX_PAYOUT, default 0.85): break-even is
      1 / (1 + payout). 0.85 → 54.05%, exactly the number the repo's own
      backtest reports state.
"""

import math
import os
import threading
import time

__all__ = ["emission_allowed", "invalidate", "guard_status",
           "break_even_pct", "reset_for_tests"]

# ── config (env-tunable, honest defaults) ─────────────────────────────────
_PAYOUT = float(os.environ.get("QX_PAYOUT", "0.85"))
_BREAK_EVEN = 1.0 / (1.0 + _PAYOUT)            # 0.85 → 0.5405
_TOL_PCT = float(os.environ.get("QX_GUARD_TOL_PP", "1.0"))  # keep-alive band
_MIN_SAMPLE = int(os.environ.get("QX_GUARD_MIN_N", "40"))   # learning floor
_WINDOW = int(os.environ.get("QX_GUARD_WINDOW", "120"))     # rolling emitted rows
_TTL = float(os.environ.get("QX_GUARD_TTL", "45"))

# soft floor for the RESUME test: the frozen directions must at least beat
# coin-flip by this margin before a suspended pair emits again.
_RESUME_MARGIN = float(os.environ.get("QX_GUARD_RESUME_MARGIN_PP", "1.5"))

_cache = {}                      # (asset, horizon) → {"verdict", "at", ...}
_lock = threading.Lock()


def break_even_pct():
    """Payout break-even win rate in percent (e.g. 54.05 at 0.85)."""
    return round(100.0 * _BREAK_EVEN, 2)


def reset_for_tests():
    """Drop all cached verdicts (test isolation)."""
    with _lock:
        _cache.clear()


def invalidate(asset=None, horizon=None):
    """Drop cached verdict(s) — called right after a settlement batch so
    the next candle close decides with fresh numbers."""
    with _lock:
        if asset is None:
            _cache.clear()
            return
        for key in [k for k in _cache
                    if k[0] == asset and (horizon is None or k[1] == horizon)]:
            _cache.pop(key, None)


def _wilson_lb(wins, n, z=1.96):
    """Wilson 95% lower bound for a binomial proportion (fraction, 0..1)."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def _load_window(asset, horizon, limit):
    """(wins, losses, draws, n) over the newest `limit` EMITTED settled rows
    + (wins, n) over ALL settled frozen directions (the resume sample)."""
    try:
        from db import _cursor
    except Exception:
        return None
    try:
        with _cursor() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN win_loss='win'  THEN 1 ELSE 0 END) AS w,
                          SUM(CASE WHEN win_loss='loss' THEN 1 ELSE 0 END) AS l,
                          SUM(CASE WHEN win_loss='draw' THEN 1 ELSE 0 END) AS d
                   FROM (SELECT win_loss FROM otc_predictions
                         WHERE asset=? AND period=60 AND horizon=? AND emit=1
                           AND settled_at IS NOT NULL
                         ORDER BY settled_at DESC LIMIT ?)""",
                (asset, int(horizon), int(limit))).fetchone()
            n_e = int(row["n"] or 0)
            w_e = int(row["w"] or 0)
            l_e = int(row["l"] or 0)
            d_e = int(row["d"] or 0)
            row2 = c.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN win_loss='win'  THEN 1 ELSE 0 END) AS w,
                          SUM(CASE WHEN win_loss='loss' THEN 1 ELSE 0 END) AS l
                   FROM (SELECT win_loss FROM otc_predictions
                         WHERE asset=? AND period=60 AND horizon=?
                           AND settled_at IS NOT NULL
                         ORDER BY settled_at DESC LIMIT ?)""",
                (asset, int(horizon), max(limit, 400))).fetchone()
            n_a = int(row2["n"] or 0)
            w_a = int(row2["w"] or 0)
            l_a = int(row2["l"] or 0)
        return {"emit": (w_e, l_e, d_e, n_e),
                "all": (w_a, l_a, n_a)}
    except Exception:
        return None


def _verdict(asset, horizon):
    """Decide RIGHT NOW: (allowed, state, reason, stats). Never raises."""
    win = _load_window(asset, horizon, _WINDOW)
    if win is None:
        # DB unreadable — fail-open but say so (the feed outranks the guard)
        return True, "db_unreadable", "guard: DB পড়া গেল না (fail-open)", None
    w_e, l_e, d_e, n_e = win["emit"]
    w_a, l_a, n_a = win["all"]

    if n_e < _MIN_SAMPLE:
        return True, "learning", (
            f"লাইভ নমুনা কম ({n_e}/{_MIN_SAMPLE}) — শেখা চলছে"), {
            "emit_n": n_e, "emit_wins": w_e, "emit_losses": l_e,
            "all_n": n_a, "all_wins": w_a}

    wr = (w_e / n_e)
    lb = _wilson_lb(w_e, n_e)
    floor = _BREAK_EVEN - _TOL_PCT / 100.0
    if lb >= floor:
        return True, "ok", None, {
            "emit_n": n_e, "emit_wins": w_e, "emit_losses": l_e,
            "emit_wr_pct": round(100 * wr, 2),
            "wilson_lb_pct": round(100 * lb, 2),
            "floor_pct": round(100 * floor, 2), "all_n": n_a,
            "all_wins": w_a}

    # suspended — but compute what it takes to come back: the frozen
    # DIRECTIONS (tracked rows count too) must beat coin-flip + margin
    if n_a >= _MIN_SAMPLE:
        wr_a = w_a / n_a
        if wr_a >= 0.5 + _RESUME_MARGIN / 100.0:
            return False, "suspended_soft", (
                f"emit জিৎ-হার {round(100 * wr, 1)}% < {break_even_pct()}% "
                f"break-even — সিগন্যাল বন্ধ, ডিরেকশন ট্র্যাকিং চলছে "
                f"({round(100 * wr_a, 1)}%)"), {
                "emit_n": n_e, "emit_wins": w_e, "emit_losses": l_e,
                "emit_wr_pct": round(100 * wr, 2),
                "wilson_lb_pct": round(100 * lb, 2),
                "floor_pct": round(100 * floor, 2),
                "all_n": n_a, "all_wins": w_a,
                "all_wr_pct": round(100 * wr_a, 2)}
    return False, "suspended", (
        f"লাইভ জিৎ-হার {round(100 * wr, 1)}% — break-even "
        f"{break_even_pct()}% এর নিচে, Wilson LB {round(100 * lb, 1)}% "
        f"→ এই পেয়ারের সিগন্যাল বন্ধ"), {
        "emit_n": n_e, "emit_wins": w_e, "emit_losses": l_e,
        "emit_wr_pct": round(100 * wr, 2),
        "wilson_lb_pct": round(100 * lb, 2),
        "floor_pct": round(100 * floor, 2), "all_n": n_a,
        "all_wins": w_a}


def emission_allowed(asset, horizon, period=60):
    """May THIS (asset, horizon) emit tradeable signals right now?

    Returns (allowed: bool, state: str, reason: str|None, stats: dict|None).
    state ∈ {"ok", "learning", "suspended", "suspended_soft",
             "db_unreadable"}.
    """
    if os.environ.get("QX_GUARD", "1") in ("0", "false", "no"):
        return True, "disabled", None, None
    key = (asset, int(horizon))
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached["at"] < _TTL:
            return cached["verdict"]
    verdict = _verdict(asset, horizon)
    with _lock:
        _cache[key] = {"verdict": verdict, "at": now}
    return verdict


def guard_status(assets=None, horizons=(1, 2)):
    """Snapshot for the UI / API: every watched pair's guard state."""
    out = {"break_even_pct": break_even_pct(),
           "payout": _PAYOUT, "min_sample": _MIN_SAMPLE,
           "window": _WINDOW, "per_pair": {}}
    for a in (assets or []):
        for h in horizons:
            allowed, state, reason, stats = emission_allowed(a, h)
            out["per_pair"][f"{a}|t{h}"] = {
                "allowed": allowed, "state": state, "reason": reason,
                "stats": stats}
    return out
