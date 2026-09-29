"""OTC GENERATOR FINGERPRINT — "এই ফিডটা কোন সিস্টেমে তৈরি?"
=================================================================
USER QUESTION (2026-09-29, verbatim intent):

  "Quotex এর ক্যান্ডেল গুলো কোনো একটা সিস্টেম দিয়ে তৈরি, কারণ এখানে কোনো
  রিয়েল বায়ার নাই। ক্যান্ডেল গুলো কি pre-জেনারেট? হতে পারে নির্দিষ্ট কিছু
  ক্যান্ডেল? না হলে এত স্মুথলি কিভাবে দেখায় frontend এ? এই বিষয়টা
  চিহ্নিত করতে পারলেই প্রেডিকশন সম্ভব।"

WHAT THIS MODULE DOES
---------------------
Every price feed leaves measurable fingerprints of the machine that
made it.  This module measures them, live, on whatever feed the app is
connected to (real Quotex OTC, real market, or demo):

  1. TICK CADENCE        — how regular are tick arrivals?
                          (fixed-cadence generator vs trade-driven bursts)
  2. TICKS PER CANDLE    — consistency of tick density per 1m candle
  3. PRICE GRID          — fixed pip grid / digit quantization
  4. INCREMENT SHAPE     — kurtosis/normality of tick returns
                          (RNG ≈ Gaussian; real flow = fat tails)
  5. VARIANCE RATIO      — Lo–MacKinlay VR(q): mean-reverting (<1),
                          random-walk (≈1), trending (>1)
  6. HURST (R/S)         — persistence measure of the close series
  7. DIRECTION MEMORY    — autocorrelation of candle colors, streak
                          distribution vs geometric expectation
  8. REPETITION SEARCH   — do exact normalized tick/candle sequences
                          repeat?  (pre-generated block library ⇒ YES;
                          fresh PRNG walk ⇒ near-unique)
  9. WEEKEND CONTINUITY  — candles on Sat/Sun ⇒ synthetic feed
                          (real FX/stock markets close)
 10. HOURLY σ SCHEDULE   — time-of-day volatility schedule ⇒ designed
                          generator, not crowd behavior
 11. CROSS-ASSET SYNC    — do many assets tick on the same seconds?
                          (one shared engine driving all "pairs")

VERDICT
-------
classify() combines the axes into one of:
  TRADE_DRIVEN_REAL     — irregular ticks, fat tails, weekend gaps
  SYNTHETIC_RANDOM_WALK — fixed cadence, Gaussian-ish, VR≈1, H≈0.5
  SYNTHETIC_MEAN_REVERT — VR<<1, H<0.45, strong color anti-persistence
  SYNTHETIC_REGIME_MIX  — VR varies by window (trend/MR regimes)
plus pre_generated_blocks: YES/NO and a predictability score that says
HOW MUCH a sequence model (CSE) can extract from this feed.

All stats are O(1) amortized per tick (deques + running sums); report()
does the heavier pass over the trailing window on demand.
Pure Python, no numpy — same convention as next_candle.py.
=====================================================================
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from typing import Deque, Dict, List, Optional, Tuple

# ─── window sizes ────────────────────────────────────────────────────
MAX_TICKS = 40_000          # trailing raw ticks per asset (~5h at 2Hz)
MAX_CANDLES = 3_000         # trailing 1m candles (~2 days)
REP_K = 8                   # repetition-search sequence length (ticks)
REP_K_CANDLE = 4            # repetition-search candle-color length
MIN_TICKS_FOR_REPORT = 300
MIN_CANDLES_FOR_REPORT = 120


# ═════════════════════════════════════════════════════════════════════
#  Small math helpers (pure python)
# ═════════════════════════════════════════════════════════════════════

def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs) -> float:
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _cv(xs) -> float:
    m = _mean(xs)
    return (_std(xs) / m) if m > 0 else 0.0


def _kurtosis(xs) -> float:
    """Excess kurtosis (normal = 0).  Fat tails ⇒ large positive."""
    xs = list(xs)
    n = len(xs)
    if n < 8:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    if var <= 0:
        return 0.0
    m4 = sum((x - m) ** 4 for x in xs) / n
    return m4 / (var * var) - 3.0


def _autocorr(xs, lag: int = 1) -> float:
    xs = list(xs)
    n = len(xs)
    if n < lag + 8:
        return 0.0
    m = sum(xs) / n
    num = 0.0
    den = 0.0
    for i in range(n):
        d = xs[i] - m
        den += d * d
        if i >= lag:
            num += d * (xs[i - lag] - m)
    return (num / den) if den > 0 else 0.0


def _variance_ratio(returns, q: int) -> Optional[float]:
    """Lo–MacKinlay VR(q).  returns = log returns, stationary-ish."""
    r = [x for x in returns if x == x and abs(x) < 1]
    n = len(r)
    if n < q * 8:
        return None
    mu = sum(r) / n
    var1 = sum((x - mu) ** 2 for x in r) / (n - 1)
    if var1 <= 0:
        return None
    # q-period returns
    rq = []
    for i in range(0, n - q + 1, q):
        rq.append(sum(r[i:i + q]) - q * mu)
    m = len(rq)
    if m < 8:
        return None
    varq = sum(x * x for x in rq) / m
    vr = varq / (q * var1)
    return vr


def _hurst_rs(series) -> Optional[float]:
    """Rescaled-range Hurst exponent.

    MUST be applied to RETURNS (differences), not price levels —
    levels are non-stationary and always give H≈1.  Validation
    caught this (BTCUSDT levels ⇒ H=1.04, impossible).
    """
    xs = [b - a for a, b in zip(series, series[1:]) if (b - a) == (b - a)]
    if len(xs) < 256:
        return None
    n = len(xs)
    if n < 128:
        return None
    # work on first differences' cumulative deviations, several scales
    rs_list = []
    ns = []
    for size in (64, 128, 256, 512, 1024):
        if n < size * 2:
            break
        chunks = 0
        acc = 0.0
        for start in range(0, n - size + 1, size):
            seg = xs[start:start + size]
            m = sum(seg) / size
            dev = 0.0
            mn = float("inf")
            mx = float("-inf")
            for x in seg:
                dev += x - m
                mn = min(mn, dev)
                mx = max(mx, dev)
            s = _std(seg)
            if s > 0:
                acc += (mx - mn) / s
                chunks += 1
        if chunks:
            rs_list.append(math.log(acc / chunks))
            ns.append(math.log(size))
    if len(rs_list) < 3:
        return None
    # slope of log(R/S) vs log(n)
    nbar = _mean(ns)
    ybar = _mean(rs_list)
    num = sum((a - nbar) * (b - ybar) for a, b in zip(ns, rs_list))
    den = sum((a - nbar) ** 2 for a in ns)
    return (num / den) if den > 0 else None


def _price_grid(prices) -> float:
    """Smallest consistent price increment (the generator's pip grid)."""
    diffs = sorted({abs(round(b - a, 10)) for a, b in zip(prices, prices[1:]) if b != a})
    if not diffs:
        return 0.0
    grid = diffs[0]
    # refine: the true grid is the largest g dividing ~all diffs
    for d in diffs[:50]:
        ok = True
        for e in diffs[:200]:
            q = e / d
            if abs(q - round(q)) > 1e-6 and round(q) != 0:
                ok = False
                break
        if ok:
            grid = d
            break
    return grid


# ═════════════════════════════════════════════════════════════════════
#  Per-asset fingerprint
# ═════════════════════════════════════════════════════════════════════

class AssetFingerprint:
    """Incremental fingerprint state for ONE asset."""

    def __init__(self, asset: str):
        self.asset = asset
        self._lock = threading.Lock()

        # raw ticks: (server_ts_seconds, price)
        self.ticks: Deque[Tuple[float, float]] = deque(maxlen=MAX_TICKS)
        # candles: dicts {t,o,h,l,c,tc} (tc = tick count in candle)
        self.candles: Deque[dict] = deque(maxlen=MAX_CANDLES)

        # tick cadence (server ts resolution may be 1s → count per sec)
        self._sec_counts: Deque[Tuple[int, int]] = deque(maxlen=4000)

        # repetition search: EXACT quantized return k-tuples.
        # Direction-only patterns (3^k space) collide by birthday paradox
        # and prove nothing — exact return tuples are effectively unique
        # (chance collision ≈ n²/2m ≈ 0.2 for n=40k, m≈4.7e9).
        self._ret_seq: Deque[int] = deque(maxlen=REP_K)
        self._tick_patterns: Counter = Counter()
        self._tick_pattern_total = 0
        self._tick_pattern_dupes = 0
        # candle color sequences
        self._col_seq: Deque[int] = deque(maxlen=REP_K_CANDLE)
        self._candle_patterns: Counter = Counter()
        self._candle_pattern_total = 0

        # True only when tick timestamps are REAL arrival times.  --db mode
        # synthesizes timestamps from ticks_json (prices only) → cadence
        # axes must be suppressed there.
        self.cadence_reliable = True

        # learned price grid (pip size) — needed for exact return tuples
        self._grid = 0.0
        self._grid_probe: Deque[float] = deque(maxlen=3000)

        self.last_update = 0.0

    # ── ingestion ────────────────────────────────────────────────────

    def ingest_tick(self, ts: float, price: float) -> None:
        try:
            ts = float(ts)
            price = float(price)
        except (TypeError, ValueError):
            return
        if not (ts > 0 and price > 0):
            return
        with self._lock:
            # learn grid from early ticks
            if self._grid_probe is not None and len(self._grid_probe) < 3000:
                self._grid_probe.append(price)
                if len(self._grid_probe) >= 800 and self._grid == 0.0:
                    self._grid = _price_grid(list(self._grid_probe))
            prev = self.ticks[-1] if self.ticks else None
            self.ticks.append((ts, price))
            if prev is not None:
                # exact grid-unit return (None until grid is learned)
                if self._grid > 0:
                    r = int(round((price - prev[1]) / self._grid))
                    self._ret_seq.append(r)
                    if len(self._ret_seq) == REP_K:
                        key = tuple(self._ret_seq)
                        cnt = self._tick_patterns[key] = \
                            self._tick_patterns[key] + 1
                        self._tick_pattern_total += 1
                        if cnt == 2:
                            self._tick_pattern_dupes += 1  # first repeat
                        elif cnt > 2:
                            self._tick_pattern_dupes += 1
            # ticks per second (server ts truncated to second)
            sec = int(ts)
            if self._sec_counts and self._sec_counts[-1][0] == sec:
                self._sec_counts[-1] = (sec, self._sec_counts[-1][1] + 1)
            else:
                self._sec_counts.append((sec, 1))
            self.last_update = time.time()

    def ingest_candle(self, t: float, o: float, h: float, l: float,
                      c: float, tick_count: Optional[int] = None) -> None:
        try:
            row = {"t": float(t), "o": float(o), "h": float(h),
                   "l": float(l), "c": float(c),
                   "tc": int(tick_count) if tick_count else None}
        except (TypeError, ValueError):
            return
        with self._lock:
            self.candles.append(row)
            col = 1 if c > o else (-1 if c < o else 0)
            self._col_seq.append(col)
            if len(self._col_seq) == REP_K_CANDLE:
                key = "".join(str(x) for x in self._col_seq)
                self._candle_patterns[key] += 1
                self._candle_pattern_total += 1
            self.last_update = time.time()

    def ingest_candles(self, rows: List[dict]) -> None:
        for r in sorted(rows, key=lambda x: x.get("t", x.get("time", 0))):
            t = r.get("t", r.get("time"))
            tc = r.get("tc", r.get("tick_count"))
            self.ingest_candle(t, r.get("o", r.get("open")),
                               r.get("h", r.get("high")),
                               r.get("l", r.get("low")),
                               r.get("c", r.get("close")), tc)

    # ── analysis ─────────────────────────────────────────────────────

    def report(self) -> dict:
        with self._lock:
            ticks = list(self.ticks)
            candles = list(self.candles)

        out: dict = {
            "asset": self.asset,
            "ticks": len(ticks),
            "candles": len(candles),
            "ready": len(ticks) >= MIN_TICKS_FOR_REPORT or
                     len(candles) >= MIN_CANDLES_FOR_REPORT,
            "metrics": {},
        }
        if not out["ready"]:
            return out

        m = out["metrics"]

        # 1) tick cadence — inter-tick gaps where resolution allows
        # (skipped when timestamps were synthesized in --db mode)
        if len(ticks) >= 200 and self.cadence_reliable:
            gaps = [b[0] - a[0] for a, b in zip(ticks, ticks[1:]) if 0 < b[0] - a[0] < 30]
            per_sec = [c for _, c in self._sec_counts]
            # cadence regularity: CV of per-second counts + CV of gaps
            m["tick_gap_cv"] = round(_cv(gaps), 4) if gaps else None
            m["ticks_per_sec_cv"] = round(_cv(per_sec), 4) if per_sec else None
            m["ticks_per_sec_mean"] = round(_mean(per_sec), 3) if per_sec else None
            m["tick_rate"] = round(len(ticks) /
                                   max(1e-9, ticks[-1][0] - ticks[0][0]), 3)

        # 2) ticks per candle (only candles with known tc)
        tcs = [c["tc"] for c in candles if c.get("tc")]
        if len(tcs) >= 50:
            m["tpc_mean"] = round(_mean(tcs), 1)
            m["tpc_cv"] = round(_cv(tcs), 4)
            m["tpc_min"] = min(tcs)
            m["tpc_max"] = max(tcs)

        # 3) price grid
        if len(ticks) >= 500:
            grid = _price_grid([p for _, p in ticks[:2000]])
            m["price_grid"] = grid
            on_grid = 0
            tot = 0
            for _, p in ticks[:2000]:
                q = p / grid if grid else 0
                tot += 1
                if abs(q - round(q)) < 1e-6:
                    on_grid += 1
            m["grid_adherence"] = round(on_grid / tot, 4) if tot else None

        # 4) increment shape (tick returns in grid units)
        # NOTE: needs >= 2000 nonzero returns — validated low-sample kurt is
        # noise (a handful of spikes at 2.5k ticks pushed kurt to 7+ and
        # flipped the verdict — NZDCAD demo case 2026-09-29).
        if len(ticks) >= 500 and m.get("price_grid"):
            g = m["price_grid"]
            rets = [(b[1] - a[1]) / g for a, b in zip(ticks, ticks[1:])][:4000]
            rets = [r for r in rets if r != 0]
            if len(rets) >= 2000:
                m["ret_kurtosis"] = round(_kurtosis(rets), 2)
                m["ret_std"] = round(_std(rets), 3)
                m["ret_ac1"] = round(_autocorr(rets, 1), 4)

        # 5) variance ratio on candle close log-returns
        closes = [c["c"] for c in candles if c["c"] > 0]
        if len(closes) >= 200:
            lrets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
            vr2 = _variance_ratio(lrets, 2)
            vr5 = _variance_ratio(lrets, 5)
            vr30 = _variance_ratio(lrets, 30)
            if vr2 is not None:
                m["vr_q2"] = round(vr2, 3)
            if vr5 is not None:
                m["vr_q5"] = round(vr5, 3)
            if vr30 is not None:
                m["vr_q30"] = round(vr30, 3)
            # VR by halves (regime mixing check)
            half = len(lrets) // 2
            vr_h1 = _variance_ratio(lrets[:half], 5)
            vr_h2 = _variance_ratio(lrets[half:], 5)
            if vr_h1 is not None and vr_h2 is not None:
                m["vr_regime_delta"] = round(abs(vr_h1 - vr_h2), 3)

        # 6) Hurst
        if len(closes) >= 256:
            h = _hurst_rs(closes)
            if h is not None:
                m["hurst"] = round(h, 3)

        # 7) direction memory on candle colors
        cols = [1 if c["c"] > c["o"] else (-1 if c["c"] < c["o"] else 0)
                for c in candles]
        nz = [x for x in cols if x != 0]
        if len(nz) >= 100:
            m["color_ac1"] = round(_autocorr(nz, 1), 4)
            m["color_ac2"] = round(_autocorr(nz, 2), 4)
            # streak distribution vs geometric expectation
            streaks = []
            cur = nz[0]
            run = 1
            for x in nz[1:]:
                if x == cur:
                    run += 1
                else:
                    streaks.append(run)
                    cur = x
                    run = 1
            streaks.append(run)
            p_switch = 1.0 - (sum(1 for a, b in zip(nz, nz[1:]) if a == b) /
                              max(1, len(nz) - 1))
            # expected mean streak under memoryless switches = 1/p
            m["streak_mean"] = round(_mean(streaks), 3)
            m["streak_expected_memoryless"] = round(1.0 / p_switch, 3) if p_switch > 0 else None

        # 8) repetition (pre-generated block library signature)
        # exact return k-tuples: chance collisions ≈ n²/2m ≈ 0.2 expected
        # for n=40k over m≈4.7e9 grid-unit space → ANY meaningful dupe
        # count = replayed library
        if self._tick_pattern_total >= 500:
            total = self._tick_pattern_total
            dupes = self._tick_pattern_dupes
            m["tick_return_dupe_count"] = dupes
            m["tick_pattern_top"] = [
                (list(k), v) for k, v in self._tick_patterns.most_common(3)]
        if self._candle_pattern_total >= 300:
            total = self._candle_pattern_total
            dup = sum(v - 1 for v in self._candle_patterns.values() if v > 1)
            m["candle_pattern_dup_rate"] = round(dup / total, 4)
            # color-pattern transition edge for CSE (2-candle basis)
            pair_counts = Counter()
            for i in range(len(cols) - 1):
                if cols[i] != 0 and cols[i + 1] != 0:
                    pair_counts[(cols[i], cols[i + 1])] += 1
            tot_pairs = sum(pair_counts.values())
            if tot_pairs >= 100:
                cont = pair_counts[(1, 1)] + pair_counts[(-1, -1)]
                m["color_continuation_rate"] = round(cont / tot_pairs, 4)

        # 9) weekend continuity (multi-day candle history)
        # NOTE: 24/7 grids are normal for CRYPTO — this axis is synthetic
        # evidence ONLY for _otc / FX-style assets.  Validation caught the
        # false positive on BTCUSDT.
        if len(candles) >= 1000:
            secs = [c["t"] for c in candles]
            days = {time.gmtime(s).tm_wday for s in secs}
            has_weekend = 5 in days or 6 in days
            if has_weekend:
                ts = sorted(secs)
                gaps = [b - a for a, b in zip(ts, ts[1:])]
                m["weekend_present"] = True
                m["max_gap_minutes"] = round(max(gaps) / 60.0, 1) if gaps else None
            else:
                m["weekend_present"] = False
                m["weekend_gap"] = True

        # 10) hourly sigma schedule
        if len(candles) >= 720:
            by_hour: Dict[int, List[float]] = {}
            for c in candles:
                lr = abs(math.log(c["c"] / c["o"])) if c["o"] > 0 and c["c"] > 0 else 0.0
                by_hour.setdefault(time.gmtime(c["t"]).tm_hour, []).append(lr)
            hmeans = {h: _mean(v) for h, v in by_hour.items() if len(v) >= 10}
            if len(hmeans) >= 6:
                vals = list(hmeans.values())
                m["hourly_sigma_cv"] = round(_cv(vals), 3)
                m["hourly_sigma_ratio"] = round(max(vals) / max(1e-12, min(vals)), 2)

        out["verdict"] = self._verdict(m, out)
        return out

    # ── verdict ──────────────────────────────────────────────────────

    def _verdict(self, m: dict, out: dict) -> dict:
        """Combine axes → classification + predictability."""
        synthetic_evidence = 0.0
        real_evidence = 0.0
        notes: List[str] = []
        is_otc = "_otc" in self.asset.lower() or "otc" in self.asset.lower()

        # cadence
        tpc_cv = m.get("tpc_cv")
        if tpc_cv is not None:
            if tpc_cv < 0.18:
                synthetic_evidence += 2.0
                notes.append(f"tick density very regular (CV={tpc_cv}) — fixed-cadence generator")
            elif tpc_cv > 0.45:
                real_evidence += 2.0
                notes.append(f"tick density irregular (CV={tpc_cv}) — trade-driven")
        tps_cv = m.get("ticks_per_sec_cv")
        if tps_cv is not None and tps_cv < 0.10:
            synthetic_evidence += 1.5
            notes.append(f"per-second tick count near-constant (CV={tps_cv}) — metronome feed")

        # increment shape
        k = m.get("ret_kurtosis")
        gap_cv = m.get("tick_gap_cv")
        tps_cv = m.get("ticks_per_sec_cv")
        # metronome check FIRST: a fixed-cadence feed with fat tails is a
        # generator doing DESIGNED spike/wick mimicry (stop-hunt flavor —
        # common in OTC-style generators, ours does it too), NOT real order
        # flow.  Real trade-driven ticks are irregular AND fat-tailed.
        metronome = (gap_cv is not None and gap_cv < 0.40) or \
                    (tps_cv is not None and tps_cv < 0.40)
        if k is not None:
            if metronome:
                if k < 1.0:
                    synthetic_evidence += 1.5
                    notes.append(f"tick returns near-Gaussian (kurt={k}) on a metronome cadence — RNG output")
                elif k > 5.0:
                    synthetic_evidence += 0.5
                    notes.append(f"fat tails (kurt={k}) ON a metronome cadence — designed spike/wick mimicry, not order flow")
                else:
                    synthetic_evidence += 1.0
                    notes.append(f"tick return shape kurt={k} on a metronome cadence — synthetic feed")
            else:
                if k < 1.0:
                    synthetic_evidence += 1.5
                    notes.append(f"tick returns near-Gaussian (kurt={k}) — RNG output")
                elif k > 5.0:
                    real_evidence += 2.0
                    notes.append(f"tick returns fat-tailed (kurt={k}) with irregular arrivals — real order flow")

        # variance ratio / hurst → process type
        vr = m.get("vr_q5")
        hurst = m.get("hurst")
        proc = "unknown"
        if vr is not None:
            if vr < 0.75:
                proc = "mean_reverting"
                synthetic_evidence += 1.0
                notes.append(f"VR(5)={vr} << 1 — mean-reverting process")
            elif vr > 1.25:
                proc = "trending"
                notes.append(f"VR(5)={vr} > 1 — trending process")
            else:
                proc = "random_walk"
                notes.append(f"VR(5)≈{vr} — random-walk-like")
        if hurst is not None:
            if hurst < 0.44:
                notes.append(f"Hurst={hurst} — anti-persistent")
                if proc == "unknown":
                    proc = "mean_reverting"
            elif hurst > 0.58:
                notes.append(f"Hurst={hurst} — persistent")
            else:
                notes.append(f"Hurst={hurst} — ≈random walk")

        # regime mixing
        rd = m.get("vr_regime_delta")
        regime_mix = rd is not None and rd > 0.35

        # repetition / pre-generated library (exact return tuples)
        dupes = m.get("tick_return_dupe_count")
        pre_gen = None
        if dupes is not None:
            # expected chance collisions for n samples over m≈4.7e9 space
            n = self._tick_pattern_total
            expected_chance = n * n / 9.4e9
            pre_gen = bool(dupes > max(3, 5 * expected_chance))
            if pre_gen:
                synthetic_evidence += 3.0
                notes.append(f"exact return-sequences repeat {dupes}× (chance expects ~{expected_chance:.1f}) — PRE-GENERATED BLOCK LIBRARY")
            else:
                notes.append(f"exact return-sequences near-unique ({dupes} dupes, chance ~{expected_chance:.1f}) — no fixed candle library, fresh generation")
        # candle color-pattern dup rate is NOT shown as evidence: at these
        # sample sizes (480 candles over 3^4=81 patterns) the birthday
        # paradox makes ~96% duplication NORMAL — it proves nothing.
        # The meaningful sequence stats are the 2-candle transitions above.

        # weekend
        if m.get("weekend_present") and m.get("max_gap_minutes") is not None:
            if m["max_gap_minutes"] < 90:
                if is_otc:
                    synthetic_evidence += 2.5
                    notes.append("weekend grid continuous on an FX-style pair — market never closes ⇒ synthetic feed")
                else:
                    notes.append("24/7 grid — normal for crypto; synthetic evidence only for FX/OTC pairs")
        elif m.get("weekend_present") is False:
            real_evidence += 1.0
            notes.append("no weekend data — market closes ⇒ not a 24/7 synthetic feed")

        # hourly schedule
        hc = m.get("hourly_sigma_cv")
        if hc is not None and hc < 0.25:
            synthetic_evidence += 1.0
            notes.append(f"hourly volatility near-flat (CV={hc}) — designed schedule, not crowd")

        # classification — tick axes decide real-vs-synthetic; without
        # them (history OHLC only) we can only classify the PROCESS.
        tick_axes = any(m.get(k) is not None for k in
                        ("tpc_cv", "ticks_per_sec_cv", "ret_kurtosis"))
        if not tick_axes:
            cls = f"OHLC_ONLY_{proc.upper()}" if proc != "unknown" else "INCONCLUSIVE"
            notes.append("no tick data — real-vs-synthetic undetermined; only the process is classified")
        elif real_evidence >= synthetic_evidence + 2.0:
            cls = "TRADE_DRIVEN_REAL"
        elif regime_mix and proc in ("mean_reverting", "trending"):
            # structure changes over time (trend ⇄ chop ⇄ revert) — the
            # signature of a regime-switching generator (OTC-style)
            cls = "SYNTHETIC_REGIME_MIX"
        elif proc == "mean_reverting":
            cls = "SYNTHETIC_MEAN_REVERT"
        elif proc == "trending":
            cls = "SYNTHETIC_TRENDING"
        elif proc == "random_walk":
            cls = "SYNTHETIC_RANDOM_WALK"
        else:
            cls = "INCONCLUSIVE"

        # predictability for a sequence model (CSE) — calibrated against
        # the CSE backtests: real BTC 1m ⇒ 52-54% WR (score ≤ 60),
        # mean-reverting synthetic ⇒ 66-77% WR (score 75+).
        score = 45.0
        if proc == "mean_reverting":
            score += 22.0
        if proc == "trending":
            score += 10.0
        if regime_mix:
            score += 5.0
        ca1 = m.get("color_ac1")
        if ca1 is not None:
            score += min(8.0, abs(ca1) * 100.0)
        cont = m.get("color_continuation_rate")
        if cont is not None:
            score += min(8.0, abs(cont - 0.5) * 100.0)
        if pre_gen:
            score = 95.0   # a block library is almost fully predictable
        score = max(0.0, min(100.0, score))

        return {
            "classification": cls,
            "synthetic_evidence": round(synthetic_evidence, 1),
            "real_evidence": round(real_evidence, 1),
            "process": proc,
            "regime_mixing": regime_mix,
            "pre_generated_blocks": pre_gen,
            "predictability_score": round(score),
            "notes": notes,
        }


# ═════════════════════════════════════════════════════════════════════
#  Engine: all assets + cross-asset sync
# ═════════════════════════════════════════════════════════════════════

class FingerprintEngine:
    """Registry of per-asset fingerprints + cross-asset sync detection."""

    def __init__(self):
        self.assets: Dict[str, AssetFingerprint] = {}
        self._lock = threading.Lock()
        # cross-asset: second → {asset: tick_count}.
        # FIX (12-b-found/12-d-diagnosed): per-stream tick batches arrive
        # OUT OF ORDER across streams, so a "merge only if last entry ==
        # sec" deque created DUPLICATE entries per second — inflating
        # len(data) and breaking the 80% presence threshold (sync was
        # always null).  A keyed map + ordered prune merges any arrival
        # order correctly.
        self._cross_map: Dict[int, Dict[str, int]] = {}
        self._cross_order: Deque[int] = deque()
        self._cross_max = 3600   # seconds kept (~1h)

    def fp(self, asset: str) -> AssetFingerprint:
        with self._lock:
            if asset not in self.assets:
                self.assets[asset] = AssetFingerprint(asset)
            return self.assets[asset]

    def ingest_tick(self, asset: str, ts: float, price: float) -> None:
        self.fp(asset).ingest_tick(ts, price)
        sec = int(float(ts))
        with self._lock:
            d = self._cross_map.get(sec)
            if d is None:
                d = self._cross_map[sec] = {}
                self._cross_order.append(sec)
                while len(self._cross_order) > self._cross_max:
                    old = self._cross_order.popleft()
                    self._cross_map.pop(old, None)
            d[asset] = d.get(asset, 0) + 1

    def ingest_candle(self, asset: str, row: dict) -> None:
        self.fp(asset).ingest_candle(
            row.get("t", row.get("time")), row.get("o", row.get("open")),
            row.get("h", row.get("high")), row.get("l", row.get("low")),
            row.get("c", row.get("close")), row.get("tc", row.get("tick_count")))

    def ingest_candles(self, asset: str, rows: List[dict]) -> None:
        self.fp(asset).ingest_candles(rows)

    def cross_asset_sync(self) -> Optional[dict]:
        """Do assets tick on the same seconds?  Shared engine signature."""
        with self._lock:
            data = [dict(d) for d in self._cross_map.values()]
        if len(data) < 300:
            return None
        # assets seen in >= 80% of seconds
        presence = Counter()
        for d in data:
            for a in d:
                presence[a] += 1
        common = [a for a, n in presence.items() if n >= 0.8 * len(data)]
        if len(common) < 2:
            return None
        # count seconds where >=2 common assets ticked together
        both = 0
        for d in data:
            k = sum(1 for a in common if d.get(a))
            if k >= 2:
                both += 1
        # independence baseline: P(A)·P(B) overlap
        pa = _mean([1.0 if d.get(common[0]) else 0.0 for d in data])
        pb = _mean([1.0 if d.get(common[1]) else 0.0 for d in data])
        indep = pa * pb
        obs = both / len(data)
        lift = (obs / indep) if indep > 0 else None
        # Saturated feeds (every asset ticks nearly every second, pa/pb>0.95)
        # make lift ≈ 1 uninformative — but saturation ITSELF is the shared
        # fixed-cadence engine signature (real multi-venue markets have
        # quiet seconds).  Flag metronome behavior in that case.
        saturated = pa > 0.95 and pb > 0.95
        if saturated:
            shared = bool(obs > 0.98)
            mode = "saturated-metronome"
        else:
            shared = bool(lift and lift > 1.5)
            mode = "co-tick-lift"
        return {
            "assets": common[:6],
            "seconds_sampled": len(data),
            "co_tick_rate": round(obs, 4),
            "independence_expected": round(indep, 4),
            "lift": round(lift, 2) if lift else None,
            "mode": mode,
            "shared_engine": shared,
        }

    def report(self, asset: Optional[str] = None) -> dict:
        if asset:
            return self.fp(asset).report()
        per_asset = {}
        with self._lock:
            names = list(self.assets.keys())
        for a in names:
            r = self.assets[a].report()
            if r.get("ready"):
                per_asset[a] = r
        return {
            "assets": per_asset,
            "cross_asset_sync": self.cross_asset_sync(),
            "generated_at": time.time(),
        }
