"""core/otc_predict/hist_stats.py — Historical Setup-Match Engine.

USER SPEC (Deep Report 2026-09-13 §13 + §26 — "সবচেয়ে শক্তিশালী অংশ"):

    বর্তমান setup (শেষ candles + trend + momentum + structure + S/R
    distance + wick/body) — database-এর আগের সব candle-এর মধ্যে একই
    ধরনের setup খুঁজে তার পরের candle কতবার UP হয়েছে সেটা থেকে:

        similar setups = 8,200
        next candle UP = 5,986   →   P(UP) ≈ 73%

    এটাই statistical future-candle prediction — Version 2 (Historical
    Probability) of the report's roadmap; the ML models (Version 3) and
    the ensemble UI (Version 4) consume it as an independent voice.

DESIGN — how "একই ধরনের setup" is defined:

    The current market state is collapsed into a coarse SIGNATURE tuple
    built ONLY from the base feature row (core.otc_features — the same
    numbers the dataset rows and the live predictor already compute, so
    training and live can never diverge):

        L0 (fine)    : trend_z, mom_z, hi20_pos, body, streak, sr, vol, dir_1
        L1 (medium)  : trend_z, mom_z, hi20_pos, streak, dir_1
        L2 (coarse)  : trend_z, mom_z

    Lookup walks L0 → L1 → L2 and stops at the first level with enough
    samples (hierarchical backoff — a rare exact state borrows the stats
    of its coarser neighbourhood instead of abstaining). With no level
    reaching the floor the engine ABSTAINS (honest None — Deep Report
    §35: "SKIP করা prediction system-এর দুর্বলতা নয়, risk control").

    p_up uses Jeffreys smoothing (up+0.5)/(n+1) — never 0% / 100% from a
    handful of samples.

LEAK-SAFETY (PART 19 protocol, Deep Report §17):

    * Training enrichment (enrich_rows): an outcome is added to the maps
      only AFTER a row whose prediction time (window_end_ctime) is >= the
      outcome candle's close time — time-deferred updates. Row i's y1
      (candle i+1) becomes visible at row i+1, y2 (candle i+2) at row
      i+2 — exactly the information a live trader would have had.
    * Live (live_lookup): the just-closed candle only ever resolves the
      signatures of the PREVIOUS two closes (its own T+1/T+2 targets from
      their point of view) — the lookup for "now" uses closed candles
      only, same as every other feature in this package.
    * Doji targets (open==close) are skipped — mirrors build_dataset's
      honest row-drop, so live maps and training maps stay identical.

INTEGRATION:

    * features_ext.build_unified_row(..., hist=<lookup>) merges
      hist_p_up_t1 / hist_p_up_t2 / hist_conf into the ML feature row —
      NEW bundles learn how much historical edge to trust per pair; OLD
      bundles ignore the extra keys (predict_up keys off the bundle's own
      feature_names) and live never KeyErrors because the neutral values
      are always present.
    * fast_train / train_otc_model call enrich_rows() right after
      build_dataset — no change to the locked dataset builder.
    * predictor freezes the per-horizon hist verdict into the payload
      (t1/t2 .hist) AND into the components JSON (survives reloads).
    * tracker.prediction_analytics gains Brier score + calibration
      buckets + payout break-even (Deep Report §21/§34).

This module never raises into the live feed: every public entry point
degrades honestly (neutral 0.5 / abstain) on any internal failure.
"""

import math
import os
import threading

__all__ = [
    "HIST_FEATURE_NAMES", "SIG_WINDOW", "MIN_N_L0", "MIN_N_L1", "MIN_N_L2",
    "signature_from_row", "enrich_rows", "live_lookup", "hist_feature_values",
    "reset_live", "hist_neutral",
]

# Feature names appended to UNIFIED_FEATURE_NAMES (features_ext).
HIST_FEATURE_NAMES = (
    "hist_p_up_t1",   # historical P(UP) for T+1 setups like this one
    "hist_p_up_t2",   # historical P(UP) for T+2 setups like this one
    "hist_conf",      # sample-size confidence in [0, 1] (0 = no match)
)

# Signature window — 50 candles, matching the live PRED_WINDOW and the
# training WINDOW so the feature values (and hence signatures) come from
# the same 50-candle slice everywhere.
SIG_WINDOW = 50

# Minimum samples per level before the level is trusted (L2 needs more
# because it is coarser — small-n there is noisier per definition).
MIN_N_L0 = 30
MIN_N_L1 = 30
MIN_N_L2 = 50

# Cold-start history: the live stream only holds ~400-500 closed candles
# (too few for the sample floors), so a cold build ALSO merges the pair's
# candle_micro history (bounded). Env-tunable, 0 disables the DB read.
_HIST_MAX_CANDLES = int(os.environ.get("QX_HIST_MAX_CANDLES", "3000"))
_HIST_USE_DB = os.environ.get("QX_HIST_DB", "1") not in ("0", "false", "no")

_CONF_CAP = 500.0          # n at/above this → hist_conf = 1.0
_levels = (("L0", MIN_N_L0), ("L1", MIN_N_L1), ("L2", MIN_N_L2))


def hist_neutral():
    """Neutral hist feature values (no historical knowledge)."""
    return {"hist_p_up_t1": 0.5, "hist_p_up_t2": 0.5, "hist_conf": 0.0}


def hist_feature_values(look):
    """Merge the engine's lookup dict into ML feature values.

    `look` — live_lookup() output {"p_up_t1", "n_t1", "level_t1",
    "p_up_t2", "n_t2", "level_t2"} or None. Neutral values when absent,
    so a bundle trained with hist features never KeyErrors live.
    """
    if not look:
        return hist_neutral()
    p1 = look.get("p_up_t1")
    p2 = look.get("p_up_t2")
    n = max(int(look.get("n_t1") or 0), int(look.get("n_t2") or 0))
    return {
        "hist_p_up_t1": float(p1) if p1 is not None else 0.5,
        "hist_p_up_t2": float(p2) if p2 is not None else 0.5,
        "hist_conf": _conf(n),
    }


def _conf(n):
    """Sample-size confidence: log-scaled, 1.0 at/above _CONF_CAP."""
    if n <= 0:
        return 0.0
    return round(min(1.0, math.log(1.0 + n) / math.log(1.0 + _CONF_CAP)), 4)


# ─────────────────────────── signature ────────────────────────────────

def _zbin(x, lo=0.5, hi=1.5):
    """Signed z-score → 5 bins (-2..2)."""
    if x <= -hi:
        return -2
    if x < -lo:
        return -1
    if x <= lo:
        return 0
    if x < hi:
        return 1
    return 2


def _pos_bin(p):
    """Position inside the 20-candle range → 5 bins (0..4)."""
    if p < 0.20:
        return 0
    if p < 0.40:
        return 1
    if p < 0.60:
        return 2
    if p < 0.80:
        return 3
    return 4


def _sr_bin(d):
    """Distance to the NEARER of support/resistance (ATR units) → 3 bins:
    0 = at a level, 1 = approaching, 2 = mid-range."""
    if d < 0.75:
        return 0
    if d < 2.0:
        return 1
    return 2


def _vol_bin(r):
    """vol_10/vol_20 → 3 bins: contraction / normal / expansion."""
    if r < 0.80:
        return 0
    if r < 1.25:
        return 1
    return 2


def _body_bin(b):
    if b < 0.30:
        return 0
    if b < 0.70:
        return 1
    return 2


def signature_from_row(f):
    """The 3-level signature dict {"L0": tuple, "L1": tuple, "L2": tuple}.

    `f` — any dict carrying the BASE feature names (a dataset row, or the
    live build_feature_row / build_unified_row output). Missing keys
    degrade to neutral bins (never raises).
    """
    eps = 1e-12
    vol20 = float(f.get("vol_20") or 0.0)
    if vol20 <= eps:
        vol20 = 1e-4          # flat window — ratios fall into bin 0
    trend_z = float(f.get("mom_10") or 0.0) / (vol20 * math.sqrt(10.0) + eps)
    mom_z = float(f.get("mom_5") or 0.0) / (vol20 * math.sqrt(5.0) + eps)
    trend_b = _zbin(trend_z)
    mom_b = _zbin(mom_z)
    pos_b = _pos_bin(float(f.get("hi20_pos") if f.get("hi20_pos") is not None
                           else 0.5))
    body_b = _body_bin(float(f.get("body_range_ratio") or 0.0))
    try:
        streak_b = int(max(-3, min(3, round(float(f.get("streak") or 0.0)))))
    except (TypeError, ValueError):
        streak_b = 0
    sr_b = _sr_bin(min(
        float(f.get("dist_support_atr") if f.get("dist_support_atr")
              is not None else 9.0),
        float(f.get("dist_resistance_atr") if f.get("dist_resistance_atr")
              is not None else 9.0)))
    vol_b = _vol_bin(float(f.get("vol_10") or 0.0) / (vol20 + eps))
    try:
        dir_b = int(f.get("dir_1") or 0)
    except (TypeError, ValueError):
        dir_b = 0
    if dir_b > 1:
        dir_b = 1
    elif dir_b < -1:
        dir_b = -1

    return {
        "L0": (trend_b, mom_b, pos_b, body_b, streak_b, sr_b, vol_b, dir_b),
        "L1": (trend_b, mom_b, pos_b, streak_b, dir_b),
        "L2": (trend_b, mom_b),
    }


# ─────────────────────────── maps / lookup ────────────────────────────

def _add(m, sigs, up):
    """Record one resolved outcome under every level's key of `sigs`."""
    for lvl in ("L0", "L1", "L2"):
        k = sigs[lvl]
        c = m.get(k)
        if c is None:
            m[k] = [1, 1 if up else 0]
        else:
            c[0] += 1
            c[1] += 1 if up else 0


def _lookup_one(m, sigs):
    """(level, p_up, n) at the first level with enough samples, else
    (None, None, 0). Jeffreys smoothing on the reported probability."""
    for lvl, min_n in _levels:
        c = m.get(sigs[lvl])
        if c and c[0] >= min_n:
            n, up = c
            return lvl, (up + 0.5) / (n + 1.0), n
    return None, None, 0


def _dir_up(candle):
    """1 UP / 0 DOWN / None doji — the app's own grading rule."""
    if candle["close"] > candle["open"]:
        return 1
    if candle["close"] < candle["open"]:
        return 0
    return None


# ───────────────────── training-row enrichment ────────────────────────

def enrich_rows(rows):
    """Add HIST_FEATURE_NAMES to build_dataset rows (LEAK-SAFE, per-asset).

    Rows must be grouped per asset and time-ordered inside each group —
    exactly what build_dataset returns. Maps are per-asset (the live
    engine is per-pair too — training and live must see the same stats).

    Deferral rule: row i's y1 (candle i+1) enters the maps only when a
    row with window_end_ctime >= t1_ctime is processed; y2 likewise with
    t2_ctime. Doji outcomes are skipped (build_dataset drops those rows).
    """
    cur_asset = None
    t1m, t2m = {}, {}
    pend_t1, pend_t2 = [], []

    def _flush(we):
        i = 0
        while i < len(pend_t1):
            if pend_t1[i][0] <= we:
                _, sigs, up = pend_t1.pop(i)
                _add(t1m, sigs, up)
            else:
                i += 1
        i = 0
        while i < len(pend_t2):
            if pend_t2[i][0] <= we:
                _, sigs, up = pend_t2.pop(i)
                _add(t2m, sigs, up)
            else:
                i += 1

    for r in rows:
        if r.get("asset") != cur_asset:
            cur_asset = r.get("asset")
            t1m, t2m = {}, {}
            pend_t1, pend_t2 = [], []
        we = r["window_end_ctime"]
        _flush(we)

        sigs = signature_from_row(r)
        _, p1, n1 = _lookup_one(t1m, sigs)
        _, p2, n2 = _lookup_one(t2m, sigs)
        r["hist_p_up_t1"] = round(p1, 4) if p1 is not None else 0.5
        r["hist_p_up_t2"] = round(p2, 4) if p2 is not None else 0.5
        r["hist_conf"] = _conf(max(n1, n2))

        y1, y2 = r.get("y1_up"), r.get("y2_up")
        if y1 is not None:
            pend_t1.append((r["t1_ctime"], sigs, bool(y1)))
        if y2 is not None:
            pend_t2.append((r["t2_ctime"], sigs, bool(y2)))
    return rows


# ─────────────────────────── live engine ──────────────────────────────

_live = {}
_live_lock = threading.Lock()


def reset_live():
    """Drop all live engine state (tests / pair list changes)."""
    with _live_lock:
        _live.clear()


def _sig_at(candles, end_idx):
    """Signature of the window ENDING at candles[end_idx] (inclusive)."""
    from core.otc_features import build_feature_row
    lo = end_idx - SIG_WINDOW + 1
    if lo < 0:
        return None
    return signature_from_row(build_feature_row(candles[lo:end_idx + 1]))


def _cold_build(candles):
    """Batch-build both maps from the closed history (restart / gap)."""
    t1m, t2m = {}, {}
    n = len(candles)
    for i in range(SIG_WINDOW - 1, n):
        sigs = _sig_at(candles, i)
        if sigs is None:
            continue
        if i + 1 < n:
            up = _dir_up(candles[i + 1])
            if up is not None:
                _add(t1m, sigs, up)
        if i + 2 < n:
            up = _dir_up(candles[i + 2])
            if up is not None:
                _add(t2m, sigs, up)
    # trailing signatures for the incremental path: [sig_{n-2}, sig_{n-1}]
    last_two = [_sig_at(candles, n - 2), _sig_at(candles, n - 1)]
    return {"t1": t1m, "t2": t2m, "last_two": last_two,
            "last_close": candles[-1]["time"] if n else None}


def _cold_history(asset, period, candles):
    """Merged closed-candle history for the cold build: candle_micro (long,
    bounded) + the live stream's own candles (newest, authoritative).

    Falls back to the stream alone when the DB is missing/empty (tests,
    first deploy) — never raises into the prediction path.
    """
    hist = list(candles)
    if _HIST_USE_DB and _HIST_MAX_CANDLES > 0:
        try:
            from core.otc_dataset import load_candles_from_db
            from db import DB_PATH
            db_c = (load_candles_from_db(DB_PATH, period=period)
                    .get(asset) or [])
            if db_c:
                stream_times = {c["time"] for c in candles}
                extra = [c for c in db_c
                         if c["time"] not in stream_times
                         and c["time"] < candles[0]["time"]]
                hist = extra + list(candles)
        except Exception:
            pass  # DB read is best-effort only
    if len(hist) > _HIST_MAX_CANDLES:
        hist = hist[-_HIST_MAX_CANDLES:]
    return hist


def live_lookup(asset, period, candles):
    """Update the per-(asset, period) engine with the just-closed candle
    and return the CURRENT setup's historical stats.

    candles — ALL closed candles oldest→newest (the live stream slice the
    predictor already holds). Returns
        {"p_up_t1", "n_t1", "level_t1", "p_up_t2", "n_t2", "level_t2"}
    or None when there is not enough history or no level reached its
    sample floor (honest abstention — Deep Report §35).
    """
    if not candles or len(candles) < SIG_WINDOW + 2:
        return None
    key = (asset, int(period))
    closed = candles[-1]

    with _live_lock:
        st = _live.get(key)
        if st is None or st.get("last_close") != candles[-2]["time"]:
            # cold start / redeploy / feed gap — rebuild from history
            # (candle_micro merge inside, bounded, best-effort)
            st = _cold_build(_cold_history(asset, int(period), candles))
            _live[key] = st
        else:
            # incremental: this close resolves the PREVIOUS close's T+1
            # and the one before that's T+2 — nothing else becomes known
            up = _dir_up(closed)
            if up is not None and len(st["last_two"]) == 2:
                sig_prev, sig_prev2 = st["last_two"]
                if sig_prev:
                    _add(st["t1"], sig_prev, up)
                if sig_prev2:
                    _add(st["t2"], sig_prev2, up)
        # current signature (window ending at the just-closed candle)
        sigs = _sig_at(candles, len(candles) - 1)
        if sigs is None:
            return None
        # shift the trailing pair for the next close
        prev_sig = st["last_two"][1] if len(st["last_two"]) == 2 else None
        st["last_two"] = [prev_sig, sigs]
        st["last_close"] = closed["time"]

        l1, p1, n1 = _lookup_one(st["t1"], sigs)
        l2, p2, n2 = _lookup_one(st["t2"], sigs)
        if p1 is None and p2 is None:
            return None
        return {
            "p_up_t1": round(p1, 4) if p1 is not None else None,
            "n_t1": n1, "level_t1": l1,
            "p_up_t2": round(p2, 4) if p2 is not None else None,
            "n_t2": n2, "level_t2": l2,
        }
