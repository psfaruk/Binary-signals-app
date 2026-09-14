"""core/otc_predict/features_deep.py — DEEP feature block (2026-09-14).

USER SPEC (Bengali, verbatim):
    "আসল মডেলকে উন্নত করতে হবে: Feature engineering আরও গভীর করা
     (এখন যেগুলো আছে তার বাইরে broker-এর algorithm-এর কোনো detectable
     pattern খোঁজা — নির্দিষ্ট সময়, নির্দিষ্ট পেয়ার, নির্দিষ্ট regime-এ)"

TRANSLATION OF THE ASK — hunt for the broker algorithm's own fingerprints,
BEYOND the 73 unified features the models already see:
    * নির্দিষ্ট সময়  (specific TIME)   — hour-of-day / day-of-week / session
      structure. OTC feeds are algorithmically generated around the clock,
      and many such engines embed time-dependent behaviour (volatility
      schedules, hourly shaping, weekend participant mix). If the broker's
      generator has ANY time fingerprint, these features expose it.
    * নির্দিষ্ট পেয়ার (specific PAIR)  — models are per-pair (PART 22), so
      pair identity is implicit; what was missing are pair-LOCAL rolling
      statistics (own up-rate bias, own micro-activity z-scores) that let
      a pair's bundle learn "how THIS feed behaves lately".
    * নির্দিষ্ট regime (specific REGIME) — one-hot regime states + Hurst
      exponent + persistence/alternation statistics. A regime-switching
      generator is detectable exactly through serial-dependence features
      (direction autocorrelation, run lengths, order-2 pattern entropy,
      conditional up-after-up/down rates).

LEAK-SAFETY — same API contract as core/otc_features.py /
features_ext.py (PART 19): `build_deep_row(window)` receives ONLY closed
candles up to and including the prediction-time candle. The timestamp of
candle i (window[-1]["time"]) is the candle's own ctime — known the
moment it closes, before any future candle exists. No index, no way to
touch the future. `verify_deep_lock()` proves it with the standard
perturbation protocol (mutating every candle AFTER i must not change any
feature; mutating candle i must).

All statistics degrade honestly (neutral values) on short windows instead
of raising, and every field read off a candle is .get()-guarded so a live
candle dict without microstructure cannot crash the prediction path.
"""

import math

__all__ = ["DEEP_FEATURE_NAMES", "MIN_WINDOW_DEEP", "build_deep_row",
           "verify_deep_lock", "DEEP_BLOCK_NAMES"]

# The 50-stat blocks want 50 candles; shorter windows degrade gracefully
# (the 20-stat blocks need ~24, same floor as the extended block).
MIN_WINDOW_DEEP = 24

# ── block decomposition (ablation experiments address blocks by name) ──────
TIME_BLOCK = (
    "tod_sin", "tod_cos",            # sin/cos of time-of-day on the 24h circle
    "dow_sin", "dow_cos",            # sin/cos of weekday on the 7-day circle
    "hour_f",                        # UTC hour 0..23 (raw — trees can split)
    "minute_f",                      # minute inside the hour, 0..1
    "is_weekend",                    # Sat/Sun — OTC trades weekends, real
    "sess_asia",                     # UTC 00-08 Tokyo window
    "sess_london",                   # UTC 07-16 London window
    "sess_ny",                       # UTC 12-21 New York window
    "sess_overlap",                  # London∩NY 12-16 UTC
    "is_hour_edge",                  # minute ≤4 or ≥55 — hour-roll shaping
)

SERIAL_BLOCK = (
    "autocorr_lag1",                 # direction autocorrelation, last 50, lag 1
    "autocorr_lag2",                 # … lag 2
    "autocorr_lag3",                 # … lag 3
    "autocorr_lag4",                 # … lag 4
    "autocorr_lag5",                 # … lag 5
    "ret_autocorr1_50",              # lag-1 autocorr of SIGNED returns (50)
    "up_rate_20",                    # fraction of UP candles, last 20
    "up_rate_50",                    # fraction of UP candles, last 50
    "alt_rate_20",                   # direction-flip rate, last 20
    "run_len_mean_20",               # mean same-direction run length, last 20
    "bigram_entropy_50",             # order-2 direction-pattern entropy (bits)
    "cond_up_after_up_50",           # P(next UP | cur UP) from last 50
    "cond_up_after_dn_50",           # P(next UP | cur DN) from last 50
    "last2_pattern",                 # base-3 code of the last 2 directions
    "last3_pattern",                 # base-3 code of the last 3 directions
)

REGIME_BLOCK = (
    "regime_trending_up",            # detect_regime one-hots (closed window)
    "regime_trending_down",
    "regime_ranging",
    "regime_high_vol",
    "regime_low_vol",
    "regime_trend_score",            # signed |ema10-ema20|/ATR
    "regime_vol_ratio",              # mean range 10 / mean range 30
    "hurst_50",                      # R/S Hurst estimate — the classic
                                     # persistence (>.5) / anti-persistence
                                     # (<.5) fingerprint of a generator
    "zscore_20",                     # (close - SMA20) / std20
    "atr_rank_50",                   # percentile of current TR in last 50
    "vol_of_vol",                    # CV of |returns| last 20 — vol clustering
)

INTER_BLOCK = (
    "hour_x_dir",                    # hour × current direction
    "hour_x_streak",                 # hour × signed streak
    "dow_x_dir",                     # weekday × current direction
    "trend_x_alt",                   # trend score × alternation rate
    "regime_x_dir",                  # trend score × direction
    "tod_x_hurst",                   # time-of-day × Hurst
)

MICRO_BLOCK = (
    "micro_tick_z50",                # tick_count z-score vs last 50 candles
    "micro_buy_z50",                 # buy_pct z-score vs last 50 candles
    "micro_fight_rate_20",           # fight-candle fraction, last 20
    "micro_fight_streak",            # consecutive fight candles ending at i
)

DEEP_BLOCK_NAMES = {"time": TIME_BLOCK, "serial": SERIAL_BLOCK,
                    "regime": REGIME_BLOCK, "inter": INTER_BLOCK,
                    "micro": MICRO_BLOCK}

DEEP_FEATURE_NAMES = (TIME_BLOCK + SERIAL_BLOCK + REGIME_BLOCK +
                      INTER_BLOCK + MICRO_BLOCK)


# ─────────────────────────── small helpers ─────────────────────────────────

def _dir(c):
    o, cl = c["open"], c["close"]
    if cl > o:
        return 1
    if cl < o:
        return -1
    return 0


def _dirs(window):
    return [_dir(c) for c in window]


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _autocorr(series, lag):
    """Pearson autocorrelation at `lag` (0 when degenerate)."""
    n = len(series)
    if n <= lag + 1:
        return 0.0
    m = _mean(series)
    num = sum((series[t] - m) * (series[t - lag] - m)
              for t in range(lag, n))
    den = sum((x - m) ** 2 for x in series)
    return (num / den) if den > 1e-12 else 0.0


def _hurst(closes):
    """Simplified R/S Hurst estimate on the last ≤50 closes."""
    n = len(closes)
    if n < 20:
        return 0.5
    rets = [closes[i + 1] - closes[i] for i in range(n - 1)]
    m = _mean(rets)
    cum, dev = 0.0, []
    for r in rets:
        cum += (r - m)
        dev.append(cum)
    r_span = (max(dev) - min(dev)) if dev else 0.0
    s = _std(rets)
    if s <= 1e-12 or r_span <= 1e-12:
        return 0.5
    h = math.log(r_span / s) / math.log(len(rets))
    return min(0.9, max(0.1, h))


def _regime_of(window):
    """detect_regime on the closed window (leak-safe, reused directly)."""
    try:
        from core.otc_predict.regime import detect_regime
        return detect_regime(window)
    except Exception:
        return {"regime": "RANGING", "trend_score": 0.0,
                "vol_state": "normal", "extreme_vol": False}


def _z(vs, cur):
    """z-score of `cur` against list `vs` (0 when degenerate)."""
    s = _std(vs)
    if s <= 1e-12:
        return 0.0
    return (cur - _mean(vs)) / s


# ───────────────────────────── main builder ────────────────────────────────

def build_deep_row(window):
    """Compute the DEEP feature dict from the closed-candle window.

    `window` — CLOSED candles oldest→newest, ending with the prediction-time
    candle (its "time" is that candle's own ctime — known at close time).
    Needs >= MIN_WINDOW_DEEP candles; longer lookbacks (50) degrade to what
    is available with neutral fallbacks.
    """
    if not window or len(window) < MIN_WINDOW_DEEP:
        raise ValueError(
            f"build_deep_row: need >= {MIN_WINDOW_DEEP} closed candles, "
            f"got {len(window) if window else 0}")

    feats = {}
    cur = window[-1]

    # ── TIME block — candle i's own ctime (UTC), known at close ─────────
    t = int(cur.get("time") or 0)
    if t > 0:
        import time as _time
        st = _time.gmtime(t)
        hour, minute = st.tm_hour, st.tm_min
        wday = st.tm_wday            # 0=Mon … 6=Sun
    else:                            # defensive: neutral Monday 00:00
        hour, minute, wday = 0, 0, 0

    tod = (hour * 60.0 + minute) / 1440.0
    feats["tod_sin"] = math.sin(2.0 * math.pi * tod)
    feats["tod_cos"] = math.cos(2.0 * math.pi * tod)
    feats["dow_sin"] = math.sin(2.0 * math.pi * wday / 7.0)
    feats["dow_cos"] = math.cos(2.0 * math.pi * wday / 7.0)
    feats["hour_f"] = float(hour)
    feats["minute_f"] = minute / 60.0
    feats["is_weekend"] = 1.0 if wday >= 5 else 0.0
    feats["sess_asia"] = 1.0 if hour < 8 else 0.0
    feats["sess_london"] = 1.0 if 7 <= hour < 16 else 0.0
    feats["sess_ny"] = 1.0 if 12 <= hour < 21 else 0.0
    feats["sess_overlap"] = 1.0 if 12 <= hour < 16 else 0.0
    feats["is_hour_edge"] = 1.0 if (minute <= 4 or minute >= 55) else 0.0

    # ── SERIAL block — the broker's own sequence fingerprint ────────────
    w50 = window[-50:]
    d50 = _dirs(w50)
    d20 = d50[-20:]
    for lag in (1, 2, 3, 4, 5):
        feats[f"autocorr_lag{lag}"] = round(_autocorr(d50, lag), 4)
    closes50 = [c["close"] for c in w50]
    rets50 = [closes50[i + 1] - closes50[i]
              for i in range(len(closes50) - 1)]
    feats["ret_autocorr1_50"] = round(_autocorr(rets50, 1), 4)

    up20 = sum(1 for d in d20 if d == 1)
    up50 = sum(1 for d in d50 if d == 1)
    dn50 = sum(1 for d in d50 if d == -1)
    feats["up_rate_20"] = up20 / len(d20) if d20 else 0.5
    feats["up_rate_50"] = up50 / len(d50) if d50 else 0.5

    flips = sum(1 for a, b in zip(d20, d20[1:]) if a != b)
    feats["alt_rate_20"] = flips / (len(d20) - 1) if len(d20) > 1 else 0.5

    # mean same-direction run length over the last 20 candles
    runs = []
    run = 0
    last = None
    for d in d20:
        if d != 0 and d == last:
            run += 1
        else:
            if last not in (None, 0) and run:
                runs.append(run)
            run = 1 if d != 0 else 0
        last = d
    if last not in (None, 0) and run:
        runs.append(run)
    feats["run_len_mean_20"] = _mean(runs) if runs else 1.0

    # order-2 direction pattern entropy (UU/UD/DU/DD) over last 50
    # transitions; doji transitions are skipped honestly.
    counts = {"UU": 0, "UD": 0, "DU": 0, "DD": 0}
    for a, b in zip(d50, d50[1:]):
        if a == 1 and b == 1:
            counts["UU"] += 1
        elif a == 1 and b == -1:
            counts["UD"] += 1
        elif a == -1 and b == 1:
            counts["DU"] += 1
        elif a == -1 and b == -1:
            counts["DD"] += 1
    tot = sum(counts.values())
    if tot >= 8:
        ent = -sum((v / tot) * math.log(v / tot, 2.0)
                   for v in counts.values() if v)
        feats["bigram_entropy_50"] = round(min(2.0, ent), 4)
    else:
        feats["bigram_entropy_50"] = 2.0        # no info → max entropy
    uu, ud = counts["UU"], counts["UD"]
    du, dd = counts["DU"], counts["DD"]
    feats["cond_up_after_up_50"] = (uu / (uu + ud)) if (uu + ud) else 0.5
    feats["cond_up_after_dn_50"] = (du / (du + dd)) if (du + dd) else 0.5

    # base-3 codes of the trailing direction patterns (doji = 0 symbol)
    _m = {1: 2, -1: 1, 0: 0}                    # UP=2, DOWN=1, doji=0
    d_last = [_dir(c) for c in window[-3:]]
    feats["last3_pattern"] = float(
        _m.get(d_last[0], 0) * 9 + _m.get(d_last[1], 0) * 3 +
        _m.get(d_last[2], 0)) if len(d_last) == 3 else 13.5   # 13.5 = neutral
    d_last2 = [_dir(c) for c in window[-2:]]
    feats["last2_pattern"] = float(
        _m.get(d_last2[0], 0) * 3 + _m.get(d_last2[1], 0)) \
        if len(d_last2) == 2 else 4.5           # 4.5 = neutral

    # ── REGIME block — closed-window regime, quantified ─────────────────
    reg = _regime_of(window)
    for name in ("trending_up", "trending_down", "ranging", "high_vol",
                 "low_vol"):
        feats[f"regime_{name}"] = 1.0 if reg.get("regime") == name.upper() \
            else 0.0
    feats["regime_trend_score"] = float(reg.get("trend_score") or 0.0)
    feats["regime_vol_ratio"] = float(reg.get("range_ratio") or 1.0)
    feats["hurst_50"] = round(_hurst(closes50), 4)

    c = cur["close"]
    c20 = [x["close"] for x in window[-20:]]
    m20, s20 = _mean(c20), _std(c20)
    feats["zscore_20"] = ((c - m20) / s20) if s20 > 1e-12 else 0.0

    # percentile rank of the current true range among the last 50
    trs = []
    for j in range(max(1, len(w50)), len(w50)):
        h, l, pc = w50[j]["high"], w50[j]["low"], w50[j - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    cur_tr = (cur["high"] - cur["low"]) if trs else 0.0
    if trs:
        feats["atr_rank_50"] = (sum(1 for x in trs if x <= cur_tr) /
                                len(trs))
    else:
        feats["atr_rank_50"] = 0.5

    absr = [abs(closes50[i + 1] - closes50[i])
            for i in range(len(closes50) - 1)][-20:]
    ma = _mean(absr)
    feats["vol_of_vol"] = (_std(absr) / ma) if ma > 1e-12 else 0.0

    # ── INTERACTION block — time×pattern / regime×pattern ───────────────
    dir_i = float(_dir(cur))
    streak = 0
    if dir_i != 0:
        streak = 1
        for j in range(len(window) - 2, -1, -1):
            if _dir(window[j]) == dir_i:
                streak += 1
            else:
                break
    signed_streak = streak if dir_i > 0 else -streak if dir_i < 0 else 0
    feats["hour_x_dir"] = feats["hour_f"] * dir_i
    feats["hour_x_streak"] = feats["hour_f"] * signed_streak
    feats["dow_x_dir"] = float(wday) * dir_i
    feats["trend_x_alt"] = feats["regime_trend_score"] * feats["alt_rate_20"]
    feats["regime_x_dir"] = feats["regime_trend_score"] * dir_i
    feats["tod_x_hurst"] = feats["tod_sin"] * feats["hurst_50"]

    # ── MICRO block — pair-local activity statistics ─────────────────────
    ticks = [float(x.get("tick_count") or 0.0) for x in w50]
    buys = [float(x.get("buy_pct") if x.get("buy_pct") is not None else 50.0)
            for x in w50]
    cur_tick = ticks[-1] if ticks else 0.0
    cur_buy = buys[-1] if buys else 50.0
    feats["micro_tick_z50"] = round(_z(ticks, cur_tick), 4)
    feats["micro_buy_z50"] = round(_z(buys, cur_buy), 4)

    fights = [1 if x.get("is_fight") else 0 for x in w50[-20:]]
    feats["micro_fight_rate_20"] = _mean(fights) if fights else 0.0
    fstreak = 0
    for x in reversed(w50):
        if x.get("is_fight"):
            fstreak += 1
        else:
            break
    feats["micro_fight_streak"] = float(min(fstreak, 20))

    return feats


# ─────────────────────── PART 19 perturbation proof ────────────────────────

def verify_deep_lock(candles, n_checks=25, window=50, seed=19):
    """Mutating every candle strictly AFTER i must not change any DEEP
    feature; mutating candle i must change features (non-vacuity).
    Times are NOT mutated (times are not future-observable anyway — the
    window slice ends at i). Returns (n_checks, future_hits, self_hits).
    """
    import random
    rng = random.Random(seed)
    n = len(candles)
    if n < max(window, MIN_WINDOW_DEEP) + 3:
        raise ValueError("verify_deep_lock: not enough candles")

    future_hits = self_hits = 0
    wlen = max(window, MIN_WINDOW_DEEP)
    for _ in range(n_checks):
        i = rng.randrange(wlen - 1, n - 2)
        base = build_deep_row(candles[i - wlen + 1: i + 1])

        mutated = [dict(x) for x in candles]
        for j in range(i + 1, n):
            mutated[j]["open"] = mutated[j]["open"] * 1.9 + 0.31
            mutated[j]["close"] = mutated[j]["close"] * 0.5 + 7.77
            mutated[j]["high"] = mutated[j]["high"] * 1.4 + 3.3
            mutated[j]["low"] = mutated[j]["low"] * 0.7 + 1.1
        after = build_deep_row(mutated[i - wlen + 1: i + 1])
        if repr(base) != repr(after):
            future_hits += 1

        mutated2 = [dict(x) for x in candles]
        mutated2[i]["close"] = mutated2[i]["close"] * 1.07 + 0.002
        after2 = build_deep_row(mutated2[i - wlen + 1: i + 1])
        if repr(base) != repr(after2):
            self_hits += 1

    return n_checks, future_hits, self_hits
