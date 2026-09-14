"""core/otc_predict/synth_seed.py — OFFLINE SYNTHETIC OTC HISTORY SEEDER
(2026-09-14, "ডেটা আসছে না ও ট্রেইন হচ্ছে না" fix).

ROOT CAUSE this module solves:
    candle_micro's ONLY data sources were (a) the live Quotex tick feed and
    (b) the platform history top-up — BOTH require a valid session token.
    With no/expired token the table stays EMPTY, the fast-train daemon logs
    "no candle data at all — nothing to train on yet", zero models ever
    register, and the মডেল tab shows nothing forever. The user sees exactly
    that: মডেল গুলো তে ডেটা আসছে না, ট্রেইন হচ্ছে না.

WHY SYNTHETIC IS LEGITIMATE HERE:
    Quotex OTC pairs are NOT real-market instruments — the broker generates
    their price feeds algorithmically (random-walk style engines with
    regime changes). A locally generated candle stream with matching
    statistical shape is the same KIND of data, clearly labelled. It exists
    ONLY to warm-start the training pipeline so that:
      * the whole train→register→predict→settle machinery is exercised and
        verifiable from the first minutes of a fresh deploy,
      * models exist the moment a real token arrives (they retrain on real
        data within one 10-minute consolidation cycle).

HONESTY CONTRACT (the app's core principle — never fake a live feed):
    1. Synthetic rows NEVER drive the live chart/signals — the feed stays
       real-token-only (sim mode was permanently disabled 2026-07-25 and
       this module does NOT resurrect it). Synthetic data feeds TRAINING
       (candle_micro consumers: build_dataset / backtests) only.
    2. Provenance is tracked in _meta ('synth_seed' → per-pair time
       ranges). Any model trained while a pair still holds synthetic rows
       carries meta data_source="synthetic" and is hard-capped to
       status="provisional" — the UI badge says সিন্থেটিক ডেটা.
    3. REAL DATA ALWAYS WINS: live writes use INSERT OR REPLACE on the same
       (asset,period,ctime) key; the seeder uses INSERT OR IGNORE.
    4. PURGE: once a pair has ≥ PURGE_MIN_REAL closed candles AFTER the
       synthetic range (i.e. the live feed actually flowed), the synthetic
       rows are deleted and the next bootstrap retrains on pure real data.
    5. Pair-specific shape: family profiles (exotic/cross/major) set base
       price, per-candle volatility and tick density — mirrors
       engines/otc/config.py's per-family behaviour classes.

GENERATOR (statistically honest — no planted edge):
    regime-switching random walk: {trend, range, volatile} Markov chain,
    zero long-run drift ⇒ candle directions ≈ 50/50 coin-flip. Models
    trained on it land ~50% walk-forward accuracy → status provisional,
    which is exactly the truthful statement about synthetic cold-start.
    Microstructure (buy_pct/sell_pct/tick_count/is_fight/...) is derived
    from the same sub-minute ticks the candle is built from.

Env knobs: QX_SYNTH_SEED (default 1) — set 0 to forbid synthetic seeding.
"""

import json
import math
import os
import random
import sqlite3
import time

__all__ = ["seed_synthetic_history", "purge_stale_synthetic", "purge_pair",
           "effective_real_counts", "synth_ranges", "synth_stats",
           "seed_enabled", "SYNTH_DAYS"]

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# ── hardcoded config (same style as fast_train.py) ────────────────────────
SYNTH_DAYS = int(os.environ.get("QX_SYNTH_DAYS", "2"))     # 2d ≈ 2880 1m candles
PURGE_MIN_REAL = 200      # live candles after the synth range → purge it
META_KEY = "synth_seed"

# per-pair shape: base price range / per-candle RELATIVE vol (σ as a
# fraction of price) / tick density per minute. Tuned to look like the
# broker's OTC engines (exotics swing harder than crosses/majors).
_FAMILY = {
    "exotic":   {"vol": 0.0011, "ticks": (18, 42)},
    "cross":    {"vol": 0.00045, "ticks": (30, 70)},
    "major":    {"vol": 0.00028, "ticks": (40, 90)},
    "volatile": {"vol": 0.0018, "ticks": (15, 35)},
}

# per-pair family + realistic OTC quote range (start anchor drawn inside)
_PAIR_SPEC = {
    # USD-exotics (the repo's own otc/config.py classes these volatile)
    "USDBDT_otc": ("exotic", (112.0, 122.0)),
    "USDPKR_otc": ("exotic", (272.0, 284.0)),
    "USDPHP_otc": ("exotic", (55.5, 59.5)),
    "USDDZD_otc": ("exotic", (131.0, 137.0)),
    "USDINR_otc": ("exotic", (83.0, 87.0)),
    "USDIDR_otc": ("exotic", (15600.0, 16200.0)),
    "USDMXN_otc": ("volatile", (17.2, 19.6)),
    "USDCOP_otc": ("volatile", (3900.0, 4150.0)),
    "USDZAR_otc": ("volatile", (17.0, 19.0)),
    "USDARS_otc": ("volatile", (950.0, 1080.0)),
    "USDNGN_otc": ("volatile", (1450.0, 1580.0)),
    "BRLUSD_otc": ("volatile", (0.172, 0.196)),
    # crosses (yen pairs quoted ~90, others as usual)
    "AUDJPY_otc": ("cross", (90.0, 97.0)),
    "NZDJPY_otc": ("cross", (87.0, 94.0)),
    "NZDCAD_otc": ("cross", (0.80, 0.92)),
    "GBPNZD_otc": ("cross", (2.00, 2.20)),
    "EURNZD_otc": ("cross", (1.74, 1.90)),
    # majors-ish OTC
    "NZDUSD_otc": ("major", (0.575, 0.625)),
}

_DEFAULT_SPEC = ("exotic", (50.0, 150.0))

# decimals per price magnitude — candles must round like real quotes
def _decimals(price):
    if price >= 100:  return 2
    if price >= 10:   return 3
    if price >= 2:    return 4
    return 5


def seed_enabled():
    return os.environ.get("QX_SYNTH_SEED", "1") not in ("0", "false", "no")


def _db_path():
    from db import DB_PATH
    return DB_PATH


# ─────────────────────── provenance (_meta) ───────────────────────────────

def synth_ranges():
    """{asset: [first_ctime, last_ctime]} currently-seeded synthetic ranges."""
    try:
        conn = sqlite3.connect(_db_path(), timeout=15)
        try:
            row = conn.execute(
                "SELECT value FROM _meta WHERE key=?", (META_KEY,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row or not row[0]:
        return {}
    try:
        data = json.loads(row[0])
        return {a: [int(v[0]), int(v[1])] for a, v in data.items()
                if isinstance(v, (list, tuple)) and len(v) == 2}
    except Exception:
        return {}


def _save_ranges(conn, ranges):
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
        (META_KEY, json.dumps({a: list(v) for a, v in ranges.items()})))
    conn.commit()


# ───────────────────────── generator core ─────────────────────────────────

def _gen_pair_candles(asset, days, end_ts, rng):
    """One pair's contiguous 1m OHLCV-with-micro candles, ending at end_ts.

    Regime chain: trend(+drift) / range(ω-revert) / volatile(2σ). Zero
    long-run drift (plus a weak anchor pull) ⇒ directions are an honest
    coin-flip; the regimes only shape autocorrelation of RETURNS and
    volatility clustering, which is what the feature engine (atr_norm,
    vol_10/20, streak, mom_*) actually reads — so models train on realistic
    dynamics, not white noise.
    """
    fam_name, prange = _PAIR_SPEC.get(asset, _DEFAULT_SPEC)
    fam = _FAMILY[fam_name]
    n = int(days * 1440)
    start = end_ts - (n - 1) * 60
    anchor = rng.uniform(*prange)          # walk reverts weakly toward it
    price = anchor
    dec = _decimals(price)
    pip = 10.0 ** (-dec)

    # regime state
    regime = rng.choice(("trend", "range", "volatile"))
    drift = 0.0
    regime_left = rng.randint(20, 120)

    candles = []
    for i in range(n):
        if regime_left <= 0:
            regime = rng.choices(("trend", "range", "volatile"),
                                 weights=(0.30, 0.45, 0.25))[0]
            drift = rng.uniform(0.08, 0.25) * rng.choice((-1.0, 1.0))
            regime_left = rng.randint(20, 160)
        regime_left -= 1

        sigma = fam["vol"] * price        # RELATIVE vol → price units
        if regime == "volatile":
            sigma *= rng.uniform(1.8, 2.6)
            drift *= 0.3
        elif regime == "range":
            sigma *= rng.uniform(0.55, 0.9)
            drift *= 0.1
        # trend keeps base sigma, full drift

        o = price
        # sub-minute ticks build the candle body AND microstructure
        n_ticks = rng.randint(*fam["ticks"])
        step_sd = sigma / math.sqrt(max(1, n_ticks))
        step_mu = drift * sigma / max(1, n_ticks)   # drift per candle = drift·σ
        # weak anchor pull keeps the walk inside the pair's quote band
        kappa = 0.0006
        steps = [rng.gauss(step_mu + kappa * (anchor - price) / n_ticks,
                           step_sd)
                 for _ in range(n_ticks)]
        prices = [o]
        for s in steps:
            prices.append(prices[-1] + s)
        c = prices[-1]
        hi = max(prices)
        lo = min(prices)
        # small spike wicks beyond the tick extremes (feed spikes),
        # bounded by ~20% of the candle's own range
        span = max(hi - lo, pip)
        hi += rng.uniform(0.0, 0.20) * span
        lo -= rng.uniform(0.0, 0.20) * span

        # microstructure from the SAME tick path (buy/sell pressure ≈
        # fraction of ticks trading above the candle open — exactly how
        # feed._analyze_microstructure derives it from real ticks)
        above = sum(1 for p in prices[1:] if p > o)
        below = sum(1 for p in prices[1:] if p < o)
        denom = max(1, above + below)
        buy_pct = round(100.0 * above / denom, 1)
        sell_pct = round(100.0 * below / denom, 1)
        is_fight = 1 if (0.42 <= buy_pct / 100.0 <= 0.58 and
                         abs(c - o) < 0.30 * max(hi - lo, pip)) else 0
        # round to quote precision in INTEGER PIPS — float dust (1-ulp
        # double-rounding like 2.0848999999999998 vs 2.0849) can otherwise
        # break high≥body≥low on the stored doubles; integer comparison
        # + one shared scale division keeps the invariants EXACT.
        # A doji (close==open) must stay IMPOSSIBLE (real feeds tick past
        # precision) — force a one-pip body minimum before high/low.
        scale = 10 ** dec
        ro_i = int(round(o * scale))
        rc_i = int(round(c * scale))
        if rc_i == ro_i:
            rc_i = ro_i + (1 if c >= o else -1)
        hi_i = max(int(round(hi * scale)), ro_i, rc_i)
        lo_i = max(min(int(round(lo * scale)), ro_i, rc_i), 1)
        candles.append({
            "time": start + i * 60,
            "open": ro_i / scale, "high": hi_i / scale,
            "low": lo_i / scale, "close": rc_i / scale,
            "buy_pct": buy_pct, "sell_pct": sell_pct,
            "tick_count": n_ticks, "is_fight": is_fight,
        })
        price = c
    return candles


def seed_synthetic_history(assets, days=None, db_path=None, log=print,
                            max_existing=0):
    """Seed candle_micro with synthetic 1m candles for the given assets.

    Honesty boundary (STARVED-SEED 2026-09-14): a pair whose EXISTING
    candle count exceeds ``max_existing`` is never touched — real history
    stays pure. Pairs at or below it (zero, or a starved handful of stale
    rows that can never reach FAST_MIN_PAIR_ROWS on their own) get a
    synthetic cold-start on top; INSERT OR IGNORE keeps every real row
    sacred, provenance ranges cover ONLY the synthetic block, and the
    মডেল tab badges the pair সিন্থেটিক until real data purges it.
    fast_train passes max_existing=SEED_STARVED_CANDLES so the old
    zero-only rule widens to "too little data to ever train" — closing
    the band where a pair with e.g. 40 stale candles was neither seeded
    nor trainable and the app showed "মডেল এখনো প্রস্তুত নয়" forever.
    Returns {asset: {"added": n, "range": [t0, t1]}} for seeded pairs.
    """
    days = days or SYNTH_DAYS
    db_path = db_path or _db_path()
    if not seed_enabled():
        log("[synth-seed] disabled by QX_SYNTH_SEED=0")
        return {}
    if not assets:
        return {}

    conn = sqlite3.connect(db_path, timeout=60)
    seeded = {}
    try:
        existing_counts = {a: int(n) for a, n in conn.execute(
            "SELECT asset, COUNT(*) FROM candle_micro WHERE period=60 "
            "GROUP BY asset")}
        now_min = int(time.time()) // 60 * 60
        ranges = synth_ranges()
        for asset in assets:
            have = existing_counts.get(asset, 0)
            if have > int(max_existing):
                continue    # enough real history — never blend
            if have and asset in ranges:
                continue    # already carries a synth block — never re-stack
            rng = random.Random(f"{asset}:synth:v1")   # stable, reproducible
            candles = _gen_pair_candles(asset, days, now_min, rng)
            conn.executemany(
                "INSERT OR IGNORE INTO candle_micro"
                "(asset, period, ctime, open, high, low, close,"
                " buy_pct, sell_pct, pressure, is_fight, crosses,"
                " tick_count, net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(asset, 60, c["time"], c["open"], c["high"], c["low"],
                  c["close"], c["buy_pct"], c["sell_pct"],
                  "buy" if c["buy_pct"] > 55 else
                  ("sell" if c["sell_pct"] > 55 else "fight"),
                  c["is_fight"],
                  1 if c["buy_pct"] > 58 or c["sell_pct"] > 58 else 0,
                  c["tick_count"],
                  round((c["close"] - c["open"]) / max(c["tick_count"], 1), 9))
                 for c in candles])
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) FROM candle_micro "
                "WHERE asset=? AND period=60", (asset,)).fetchone()[0]
            # "added" = rows this seed actually contributed (a blended
            # starved pair keeps its few pre-existing real rows — those
            # are not ours to claim)
            seeded[asset] = {"added": max(0, n - have),
                             "range": [candles[0]["time"], candles[-1]["time"]]}
            ranges[asset] = [candles[0]["time"], candles[-1]["time"]]
        _save_ranges(conn, ranges)
    finally:
        conn.close()
    for a, r in seeded.items():
        log(f"[synth-seed] {a}: +{r['added']} synthetic candles "
            f"({days}d) — provenance tracked, purge-on-live armed")
    return seeded


# ───────────────────── purge when real data arrives ───────────────────────

def purge_pair(asset, db_path=None, log=print):
    """Delete ONE pair's synthetic rows + provenance (real data wins).

    Called by fast_train._fetch_batch the moment a REAL platform history
    pull succeeds for the pair — the fetch's INSERT OR IGNORE must not be
    blocked by same-minute synthetic rows, so the synthetic range is
    dropped right before the real rows land.
    """
    db_path = db_path or _db_path()
    ranges = synth_ranges()
    rng_ = ranges.pop(asset, None)
    if rng_ is None:
        return 0
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        cur = conn.execute(
            "DELETE FROM candle_micro "
            "WHERE asset=? AND period=60 AND ctime BETWEEN ? AND ?",
            (asset, rng_[0], rng_[1]))
        conn.commit()
        _save_ranges(conn, ranges)
        n = cur.rowcount
    finally:
        conn.close()
    if n:
        log(f"[synth-seed] {asset}: purged {n} synthetic rows — "
            f"real platform history superseded them")
    return n


def effective_real_counts(counts, db_path=None):
    """{asset: real_rows} — counts minus rows inside synthetic ranges.

    fast_train passes these to ensure_history so a synth-seeded pair is
    still considered 'short' and gets its REAL platform top-up (its 2880
    synthetic candles must never mask the need for real data).
    """
    ranges = synth_ranges()
    if not ranges or not counts:
        return dict(counts or {})
    db_path = db_path or _db_path()
    out = dict(counts)
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            for asset, (t0, t1) in ranges.items():
                if asset not in out:
                    continue
                synth_n = conn.execute(
                    "SELECT COUNT(*) FROM candle_micro "
                    "WHERE asset=? AND period=60 AND ctime BETWEEN ? AND ?",
                    (asset, t0, t1)).fetchone()[0]
                out[asset] = max(0, out[asset] - int(synth_n))
        finally:
            conn.close()
    except Exception:
        pass
    return out


def purge_stale_synthetic(db_path=None, log=print):
    """Delete synthetic rows for pairs the live feed has taken over.

    A pair is "taken over" when it holds ≥ PURGE_MIN_REAL candles dated
    AFTER its synthetic range end (only the live feed or a platform
    history top-up can write those — the seeder never extends past its
    recorded range without updating it). Called at the START of every
    fast-train bootstrap so a retrains-on-pure-real-data cycle follows
    the token import within one retry window (≤10 min).
    """
    db_path = db_path or _db_path()
    ranges = synth_ranges()
    if not ranges:
        return {}
    conn = sqlite3.connect(db_path, timeout=60)
    purged = {}
    try:
        for asset, (t0, t1) in list(ranges.items()):
            real_n = conn.execute(
                "SELECT COUNT(*) FROM candle_micro "
                "WHERE asset=? AND period=60 AND ctime>?", (asset, t1)
            ).fetchone()[0]
            if real_n >= PURGE_MIN_REAL:
                cur = conn.execute(
                    "DELETE FROM candle_micro "
                    "WHERE asset=? AND period=60 AND ctime BETWEEN ? AND ?",
                    (asset, t0, t1))
                conn.commit()
                purged[asset] = {"deleted": cur.rowcount, "real": real_n}
                ranges.pop(asset)
                log(f"[synth-seed] {asset}: purged {cur.rowcount} synthetic "
                    f"rows — {real_n} live candles took over")
        _save_ranges(conn, ranges)
    finally:
        conn.close()
    return purged


def synth_stats():
    """Compact provenance snapshot for the মডেল tab / diagnostics."""
    ranges = synth_ranges()
    return {"enabled": seed_enabled(), "days": SYNTH_DAYS,
            "pairs": sorted(ranges.keys()),
            "ranges": ranges,
            "purge_min_real": PURGE_MIN_REAL}
