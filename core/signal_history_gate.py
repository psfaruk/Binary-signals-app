"""
core/signal_history_gate.py — SIGNAL-HISTORY-GATE (USER-2026-09-18).

USER REQUIREMENT (verbatim, 2026-09-18 — supersedes the 2026-09-09/14
"every candle must signal on every pair" mode):
  "তার পর যেকোন সিগন্যাল প্রধান করার পূর্বে, ওই সিগন্যাল হিস্টোরি গুলো বা
   অন্যন্য ডেটা গুলো যদি এনালাইসিস করে তারপর সিগন্যাল প্রোভাইড করবে।
   এক পেয়ার এর ডেটা অন্য পেয়ার এর সাথে মিলিয়ে দেখবে। কোন পেয়ার এ সঠিক
   সিগন্যাল বেশি দিচ্ছে, সেই গুলো ভালোভাবে যাচাই করে সিগন্যাল প্রধান করবে।"
  = Before providing ANY signal, analyse that pair's signal history and the
    other data first. Match one pair's data against the other pairs
    (cross-pair). Verify carefully WHICH pairs give more correct signals
    — and provide signals on those verified pairs.

WHY THIS WAS NEEDED (measured on live production data, 2026-09-18):
  * Overall live WR 50.9% (n=106) — below the 54.05% OTC break-even.
  * `confluence_v1_any` (any-one-theory fallback) alone: 50.5% (n=101) —
    statistically a coin flip; the any_theory mode pins WR at ~51%.
  * Worst pairs were actively signalling all day: NZDUSD_otc 16.7%,
    USDPHP_otc / BRLUSD_otc / USDZAR_otc 33.3%.
  * Direction-specific failures (e.g. NZDUSD_otc PUT 0% n=4) were never
    filtered because no gate ever looked at per-direction history.
  * The old breakeven/pair-health gates needed ≥50 samples that the old
    30-min retention could never accumulate. With the 2026-09-18 retention
    fix (200 signal rows per pair ≈ 3.3 h of history), this gate finally
    has the data it needs.

WHAT THIS GATE DOES (all env-tunable, ALL fail-open — an internal error
never blocks a signal):
  1. LEARNING MODE    — pair has < QX_HG_MIN_SAMPLES (20) graded signals:
                        allow, small confidence damp. Never judge a pair
                        on a handful of candles.
  2. STREAK COOLDOWN  — QX_HG_STREAK_LIMIT (6) consecutive recent losses
                        → suppress (NEUTRAL). Catches intraday regime
                        changes / broker algorithm flips fast. Persisted in
                        signal_log, so it survives restarts (unlike the
                        in-memory stream._consecutive_losses).
  3. PAIR FLOOR       — Bayesian-shrunk pair WR < QX_HG_PAIR_MIN_WR (46%)
                        with n ≥ 20 → suppress the PAIR. Shrinkage
                        (Jeffreys prior, k=12) keeps tiny samples near 50%
                        so early bad luck can't nuke a good pair, while a
                        proven 40%-true-WR pair trips it within ~30-40
                        signals.
  4. DIRECTION FLOOR  — candidate direction (CALL or PUT) with
                        dir_n ≥ QX_HG_DIR_MIN_SAMPLES (15) and dir_wr <
                        QX_HG_DIR_MIN_WR (42%) → suppress THAT direction
                        (the opposite direction may still be emitted).
  5. FLEET RELATIVE   — cross-pair comparison ("এক পেয়ার এর ডেটা অন্য
                        পেয়ার এর সাথে মিলিয়ে দেখবে"): when ≥3 sibling
                        pairs have ≥20 graded signals each, a pair whose
                        shrunk WR is more than QX_HG_FLEET_DROP_PP (8pp)
                        below the fleet median is suppressed — capital
                        flows to the verified-best pairs.
  6. CURRENCY FLOW    — cross-pair consensus: among OTHER pairs sharing a
                        base/quote currency (e.g. all USD-quoted exotics),
                        what the recent graded outcomes imply about that
                        currency's direction. If ≥ QX_HG_FLOW_MIN_CONSENSUS
                        (67%) of ≥ QX_HG_FLOW_MIN_N (8) sibling outcomes
                        point one way and the candidate signal OPPOSES the
                        flow while the pair's own shrunk WR < 50%, apply a
                        confidence penalty (QX_HG_FLOW_PENALTY ×0.85) and
                        tag the reason — soft, never a suppression.
  7. VERIFIED BOOST   — pair with n ≥ QX_HG_VERIFY_N (50) and shrunk WR ≥
                        its payout break-even is tagged "verified" and gets
                        a small confidence bonus (QX_HG_GOOD_BOOST ×1.05)
                        — the "কোন পেয়ার এ সঠিক সিগন্যাল বেশি দিচ্ছে"
                        list the user asked the engine to prefer.

PUBLIC API:
  apply_history_gate(result, asset, period, category) -> result'
      Returns the prediction dict with either the SAME direction (with
      confidence adjustments + a result["history_gate"] audit block) or a
      SUPPRESSED NEUTRAL (result["_history_gate_suppressed"] = True) when
      the ledger measured this pair/direction anti-predictive. FIX
      (FADE-REMOVAL-2026-09-19): the interim FADE policy (invert
      CALL↔PUT + history_faded marker) was removed — inversion at these
      sample sizes is gambler's fallacy and it fed a fade-feedback loop
      in the ledger. A measured-bad pair honestly carries NO signal;
      feed.py bypasses the ML/fade-default for suppressed candles.
  gate_report() -> full per-pair report for /api/history-gate.

DESIGN NOTES:
  * Reads signal_log ONLY (graded correct/wrong rows) — the same ledger
    every winrate endpoint uses. No new tables.
  * Runs inside engines.predict() (already on a worker thread via
    feed._analyze_core's asyncio.to_thread) so no DB I/O ever blocks the
    feed loop.
  * Fail-open everywhere: any exception → signal allowed unchanged.
  * Env master switch QX_HISTORY_GATE (default "1" — ON per the
    2026-09-18 directive; set "0" to restore unconditional every-candle).
  * Suppressed candles are NOT graded (NEUTRAL is never graded) — the
    ledger only learns from signals the system actually stood behind.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

# ── Configuration (all env-overridable, repo convention) ────────────────────
ENABLED = os.environ.get("QX_HISTORY_GATE", "1") == "1"

MIN_SAMPLES = int(os.environ.get("QX_HG_MIN_SAMPLES", "20"))          # learning mode below this
STREAK_LIMIT = int(os.environ.get("QX_HG_STREAK_LIMIT", "6"))        # consecutive losses → cooldown
PAIR_MIN_WR = float(os.environ.get("QX_HG_PAIR_MIN_WR", "46"))       # shrunk WR floor (%)
DIR_MIN_SAMPLES = int(os.environ.get("QX_HG_DIR_MIN_SAMPLES", "15"))
DIR_MIN_WR = float(os.environ.get("QX_HG_DIR_MIN_WR", "42"))         # direction floor (%)
FLEET_MIN_SIBLINGS = int(os.environ.get("QX_HG_FLEET_MIN_SIBLINGS", "3"))
FLEET_MIN_SAMPLES = int(os.environ.get("QX_HG_FLEET_MIN_SAMPLES", "20"))
FLEET_DROP_PP = float(os.environ.get("QX_HG_FLEET_DROP_PP", "8"))    # pp below fleet median
FLOW_MIN_CONSENSUS = float(os.environ.get("QX_HG_FLOW_MIN_CONSENSUS", "0.67"))
FLOW_MIN_N = int(os.environ.get("QX_HG_FLOW_MIN_N", "8"))
FLOW_PENALTY = float(os.environ.get("QX_HG_FLOW_PENALTY", "0.85"))
VERIFY_N = int(os.environ.get("QX_HG_VERIFY_N", "50"))
GOOD_BOOST = float(os.environ.get("QX_HG_GOOD_BOOST", "1.05"))
LEARNING_DAMP = float(os.environ.get("QX_HG_LEARNING_DAMP", "0.97"))
LOOKBACK_N = int(os.environ.get("QX_HG_LOOKBACK_N", "200"))          # == retention rows per pair
SHRINKAGE_K = float(os.environ.get("QX_HG_SHRINKAGE_K", "12"))       # Jeffreys prior strength

# FADE-REMOVAL (2026-09-19): the interim EVERY-CANDLE FADE policy constants
# (FADE_CONF_PENALTY / FADE_CONF_CAP) were removed with the fade itself —
# inversion at these sample sizes is gambler's fallacy and it fed a
# fade-feedback loop in the ledger. Suppression carries no confidence to
# penalize; FLEET_PENALTY below remains for the soft cross-pair penalty.
FLEET_PENALTY = float(os.environ.get("QX_HG_FLEET_PENALTY", "0.85"))

# Cache TTL — the gate runs once per candle per pair (~1/min), the report
# endpoint a bit more often; 45 s keeps DB load trivial without serving
# stale decisions across candle boundaries.
_CACHE_TTL = float(os.environ.get("QX_HG_CACHE_TTL", "45"))
_cache: Dict[Tuple[str, int], Tuple[float, dict]] = {}
_cache_lock = threading.Lock()


# ── Small statistics helpers ──────────────────────────────────────────────────
def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> Optional[float]:
    """95% Wilson score-interval lower bound as a % (None if n==0)."""
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(100.0 * max(0.0, (centre - margin) / denom), 2)


def shrunk_win_rate(wins: int, n: int, k: float = None) -> float:
    """Bayesian-shrunk win rate (%) toward a 50% coin-flip prior.

    Jeffreys-style: (wins + k/2) / (n + k). With k=12 a pair must show a
    REAL deficit to fall through the 46% floor: e.g. n=30,w=10 (33% raw)
    → 41.7% shrunk (suppressed), while n=20,w=8 (40% raw) → 46.9%
    (allowed — genuinely inconclusive small sample).
    """
    if k is None:
        k = SHRINKAGE_K
    if n <= 0:
        return 50.0
    return round(100.0 * (wins + k / 2.0) / (n + k), 2)


def _category_for_asset(asset: str) -> str:
    return "otc" if (asset or "").lower().endswith("otc") else "real"


def _breakeven_for_asset(asset: str) -> float:
    """Payout break-even WR, same formula as core/breakeven.py."""
    if _category_for_asset(asset) == "otc":
        payout = int(os.environ.get("QX_PAYOUT_FLOOR_OTC",
                                    os.environ.get("QX_PAYOUT_FLOOR", "85")))
    else:
        payout = int(os.environ.get("QX_PAYOUT_FLOOR_REAL", "70"))
    return round(10000.0 / (100.0 + float(payout)), 2)


# ── DB access ────────────────────────────────────────────────────────────────
def _db_path() -> str:
    # Consistent with the rest of the codebase: db.DB_PATH honours the
    # DB_PATH env (backtests override it). Never a hardcoded /app path.
    try:
        import db as _db
        return _db.DB_PATH
    except Exception:
        return os.environ.get("DB_PATH", "signals.db")


def _pair_history_rows(asset: str, period: int, n: int) -> List[dict]:
    """Newest `n` graded signal_log rows for (asset, period), newest first."""
    conn = sqlite3.connect(_db_path(), timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT signal, accuracy FROM signal_log
               WHERE asset = ? AND period = ?
                 AND signal IN ('CALL', 'PUT')
                 AND accuracy IN ('correct', 'wrong')
               ORDER BY ctime DESC, id DESC
               LIMIT ?""",
            (asset, period, n)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _fleet_rows(period: int) -> List[dict]:
    """Per-pair aggregate over the newest LOOKBACK_N rows of EVERY pair —
    the cross-pair comparison base (single GROUP BY, bounded by retention)."""
    conn = sqlite3.connect(_db_path(), timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT asset,
                      COUNT(*)                        AS n,
                      SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN signal='CALL' THEN 1 ELSE 0 END)      AS call_n,
                      SUM(CASE WHEN signal='CALL' AND accuracy='correct' THEN 1 ELSE 0 END) AS call_wins,
                      SUM(CASE WHEN signal='PUT'  THEN 1 ELSE 0 END)      AS put_n,
                      SUM(CASE WHEN signal='PUT'  AND accuracy='correct' THEN 1 ELSE 0 END) AS put_wins
               FROM (
                   SELECT asset, signal, accuracy,
                          ROW_NUMBER() OVER (
                              PARTITION BY asset
                              ORDER BY ctime DESC, id DESC) AS rn
                   FROM signal_log
                   WHERE period = ? AND signal IN ('CALL','PUT')
                     AND accuracy IN ('correct','wrong'))
               WHERE rn <= ?
               GROUP BY asset""",
            (period, LOOKBACK_N)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        # Older SQLite without window functions → plain aggregate (the
        # retention cap already bounds the table, so this is still cheap).
        rows = conn.execute(
            """SELECT asset,
                      COUNT(*)                        AS n,
                      SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN signal='CALL' THEN 1 ELSE 0 END)      AS call_n,
                      SUM(CASE WHEN signal='CALL' AND accuracy='correct' THEN 1 ELSE 0 END) AS call_wins,
                      SUM(CASE WHEN signal='PUT'  THEN 1 ELSE 0 END)      AS put_n,
                      SUM(CASE WHEN signal='PUT'  AND accuracy='correct' THEN 1 ELSE 0 END) AS put_wins
               FROM signal_log
               WHERE period = ? AND signal IN ('CALL','PUT')
                 AND accuracy IN ('correct','wrong')
               GROUP BY asset""",
            (period,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _currency_flow_rows(asset: str, period: int, n: int = 60) -> List[dict]:
    """Recent graded rows of pairs sharing a base/quote currency with
    `asset` — used to infer the shared currency's direction (cross-pair
    consensus). Excludes the pair itself to avoid self-confirmation."""
    base, quote = _split_currencies(asset)
    conn = sqlite3.connect(_db_path(), timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT asset, signal, accuracy FROM signal_log
               WHERE period = ? AND asset != ?
                 AND signal IN ('CALL','PUT')
                 AND accuracy IN ('correct','wrong')
               ORDER BY ctime DESC, id DESC
               LIMIT 400""",
            (period, asset)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        b, q = _split_currencies(r["asset"])
        if base in (b, q) or quote in (b, q):
            out.append(dict(r))
        if len(out) >= n:
            break
    return out


# ── Currency parsing ─────────────────────────────────────────────────────────
_CCY_CACHE: Dict[str, Tuple[str, str]] = {}


def _split_currencies(asset: str) -> Tuple[str, str]:
    """'USDZAR_otc' → ('USD','ZAR'); 'EURUSD' → ('EUR','USD').

    OTC exotics on Quotex are quoted as they read: USDZAR = USD base.
    The tiny hardcoded map covers the non-USD-first oddballs (BRLUSD,
    NZDUSD, AUDUSD, EURUSD, GBPUSD, NZDCAD…). Anything unknown degrades to
    a no-op (no shared currency → no flow vote), never an error.
    """
    a = (asset or "").upper()
    if a.endswith("_OTC"):
        a = a[:-4]
    if a in _CCY_CACHE:
        return _CCY_CACHE[a]
    known = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF",
             "ZAR", "MXN", "INR", "IDR", "PKR", "BDT", "COP", "DZD",
             "PHP", "ARS", "NGN", "BRL")
    pair = None
    for i in (3,):
        if len(a) >= 6:
            left, right = a[:i], a[i:i + 3]
            if left in known and right in known:
                pair = (left, right)
    _CCY_CACHE[a] = pair or ("", "")
    return _CCY_CACHE[a]


def _signal_implies_ccy_up(asset: str, signal: str, ccy: str) -> Optional[bool]:
    """Does this signal (if correct) imply `ccy` strengthening?"""
    base, quote = _split_currencies(asset)
    if ccy not in (base, quote) or not base or not quote:
        return None
    base_up = signal == "CALL"
    return base_up if ccy == base else (not base_up)


# ── Analysis core ────────────────────────────────────────────────────────────
def analyze_pair(asset: str, period: int = 60) -> dict:
    """Full history analysis for one pair (cached, thread-safe).

    Returns: {n, wins, wr, shrunk_wr, wilson_lb, streak_losses,
              call: {n, wins, wr}, put: {n, wins, wr},
              mode, verdict, reasons[], stats_source}
    """
    now = time.time()
    key = (asset, period)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]

    rows = _pair_history_rows(asset, period, LOOKBACK_N)
    n = len(rows)
    wins = sum(1 for r in rows if r["accuracy"] == "correct")
    call_rows = [r for r in rows if r["signal"] == "CALL"]
    put_rows = [r for r in rows if r["signal"] == "PUT"]

    streak_losses = 0
    for r in rows:  # rows are newest-first
        if r["accuracy"] == "wrong":
            streak_losses += 1
        else:
            break

    wr = round(100.0 * wins / n, 2) if n else 0.0
    shrunk = shrunk_win_rate(wins, n)

    analysis = {
        "asset": asset,
        "period": period,
        "n": n,
        "wins": wins,
        "wr": wr,
        "shrunk_wr": shrunk,
        "wilson_lb": wilson_lower_bound(wins, n),
        "streak_losses": streak_losses,
        "call": {
            "n": len(call_rows),
            "wins": sum(1 for r in call_rows if r["accuracy"] == "correct"),
        },
        "put": {
            "n": len(put_rows),
            "wins": sum(1 for r in put_rows if r["accuracy"] == "correct"),
        },
        "breakeven": _breakeven_for_asset(asset),
    }
    analysis["call"]["wr"] = (round(100.0 * analysis["call"]["wins"] /
                                    analysis["call"]["n"], 2)
                              if analysis["call"]["n"] else 0.0)
    analysis["put"]["wr"] = (round(100.0 * analysis["put"]["wins"] /
                                   analysis["put"]["n"], 2)
                             if analysis["put"]["n"] else 0.0)

    with _cache_lock:
        _cache[key] = (now, analysis)
    return analysis


def _fleet_stats(period: int = 60) -> dict:
    """Cross-pair aggregate: {asset: {n, wr, shrunk_wr, ...}} + fleet median."""
    rows = _fleet_rows(period)
    fleet = {}
    for r in rows:
        n = int(r["n"] or 0)
        wins = int(r["wins"] or 0)
        fleet[r["asset"]] = {
            "n": n,
            "wins": wins,
            "wr": round(100.0 * wins / n, 2) if n else 0.0,
            "shrunk_wr": shrunk_win_rate(wins, n),
        }
    judged = [v["shrunk_wr"] for v in fleet.values()
              if v["n"] >= FLEET_MIN_SAMPLES]
    fleet_median = sorted(judged)[len(judged) // 2] if len(judged) >= 3 else None
    return {"pairs": fleet, "median_shrunk_wr": fleet_median,
            "judged_pairs": len(judged)}


def _currency_flow(asset: str, period: int = 60) -> dict:
    """Cross-pair currency consensus for the pair's two currencies."""
    base, quote = _split_currencies(asset)
    out = {"base": base, "quote": quote, "evidence_n": 0,
           "consensus": None}
    if not base or not quote:
        return out
    rows = _currency_flow_rows(asset, period)
    # Which currency does the flow speak about? Use the one that appears
    # in more sibling pairs (usually USD for the exotics).
    base_count = sum(1 for r in rows
                     if base in _split_currencies(r["asset"]))
    quote_count = sum(1 for r in rows
                      if quote in _split_currencies(r["asset"]))
    ccy = base if base_count >= quote_count else quote
    up_votes = 0
    total = 0
    for r in rows:
        implies = _signal_implies_ccy_up(r["asset"], r["signal"], ccy)
        if implies is None:
            continue
        # A signal that graded CORRECT confirms its implied direction; a
        # WRONG signal implies the opposite actually happened.
        actually_up = implies if r["accuracy"] == "correct" else (not implies)
        up_votes += 1 if actually_up else 0
        total += 1
    out["ccy"] = ccy
    out["evidence_n"] = total
    out["up_share"] = round(up_votes / total, 3) if total else None
    if total >= FLOW_MIN_N:
        out["consensus"] = ("UP" if out["up_share"] >= FLOW_MIN_CONSENSUS
                            else "DOWN" if out["up_share"] <= 1 - FLOW_MIN_CONSENSUS
                            else "MIXED")
    return out


# ── The gate ─────────────────────────────────────────────────────────────────
def apply_history_gate(result: dict, asset: str, period: int = 60,
                       category: str = None) -> dict:
    """USER-2026-09-18: analyse signal history + cross-pair data BEFORE
    the signal is provided. Mutates `result` (confidence / NEUTRAL) and
    attaches result["history_gate"] with the full audit trail.

    Fail-open: ANY internal error leaves the signal untouched.
    """
    if not ENABLED:
        return result
    signal = result.get("signal")
    if signal not in ("CALL", "PUT"):
        return result  # NEUTRAL / gates already decided elsewhere

    try:
        audit: Dict = {"checked": True}
        reasons: List[str] = []
        a = analyze_pair(asset, period)

        audit["pair"] = {
            "n": a["n"], "wr": a["wr"], "shrunk_wr": a["shrunk_wr"],
            "streak_losses": a["streak_losses"],
            "call_wr": a["call"]["wr"], "call_n": a["call"]["n"],
            "put_wr": a["put"]["wr"], "put_n": a["put"]["n"],
        }

        # 1 ── STREAK: 6 straight losses is intraday regime-change evidence
        #     even on a brand-new pair (n = 6). FIX (FADE-REMOVAL-2026-09-19):
        #     this used to INVERT the direction (CALL→PUT) — gambler's
        #     fallacy. A 6-loss streak says "this pair is currently
        #     unpredictable", NOT "the opposite direction wins": at ~50%
        #     base rate the flipped coin loses just as often, and every
        #     faded outcome re-entered the ledger feeding more fades (a
        #     feedback loop). The honest action is the documented one —
        #     SUPPRESS (NEUTRAL, no signal this candle).
        suppress_reason = None
        suppress_mode = None
        if a["streak_losses"] >= STREAK_LIMIT:
            suppress_reason = (f"{a['streak_losses']} consecutive losses "
                               f"(limit {STREAK_LIMIT}) — pair is currently "
                               f"unpredictable; no signal is the honest call")
            suppress_mode = "cooldown"

        # 2 ── LEARNING MODE: never judge a pair's QUALITY on < MIN_SAMPLES
        #     signals (the streak suppression above is regime evidence, not
        #     quality evidence).
        if not suppress_reason and a["n"] < MIN_SAMPLES:
            audit["mode"] = "learning"
            audit["verdict"] = "allow"
            audit["note"] = (f"{a['n']}/{MIN_SAMPLES} graded signals — "
                             f"learning, no judgement yet")
            if a["n"] > 0:
                result["confidence"] = max(1, int(round(
                    (result.get("confidence") or 0) * LEARNING_DAMP)))
            result["history_gate"] = audit
            return result

        # 3 ── PAIR FLOOR: shrunk WR below the hard floor → the PAIR is
        #     measured bad. Suppress (documented behavior; the fade variant
        #     assumed the inverse edge which the sample size cannot support).
        if not suppress_reason and a["shrunk_wr"] < PAIR_MIN_WR:
            suppress_reason = (f"shrunk WR {a['shrunk_wr']:.1f}% < "
                               f"{PAIR_MIN_WR:.0f}% floor over last {a['n']} "
                               f"signals (raw {a['wr']:.1f}%) — pair is "
                               f"measured unprofitable")
            suppress_mode = "suppressed-pair"

        # 4 ── DIRECTION FLOOR: this specific CALL/PUT side is broken.
        d = a[signal.lower()]
        if not suppress_reason and d["n"] >= DIR_MIN_SAMPLES:
            d_shrunk = shrunk_win_rate(d["wins"], d["n"])
            if d_shrunk < DIR_MIN_WR:
                suppress_reason = (f"{signal} side shrunk WR {d_shrunk:.1f}% < "
                                   f"{DIR_MIN_WR:.0f}% floor over {d['n']} "
                                   f"{signal} signals (raw {d['wr']:.1f}%) — "
                                   f"this direction is measured broken")
                suppress_mode = "suppressed-direction"
            else:
                audit["direction_shrunk_wr"] = d_shrunk

        # 4b ── FLEET RELATIVE (suppress variant, USER-2026-09-18 "capital
        #     flows to the verified-best pairs"): when enough siblings are
        #     judged, a pair far below the fleet median is suppressed — not
        #     merely penalized. Checked after the pair/direction floors so
        #     the more specific reason wins when both apply.
        if not suppress_reason:
            try:
                fleet = _fleet_stats(period)
                if (fleet["median_shrunk_wr"] is not None
                        and fleet["judged_pairs"] >= FLEET_MIN_SIBLINGS
                        and a["shrunk_wr"] <
                            fleet["median_shrunk_wr"] - FLEET_DROP_PP):
                    suppress_reason = (
                        f"shrunk WR {a['shrunk_wr']:.1f}% is "
                        f"{fleet['median_shrunk_wr'] - a['shrunk_wr']:.1f}pp "
                        f"below the fleet median "
                        f"{fleet['median_shrunk_wr']:.1f}% across "
                        f"{fleet['judged_pairs']} verified pairs — capital "
                        f"flows to the verified-best pairs")
                    suppress_mode = "below-fleet"
                    audit["fleet_median_shrunk_wr"] = fleet["median_shrunk_wr"]
            except Exception as _fleet_exc:
                audit["fleet_error"] = f"{type(_fleet_exc).__name__}: {_fleet_exc}"

        if suppress_reason:
            # SUPPRESS, never invert: the published direction stays whatever
            # the theory voted — we simply refuse to publish it. feed.py
            # reads the marker and does NOT resurrect the candle via the ML
            # model or the fade-default (a suppressed pair gets NO signal,
            # which is exactly what "no reliable signal" should look like).
            reasons.append(
                f"[HISTORY-GATE] {asset}: SUPPRESS {signal} — "
                f"{suppress_reason} (confidence "
                f"{result.get('confidence') or 0}→0, no signal this candle)")
            audit["mode"] = suppress_mode
            audit["verdict"] = "suppress"
            audit["suppressed_direction"] = signal
            audit["reasons"] = reasons
            result["signal"] = "NEUTRAL"
            result["confidence"] = 0
            result["raw_confidence"] = 0
            result["strength"] = "NEUTRAL"
            result["_history_gate_suppressed"] = True
            result.setdefault("reasons", []).extend(reasons)
            result["history_gate"] = audit
            return result

        # 5 ── FLEET RELATIVE audit trail: far-below-fleet pairs were already
        #     suppressed in 4b above; here we only record the fleet median on
        #     the audit so /api/history-gate can show the cross-pair context
        #     ("এক পেয়ার এর ডেটা অন্য পেয়ার এর সাথে মিলিয়ে দেখবে") for
        #     allowed pairs too. The old confidence-penalty branch is gone —
        #     the laggard case never reaches this line anymore.
        try:
            fleet = _fleet_stats(period)
            audit["fleet_median_shrunk_wr"] = fleet["median_shrunk_wr"]
        except Exception as _fleet_exc:
            audit["fleet_error"] = f"{type(_fleet_exc).__name__}: {_fleet_exc}"

        # 6 ── CURRENCY FLOW: soft cross-pair consensus penalty.
        try:
            flow = _currency_flow(asset, period)
            audit["currency_flow"] = {
                "ccy": flow.get("ccy"), "n": flow["evidence_n"],
                "consensus": flow["consensus"],
                "up_share": flow.get("up_share")}
            if flow["consensus"] in ("UP", "DOWN"):
                implies_up = _signal_implies_ccy_up(
                    asset, signal, flow.get("ccy", ""))
                if implies_up is not None:
                    opposes = (flow["consensus"] == "UP") != implies_up
                    if opposes and a["shrunk_wr"] < 50.0:
                        _orig = result.get("confidence") or 0
                        result["confidence"] = max(1, int(round(
                            _orig * FLOW_PENALTY)))
                        result.setdefault("reasons", []).append(
                            f"[HISTORY-GATE] {asset}: opposes cross-pair "
                            f"{flow.get('ccy')} flow ({flow['consensus']}, "
                            f"{flow['evidence_n']} sibling outcomes, "
                            f"{round((flow.get('up_share') or 0) * 100)}% "
                            f"consensus) while own WR "
                            f"{a['shrunk_wr']:.1f}% — confidence "
                            f"{_orig} → {result['confidence']}")
                        audit["flow_penalty"] = FLOW_PENALTY
        except Exception as _flow_exc:
            audit["flow_error"] = f"{type(_flow_exc).__name__}: {_flow_exc}"

        # 7 ── VERIFIED BOOST: proven pairs get the user's preference.
        if a["n"] >= VERIFY_N and a["shrunk_wr"] >= a["breakeven"]:
            audit["mode"] = "verified-good"
            _orig = result.get("confidence") or 0
            result["confidence"] = min(99, int(round(_orig * GOOD_BOOST)))
            audit["verified"] = True
            result.setdefault("reasons", []).append(
                f"[HISTORY-GATE] {asset} VERIFIED: shrunk WR "
                f"{a['shrunk_wr']:.1f}% ≥ break-even {a['breakeven']:.1f}% "
                f"over {a['n']} signals — verified-good pair")
        else:
            audit["mode"] = "active"
            audit["verified"] = False

        audit["verdict"] = "allow"
        result["history_gate"] = audit
        return result

    except Exception as exc:
        # FAIL-OPEN: any internal error leaves the signal untouched.
        try:
            result.setdefault("reasons", []).append(
                f"[HISTORY-GATE] check failed (fail-open): "
                f"{type(exc).__name__}: {exc}")
            result["history_gate"] = {"checked": False,
                                      "error": f"{type(exc).__name__}: {exc}",
                                      "verdict": "allow"}
        except Exception:
            pass
        return result


# ── Reporting (for /api/history-gate) ────────────────────────────────────────
def gate_report(period: int = 60) -> dict:
    """Per-pair verification snapshot — the 'কোন পেয়ার এ সঠিক সিগন্যাল বেশি
    দিচ্ছে' list, live from signal_log."""
    try:
        fleet = _fleet_stats(period)
        pairs = []
        for asset, f in sorted(fleet["pairs"].items(),
                               key=lambda kv: -kv[1]["shrunk_wr"]):
            a = analyze_pair(asset, period)
            be = a["breakeven"]
            if a["streak_losses"] >= STREAK_LIMIT:
                mode = "cooldown"
            elif a["n"] < MIN_SAMPLES:
                mode = "learning"
            elif a["shrunk_wr"] < PAIR_MIN_WR:
                mode = "suppressed-pair"
            elif (fleet["median_shrunk_wr"] is not None
                    and a["shrunk_wr"] <
                        fleet["median_shrunk_wr"] - FLEET_DROP_PP):
                mode = "below-fleet"
            elif a["n"] >= VERIFY_N and a["shrunk_wr"] >= be:
                mode = "verified-good"
            else:
                mode = "active"
            pairs.append({
                "asset": asset,
                "category": _category_for_asset(asset),
                "n": a["n"], "wins": a["wins"],
                "wr": a["wr"], "shrunk_wr": a["shrunk_wr"],
                "wilson_lb": a["wilson_lb"],
                "breakeven": be,
                "streak_losses": a["streak_losses"],
                "call": a["call"], "put": a["put"],
                "mode": mode,
                "signal_now": (mode not in ("cooldown", "suppressed-pair",
                                            "below-fleet")),
            })
        return {
            "ok": True,
            "generated_at": time.time(),
            "period": period,
            "config": {
                "enabled": ENABLED,
                "min_samples": MIN_SAMPLES,
                "streak_limit": STREAK_LIMIT,
                "pair_min_wr": PAIR_MIN_WR,
                "dir_min_samples": DIR_MIN_SAMPLES,
                "dir_min_wr": DIR_MIN_WR,
                "fleet_min_siblings": FLEET_MIN_SIBLINGS,
                "fleet_drop_pp": FLEET_DROP_PP,
                "flow_min_consensus": FLOW_MIN_CONSENSUS,
                "verify_n": VERIFY_N,
                "lookback_n": LOOKBACK_N,
            },
            "fleet": {
                "median_shrunk_wr": fleet["median_shrunk_wr"],
                "judged_pairs": fleet["judged_pairs"],
                "total_pairs": len(fleet["pairs"]),
            },
            "pairs": pairs,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def invalidate_cache(asset: str = None):
    """Clear the analysis cache (called after grades / for tests)."""
    with _cache_lock:
        if asset is None:
            _cache.clear()
        else:
            for key in [k for k in _cache if k[0] == asset]:
                _cache.pop(key, None)
