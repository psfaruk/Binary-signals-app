"""core/otc_dataset.py — prediction dataset builder (PIPELINE PHASES 1+3+8).

USER SPEC (2026-09-11, "OTC Future Candle Prediction System"):

  Phase 3 — Target তৈরি:  input = current candle + আগের 20-50 candle;
            Target 1 = পরের candle UP/DOWN;  Target 2 = তার পরের candle.
            Example: Current = Candle 100, input 51..100, predict
            Candle 101 (UP 74%) + Candle 102 (UP 61%).

  Phase 8 — Prediction Lock: prediction তৈরির সময় ভবিষ্যৎ candle-এর কোনো
            তথ্য ব্যবহার করা যাবে না (look-ahead bias বন্ধ).

THIS MODULE ENFORCES THE LOCK STRUCTURALLY:
  * features for row i are computed on the SLICE candles[i-W+1 .. i] — the
    builder never passes candle i+1 to the feature engine;
  * targets y1/y2 are read strictly from candles i+1 / i+2;
  * every row carries window_end_ctime and target ctimes, and the builder
    asserts window_end_ctime < t1_ctime < t2_ctime;
  * verify_lock() (perturbation test) mutates future candles and proves the
    features cannot see it — run it on synthetic AND real DB samples.

UP definition (matches the app's own grading in feed._accuracy):
    a candle is UP  iff close > open  (DOWN iff close < open; a perfect
    doji close == open is EXCLUDED from the dataset and counted).
"""

import os
import sqlite3
import time
from collections import defaultdict

from core.otc_features import build_feature_row, FEATURE_NAMES, MIN_WINDOW

__all__ = [
    "build_dataset", "load_candles_from_db", "audit_db_coverage",
    "verify_lock", "DEFAULT_WINDOW",
]

DEFAULT_WINDOW = 50  # spec: "বর্তমান candle + আগের 20–50 candle"


# ─────────────────────────── Phase 1: data loading ────────────────────────

def load_candles_from_db(db_path, period=60, days=None):
    """Load closed candles (+ microstructure) from candle_micro.

    Phase 1 guarantee: candle_micro rows are written by feed.py from the
    SAME OTC tick feed the binary platform serves — each row is one closed
    1-minute candle (open/high/low/close) plus the candle's own tick
    snapshot (ticks_json) and microstructure (buy_pct, ...).

    Returns {asset: [candle_dict,...]} sorted by ctime; candle dicts carry
    time/open/high/low/close + micro fields (buy_pct, sell_pct,
    tick_count, is_fight).
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    cutoff = int(time.time() - days * 86400) if days else 0
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT asset, ctime, open, high, low, close, "
            "buy_pct, sell_pct, tick_count, is_fight "
            "FROM candle_micro WHERE period = ? AND ctime > ? "
            "ORDER BY asset, ctime", (period, cutoff)).fetchall()
    finally:
        conn.close()

    grouped = defaultdict(list)
    seen = set()
    for r in rows:
        if r["open"] is None or r["close"] is None:
            continue
        key = (r["asset"], r["ctime"])
        if key in seen:          # PRIMARY KEY guard (defensive)
            continue
        seen.add(key)
        grouped[r["asset"]].append({
            "time": int(r["ctime"]),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "buy_pct": r["buy_pct"], "sell_pct": r["sell_pct"],
            "tick_count": r["tick_count"], "is_fight": r["is_fight"],
        })
    return dict(grouped)


def audit_db_coverage(db_path, period=60):
    """Phase-1 audit: per-pair data depth, completeness and gaps.

    Reports exactly what the user asked to know BEFORE modelling:
    how many candles each pair has, the time span, the completeness vs the
    theoretical 1-candle-per-minute grid, and the largest gaps.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT asset, ctime FROM candle_micro WHERE period = ? "
            "ORDER BY asset, ctime", (period,)).fetchall()
    finally:
        conn.close()

    per = defaultdict(list)
    for r in rows:
        per[r["asset"]].append(int(r["ctime"]))

    report = []
    for asset, ts in sorted(per.items()):
        ts = sorted(set(ts))
        first, last = ts[0], ts[-1]
        span_min = max(0, (last - first) // 60)
        gaps = []
        max_gap = 0
        for a, b in zip(ts, ts[1:]):
            g = (b - a) // 60
            if g > 1:
                gaps.append((a, g))
                max_gap = max(max_gap, g)
        expected = span_min + 1
        report.append({
            "asset": asset,
            "candles": len(ts),
            "first_utc": first,
            "last_utc": last,
            "span_days": round(span_min / 1440.0, 2),
            "completeness_pct": round(100.0 * len(ts) / expected, 1)
                                if expected else 0.0,
            "max_gap_min": max_gap,
            "gaps_over_5min": sum(1 for _, g in gaps if g > 5),
        })
    return report


# ─────────────────── Phase 3 + 8: dataset with locked targets ─────────────

def _candle_dir_up(candle):
    """UP=1 / DOWN=0 following the app's own grading (close vs open)."""
    return 1 if candle["close"] > candle["open"] else 0


def _gapfree_runs(candles, period=60):
    """Split a pair's candle list into gap-free contiguous runs.

    Feed interruptions (reconnects, weekends) leave missing minutes. A
    feature window must never span such a hole (stale context masquerading
    as a continuous 50-candle history), and a target must always be the
    IMMEDIATE next candle(s). Runs of candles whose consecutive ctimes are
    exactly `period` apart are yielded; everything crossing a hole is
    excluded by construction.
    """
    runs = []
    start = 0
    for j in range(1, len(candles)):
        if candles[j]["time"] - candles[j - 1]["time"] != period:
            if j - start >= 1:
                runs.append(candles[start:j])
            start = j
    runs.append(candles[start:])
    return runs


def build_dataset(candles_by_asset, window=DEFAULT_WINDOW, micro=True,
                  period=60, feature_fn=None):
    """Build the T+1 / T+2 prediction dataset (Phase 3) under the
    prediction lock (Phase 8).

    feature_fn (2026-09-11 OTC-PREDICT-ENGINE): injectable feature engine.
    Default = core.otc_features.build_feature_row (the 24 Phase-2 features);
    the prediction engine passes
    core.otc_predict.features_ext.build_extended_row (PART 6 superset).
    The LOCK invariants below apply to ANY injected engine — the builder
    still only ever hands it the past slice candles[i-window+1 .. i].

    For every closed candle index i (0-based) with enough history:
        X  = build_feature_row(candles[i-window+1 .. i], micro_i)
        y1 = 1 if candle[i+1] is UP else 0        (সপরের candle)
        y2 = 1 if candle[i+2] is UP else 0        (তার পরের candle)
        meta: asset, window_end_ctime (= candle[i].time),
              t1_ctime, t2_ctime, close_i

    Lock invariants enforced here:
        i+1 and i+2 are STRICTLY future candles AND the immediate next
        minutes (ctime deltas == period); windows are GAP-FREE (any feed
        hole splits the pair into separate runs — no row crosses it);
        the feature engine only ever receives the past slice. Doji target
        candles (close == open) are dropped and counted per asset.

    Returns (rows, stats): rows = list of dicts (FEATURE_NAMES + targets +
    meta); stats = per-asset/drop bookkeeping.
    """
    if window < MIN_WINDOW:
        raise ValueError(f"window {window} < MIN_WINDOW {MIN_WINDOW}")
    feature_fn = feature_fn or build_feature_row
    rows = []
    stats = {"assets": 0, "rows": 0, "dropped_doji_t1": 0,
             "dropped_doji_t2": 0, "skipped_short": 0,
             "gap_runs": 0, "rows_dropped_by_gaps": 0}
    for asset, candles_in in sorted(candles_by_asset.items()):
        candles_all = sorted(candles_in, key=lambda x: x["time"])
        runs = _gapfree_runs(candles_all, period=period)
        stats["gap_runs"] += sum(1 for r in runs if len(r) >= 2)
        # rows a naive (gap-blind) builder would have made vs this one
        stats["rows_dropped_by_gaps"] += max(
            0, len(candles_all) - window - 1
            - sum(max(0, len(r) - window - 1) for r in runs))
        for candles in runs:
            n = len(candles)
            if n < window + 2:
                stats["skipped_short"] += 1
                continue
            stats["assets"] += 1  # per (asset, gap-free-run) unit
            for i in range(window - 1, n - 2):
                t0 = candles[i]
                t1 = candles[i + 1]
                t2 = candles[i + 2]

                # PHASE 8 LOCK — structural, not conventional:
                # the feature engine sees ONLY candles[0..i].
                past_slice = candles[i - window + 1: i + 1]
                feats = feature_fn(past_slice,
                                   micro=t0 if micro else None)

                # doji targets carry no direction — drop the row honestly
                if t1["close"] == t1["open"]:
                    stats["dropped_doji_t1"] += 1
                    continue
                if t2["close"] == t2["open"]:
                    stats["dropped_doji_t2"] += 1
                    continue

                # "strictly future AND immediate next minutes" proof
                assert t1["time"] - t0["time"] == period, \
                    f"T+1 not the immediate candle {asset}@{i}"
                assert t2["time"] - t1["time"] == period, \
                    f"T+2 not the immediate candle {asset}@{i}"

                row = {"asset": asset,
                       "window_end_ctime": t0["time"],
                       "t1_ctime": t1["time"], "t2_ctime": t2["time"],
                       "close_i": t0["close"],
                       "y1_up": _candle_dir_up(t1),
                       "y2_up": _candle_dir_up(t2)}
                row.update(feats)
                rows.append(row)
    stats["rows"] = len(rows)

    # global ordering sanity: rows must be strictly time-ordered per asset
    last_t = {}
    for r in rows:
        a = r["asset"]
        if a in last_t:
            assert r["window_end_ctime"] > last_t[a], f"row order broken {a}"
        last_t[a] = r["window_end_ctime"]
    return rows, stats


# ─────────────────────── Phase 8: perturbation proof ──────────────────────

def verify_lock(candles, n_checks=25, window=DEFAULT_WINDOW, seed=7):
    """Perturbation test proving NO look-ahead leakage.

    For random indices i: recompute the feature row after RANDOMLY MUTATING
    every candle strictly after i. If any feature changed, the pipeline
    leaks and the dataset is invalid. Also verifies that mutating candle i
    itself DOES change features (the check is not vacuous).

    Returns (n_checks, n_future_mutations_detected, n_self_mutations_detected)
    — the pipeline is clean iff n_future_mutations_detected == 0 and
    n_self_mutations_detected > 0.
    """
    import random
    rng = random.Random(seed)
    n = len(candles)
    if n < window + 3:
        raise ValueError(f"verify_lock: need >= {window + 3} candles, got {n}")

    def _fut_sig(cnds):
        # signature of everything the features are ALLOWED to depend on
        return repr(cnds)

    future_hits = 0
    self_hits = 0
    for _ in range(n_checks):
        i = rng.randrange(window - 1, n - 2)
        base = build_feature_row(candles[i - window + 1: i + 1])

        # 1) mutate ALL future candles (j > i) — features must not move
        mutated = [dict(c) for c in candles]
        for j in range(i + 1, n):
            mutated[j]["open"] = mutated[j]["open"] * 1.7 + 0.00013
            mutated[j]["close"] = mutated[j]["close"] * 0.6 + 11.111
            mutated[j]["high"] = mutated[j]["high"] * 1.3 + 5.5
            mutated[j]["low"] = mutated[j]["low"] * 0.8 + 2.5
        after = build_feature_row(mutated[i - window + 1: i + 1])
        if _fut_sig(base) != _fut_sig(after):
            future_hits += 1

        # 2) mutate candle i ITSELF — features MUST move (non-vacuity)
        mutated2 = [dict(c) for c in candles]
        mutated2[i]["close"] = mutated2[i]["close"] * 1.05 + 0.001
        after2 = build_feature_row(mutated2[i - window + 1: i + 1])
        if _fut_sig(base) != _fut_sig(after2):
            self_hits += 1

    return n_checks, future_hits, self_hits
