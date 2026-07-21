"""
Simulated Quotex Feed — replaces pyquotex with realistic tick generation.
Same message protocol as feed.py: snapshot, tick, eoc.
When pyquotex is available, the real feed.py takes over.
"""
import asyncio
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field

import db as _db
from analyze_eoc import _round_level, _key_levels, _atr

PAYOUT_FLOOR = int(os.environ.get("QX_PAYOUT_FLOOR", "81"))
# FIX (2026-07-17): split payout floors for REAL vs OTC. Real pairs have
# lower broker margins → lower payouts → lower floor (default 70).
# OTC pairs have higher headline payouts → higher floor (default 85).
PAYOUT_FLOOR_REAL = int(os.environ.get("QX_PAYOUT_FLOOR_REAL", "70"))
PAYOUT_FLOOR_OTC  = int(os.environ.get("QX_PAYOUT_FLOOR_OTC",
                                       os.environ.get("QX_PAYOUT_FLOOR", "85")))
ENABLE_LIVE_THEORY = os.environ.get("ENABLE_LIVE_REEVAL", "1") == "1"
ENABLE_STRENGTH_GATE = os.environ.get("ENABLE_STRENGTH_GATE", "1") == "1"
# Signal delay: withhold prediction for N seconds after candle open so
# opening ticks can confirm gap direction. Restored to 3.0s (2026-07-17
# bug-fix audit) — was 0.0, causing predictions to fire on open price
# without any opening-tick confirmation.
SIGNAL_DELAY_SEC = float(os.environ.get("SIGNAL_DELAY_SEC", "3.0"))
ZONE_LOSS_GUARD = 3

# FIX (2026-07-17): two separate pair lists for the 3-dot category menu.
# Real pairs (no _otc suffix) — simulated live market hours behavior.
# OTC pairs (_otc suffix) — simulated broker-generated feed.
_SIM_REAL_PAIRS = [
    {"asset": "EURUSD", "display": "EUR/USD", "status": "live", "payout": 82, "locked": False, "category": "real"},
    {"asset": "GBPUSD", "display": "GBP/USD", "status": "live", "payout": 80, "locked": False, "category": "real"},
    {"asset": "USDJPY", "display": "USD/JPY", "status": "live", "payout": 78, "locked": False, "category": "real"},
    {"asset": "AUDUSD", "display": "AUD/USD", "status": "live", "payout": 75, "locked": False, "category": "real"},
    {"asset": "USDCAD", "display": "USD/CAD", "status": "live", "payout": 76, "locked": False, "category": "real"},
    {"asset": "NZDUSD", "display": "NZD/USD", "status": "live", "payout": 72, "locked": False, "category": "real"},
    {"asset": "USDCHF", "display": "USD/CHF", "status": "live", "payout": 74, "locked": False, "category": "real"},
    {"asset": "EURJPY", "display": "EUR/JPY", "status": "live", "payout": 79, "locked": False, "category": "real"},
    {"asset": "GBPJPY", "display": "GBP/JPY", "status": "live", "payout": 81, "locked": False, "category": "real"},
    {"asset": "EURGBP", "display": "EUR/GBP", "status": "live", "payout": 73, "locked": False, "category": "real"},
]

_SIM_OTC_PAIRS = [
    {"asset": "EURUSD_otc", "display": "EUR/USD", "status": "otc", "payout": 87, "locked": False, "category": "otc"},
    {"asset": "GBPUSD_otc", "display": "GBP/USD", "status": "otc", "payout": 85, "locked": False, "category": "otc"},
    {"asset": "USDJPY_otc", "display": "USD/JPY", "status": "otc", "payout": 83, "locked": False, "category": "otc"},
    {"asset": "AUDUSD_otc", "display": "AUD/USD", "status": "otc", "payout": 82, "locked": False, "category": "otc"},
    {"asset": "EURGBP_otc", "display": "EUR/GBP", "status": "otc", "payout": 84, "locked": False, "category": "otc"},
    {"asset": "GBPJPY_otc", "display": "GBP/JPY", "status": "otc", "payout": 86, "locked": False, "category": "otc"},
    {"asset": "EURJPY_otc", "display": "EUR/JPY", "status": "otc", "payout": 82, "locked": False, "category": "otc"},
    {"asset": "NZDUSD_otc", "display": "NZD/USD", "status": "otc", "payout": 80, "locked": True,  "category": "otc"},
    {"asset": "USDCAD_otc", "display": "USD/CAD", "status": "otc", "payout": 83, "locked": False, "category": "otc"},
    {"asset": "EURCHF_otc", "display": "EUR/CHF", "status": "otc", "payout": 81, "locked": False, "category": "otc"},
]

# Combined backward-compat list (real first, then otc) — matches feed.py
_SIM_PAIRS = _SIM_REAL_PAIRS + _SIM_OTC_PAIRS

# Base prices for simulation
_BASE_PRICES = {
    "EURUSD_otc": 1.08450, "GBPUSD_otc": 1.27150, "USDJPY_otc": 161.250,
    "AUDUSD_otc": 0.67350, "EURGBP_otc": 0.85280, "GBPJPY_otc": 204.850,
    "EURJPY_otc": 174.850, "NZDUSD_otc": 0.61050, "USDCAD_otc": 1.36450,
    "EURCHF_otc": 0.94280,
    # Real-market base prices (slightly different from OTC to reflect
    # the live spread at the time the sim was started).
    "EURUSD": 1.08420, "GBPUSD": 1.27180, "USDJPY": 161.180,
    "AUDUSD": 0.67320, "USDCAD": 1.36480, "NZDUSD": 0.61020,
    "USDCHF": 0.90420, "EURJPY": 174.820, "GBPJPY": 204.780,
    "EURGBP": 0.85250,
}

_PIP = {
    "EURUSD_otc": 0.0001, "GBPUSD_otc": 0.0001, "USDJPY_otc": 0.01,
    "AUDUSD_otc": 0.0001, "EURGBP_otc": 0.0001, "GBPJPY_otc": 0.01,
    "EURJPY_otc": 0.01, "NZDUSD_otc": 0.0001, "USDCAD_otc": 0.0001,
    "EURCHF_otc": 0.0001,
    # Real pairs use the same pip sizes as their OTC twins.
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "AUDUSD": 0.0001, "USDCAD": 0.0001, "NZDUSD": 0.0001,
    "USDCHF": 0.0001, "EURJPY": 0.01, "GBPJPY": 0.01,
    "EURGBP": 0.0001,
}


@dataclass
class _AssetStream:
    asset: str
    period: int
    candles: list = field(default_factory=list)
    ticks: deque = field(default_factory=lambda: deque(maxlen=500))
    candle_open_time: int = 0
    candle_open_price: float = 0.0
    candle_open_is_real: bool = False
    last_tick_ts: float = 0.0
    last_real_tick_wall: float = 0.0
    prediction: dict | None = None
    zone_streak: dict = field(default_factory=lambda: {"regime": None, "zone": None, "losses": 0})
    payout: int | None = None
    task: object = None
    always_on: bool = False
    interested_cids: set = field(default_factory=set)
    idle_since: float | None = None
    created_at: float = field(default_factory=time.time)
    base_candles: list = field(default_factory=list)
    base_ticks: list = field(default_factory=list)
    _live_reeval_ticks: int = 0
    # Last-10s optimization (2026-07-10)
    cached_accuracy: tuple = field(default_factory=lambda: (None, 0))
    # FIX (BUG-4, 2026-07-18): live_signal_history was being assigned but
    # never declared on the dataclass — would AttributeError on first
    # LIVE re-eval. Declared here as a proper field.
    live_signal_history: list = field(default_factory=list)
    # Signal delay (2026-07-10)
    signal_delay_until: float = 0.0
    # BRAIN-LEARNED: loss cluster cooldown fields
    _consecutive_losses: int = 0
    _loss_cooldown_until: float = 0.0
    # Sim-specific
    _sim_price: float = 0.0
    _sim_momentum: float = 0.0
    _sim_volatility: float = 0.0
    # FIX (BUG-4, 2026-07-18): removed dead fields `inverted`,
    # `cached_accuracy_at`, and `live_signal_history` — these were
    # carry-overs from older feed.py that sim_feed.py never properly
    # used. `inverted` was set from `result.get("_flipped")` which the
    # engine never emits; `cached_accuracy_at` was set but never read;
    # `live_signal_history` was never declared on the dataclass (would
    # AttributeError if LIVE re-eval ran before _run_eoc set it).


class QuotexFeed:
    def __init__(self):
        """Initialize simulated feed with default Real + OTC pair lists."""
        self._connected = False
        self._broadcast = None
        self._streams: dict[tuple[str, int], _AssetStream] = {}
        self._max_streams = 45
        # FIX (AUDIT-ENGINES #5, 2026-07-19): per-key lock for ensure_stream
        # to prevent the race condition where concurrent callers both create
        # a stream for the same (asset, period) and orphan the first task.
        self._stream_locks: dict[tuple[str, int], asyncio.Lock] = {}
        # FIX (2026-07-17): split pair lists by category. _pairs_list kept
        # as combined backward-compat (real first, then otc).
        self._pairs_list = list(_SIM_PAIRS)
        self._real_pairs_list = list(_SIM_REAL_PAIRS)
        self._otc_pairs_list  = list(_SIM_OTC_PAIRS)
        # NOTE (refactor 2026-07-14): `_muted_theories` + `_last_perf_refresh`
        # removed — the prediction engine is candle_reaction (no theories).
        self._last_db_cleanup = 0.0
        self._last_pairs_refresh = 0.0

    def available_pairs(self) -> dict:
        """Return Real + OTC pair lists with their respective payout floors.

        Matches feed.py's available_pairs() structure so the frontend
        doesn't care which feed (real or sim) it's talking to.

        FIX (2026-07-17): only pairs with status="live" (real) or "otc"
        are included in the active lists. Closed pairs are filtered out
        so the UI dropdown only shows currently-tradeable pairs. The
        full lists (including closed) are kept as `_real_pairs_list` /
        `_otc_pairs_list` instance attrs for debugging — call those
        directly if you need to see closed pairs.
        """
        active_real = [p for p in self._real_pairs_list if p["status"] == "live"]
        active_otc  = [p for p in self._otc_pairs_list  if p["status"] == "otc"]
        return {
            "real_pairs": active_real,
            "otc_pairs":  active_otc,
            "payout_floor_real": PAYOUT_FLOOR_REAL,
            "payout_floor_otc":  PAYOUT_FLOOR_OTC,
            # Backward compat
            "pairs":        active_real + active_otc,
            "payout_floor": PAYOUT_FLOOR_OTC,
        }

    def snapshot(self, asset: str, period: int) -> dict | None:
        """Return current candle history + prediction for an asset."""
        stream = self._streams.get((asset, period))
        if not stream or not stream.candles:
            return None
        return {
            "type": "snapshot", "asset": stream.asset, "period": stream.period,
            "candles": stream.candles[-300:], "prediction": stream.prediction,
        }

    async def ensure_stream(self, asset: str, period: int, cid: str | None = None) -> dict:
        """Start a sim stream for (asset, period) and register the viewer cid.

        If the stream already exists, just add the cid to interested_cids
        (and remove it from any other stream). Otherwise create the stream,
        generate history, and launch the _run_stream task.

        FIX (AUDIT-ENGINES #4 + #5, 2026-07-19):
          - _max_streams=45 was set but never enforced. Now enforced.
          - Race condition: concurrent calls for the same (asset, period)
            both passed the `stream is None` check, both created streams,
            both spawned _run_stream tasks. The second overwrote the
            first in _streams, orphaning the first task forever. Now
            guarded by an asyncio.Lock per (asset, period) — concurrent
            callers await the same lock, the second sees the stream the
            first created.
        """
        key = (asset, period)
        # Capacity gate (BUG #4): reject new streams beyond _max_streams.
        # Existing streams for the same key still pass through (viewer
        # joining an active stream doesn't create a new one).
        if key not in self._streams and len(self._streams) >= self._max_streams:
            return {"ok": False, "status": "capacity",
                    "reason": f"max {self._max_streams} streams reached"}

        # Race-condition guard (BUG #5): per-key lock ensures only one
        # caller creates the stream; concurrent callers wait, then see
        # the stream the first caller created.
        lock = self._stream_locks.setdefault(key, asyncio.Lock())
        async with lock:
            stream = self._streams.get(key)
            if stream is not None:
                if cid:
                    stream.interested_cids.add(cid)
                    for k, s in self._streams.items():
                        if k != key:
                            s.interested_cids.discard(cid)
                stream.idle_since = None
                # Signal delay (2026-07-10): a joiner inside the opening-tick
                # confirmation window gets prediction=None (PENDING), same gate
                # a live viewer would see — mirrors feed.py.
                gated_prediction = stream.prediction
                if (stream.signal_delay_until > 0
                        and time.time() < stream.signal_delay_until):
                    gated_prediction = None
                return {"type": "snapshot", "ok": True, "status": "streaming",
                        "asset": asset, "period": period,
                        "candles": stream.candles[-300:], "prediction": gated_prediction}

            pair = next((p for p in self._pairs_list if p["asset"] == asset), None)
            if pair and pair.get("locked"):
                # FIX (2026-07-17): category-specific payout floor in the error
                # message — real pairs need >= PAYOUT_FLOOR_REAL (70), OTC pairs
                # need >= PAYOUT_FLOOR_OTC (85).
                floor = PAYOUT_FLOOR_OTC if asset.endswith("_otc") else PAYOUT_FLOOR_REAL
                return {"ok": False, "status": "locked", "payout": pair.get("payout"),
                        "reason": f"Needs {floor}% payout "
                                  f"(currently {pair.get('payout', '?')}%)"}

            stream = _AssetStream(asset=asset, period=period)
            if cid:
                stream.interested_cids.add(cid)
            self._streams[key] = stream
            stream.task = asyncio.create_task(self._run_stream(stream))
            return {"ok": True, "status": "starting"}

    async def drop_interest(self, cid: str) -> None:
        """Remove a viewer from all streams (on WS disconnect)."""
        for s in self._streams.values():
            s.interested_cids.discard(cid)

    def stream_status(self) -> dict:
        """Return active stream count + capacity info."""
        now = time.time()
        return {
            "active": [{"asset": s.asset, "period": s.period,
                        "viewers": len(s.interested_cids),
                        "age_sec": round(now - s.created_at)}
                       for s in self._streams.values()],
            "count": len(self._streams), "max": self._max_streams,
            "cooldown_until": None, "cooldown_reason": None,
        }

    async def shutdown(self) -> None:
        """Cancel all stream tasks (on server shutdown)."""
        for s in list(self._streams.values()):
            if s.task:
                s.task.cancel()

    # ── Simulated history generation ────────────────────────────────────────

    def _gen_history(self, asset: str, period: int = 60, n: int = 120) -> list[dict]:
        """Generate realistic candle history.

        FIX (2026-07-13): period was hardcoded to 60 — for 5-minute streams
        (period=300), history candles were 60s apart but the stream loop
        floors to 300s, causing a spurious candle-close on iteration 1 and
        misaligned timestamps for the rest of the stream.

        FIX (2026-07-18, chart-crash bug): previously history used
        ``volatility = pip * 1.5-6.0`` per candle with intra-candle wick
        ``volatility * 0.3``, giving ranges of 3-9 pip. But the live tick
        generator (_gen_tick) uses per-tick vol of ~0.3 pip at 10 ticks/sec
        for 60 sec = 600 ticks, producing expected range of ~29 pip. The
        10x mismatch made the running candle look like a giant spike next
        to small history candles. Now history volatility is scaled to
        match the tick-based volatility so ranges are consistent.
        """
        base = _BASE_PRICES.get(asset, 1.0)
        pip = _PIP.get(asset, 0.0001)
        now = int(time.time())
        # Floor `now` to the period boundary so the last history candle's
        # time is aligned with the stream loop's `_floor_to_period(now, period)`.
        now = (now // period) * period
        start = now - n * period
        candles = []
        price = base
        # Slight trend bias
        trend = random.uniform(-0.3, 0.3) * pip

        # FIX (2026-07-18): Scale volatility by sqrt(period/60) so longer
        # periods (5min, 15min) have proportionally larger ranges. Base
        # volatility is calibrated to produce ~8-20 pip ranges for 1-min
        # forex candles, matching what the tick generator produces.
        period_scale = (period / 60.0) ** 0.5

        for i in range(n):
            t = start + i * period
            # FIX: increased volatility from 1.5-6.0 to 4.0-10.0 to match
            # tick-based realized volatility.
            volatility = pip * random.uniform(4.0, 10.0) * period_scale
            # Random walk with mean reversion
            change = random.gauss(trend, volatility)
            # Occasional bigger moves
            if random.random() < 0.08:
                change *= random.uniform(2, 4)
            o = price
            # Intra-candle path — wick is 40% of volatility (was 30%)
            # to better match the tick-based high/low spread.
            hi = max(o, o + change) + abs(random.gauss(0, volatility * 0.4))
            lo = min(o, o + change) - abs(random.gauss(0, volatility * 0.4))
            c = o + change
            # Mean reversion toward base
            if abs(c - base) > pip * 20:
                change -= (c - base) * 0.05
                c = o + change
            c = round(c, 5)
            o = round(o, 5)
            hi = round(hi, 5)
            lo = round(lo, 5)
            # Safety: ensure high >= max(open, close) and low <= min(open, close)
            hi = max(hi, o, c)
            lo = min(lo, o, c)
            candles.append({"time": t, "open": o, "high": hi, "low": lo, "close": c})
            price = c
            # Slowly drift trend
            trend += random.gauss(0, pip * 0.1)
            trend = max(-pip * 0.5, min(pip * 0.5, trend))

        return candles

    def _gen_tick(self, stream: _AssetStream) -> float:
        """Generate next realistic tick price.

        FIX (2026-07-17): Real-market pairs trend harder (less mean
        reversion), OTC pairs mean-revert more (broker's algorithm
        suppresses trends). The mean-reversion factor is now category-
        dependent:
          - Real pairs: 0.001 (weak reversion — trends persist)
          - OTC pairs:  0.003 (strong reversion — broker pulls back)

        FIX (2026-07-18, chart-crash bug): reduced per-tick volatility
        from 0.3 pip to 0.1 pip. With ~10 ticks/sec for 60 sec = 600
        ticks, the old setting produced expected range of ~29 pip per
        1-min candle — way too volatile and inconsistent with the history
        candle generator. New setting produces ~10 pip range, matching
        realistic 1-min EURUSD volatility and the updated history gen.
        Spike probability also reduced from 0.8% to 0.3% (was producing
        ~5 spikes per minute = unrealistic noise).
        """
        pip = _PIP.get(stream.asset, 0.0001)
        price = stream._sim_price

        # Momentum: tends to continue in same direction (reduced from 0.15)
        stream._sim_momentum *= 0.97  # decay
        stream._sim_momentum += random.gauss(0, pip * 0.05)

        # Volatility clustering (reduced from 0.3 to 0.1)
        stream._sim_volatility = stream._sim_volatility * 0.95 + abs(random.gauss(0, pip * 0.1)) * 0.05
        # FIX: re-seed if volatility decayed too low (steady-state now ~8%)
        if stream._sim_volatility < pip * 0.1:
            stream._sim_volatility = pip * random.uniform(0.15, 0.4)
        vol = max(pip * 0.08, stream._sim_volatility)

        # Mean reversion — category-dependent.
        # Real markets: weaker reversion (trends driven by real order flow)
        # OTC markets: stronger reversion (broker algorithm suppresses trends)
        base = _BASE_PRICES.get(stream.asset, 1.0)
        is_otc = stream.asset.endswith("_otc")
        reversion_factor = 0.003 if is_otc else 0.001
        reversion = (base - price) * reversion_factor

        change = stream._sim_momentum + random.gauss(0, vol) + reversion

        # Occasional spikes — reduced probability (was 0.5%/0.8%, now 0.2%/0.3%)
        # and reduced magnitude (was pip*3, now pip*1.5)
        spike_prob = 0.002 if not is_otc else 0.003
        if random.random() < spike_prob:
            change += random.gauss(0, pip * 1.5)

        new_price = price + change
        new_price = round(new_price, 5)
        stream._sim_price = new_price
        return new_price

    # ── Microstructure analysis (same as real feed.py) ─────────────────────

    def _analyze_microstructure(self, ticks, open_price):
        ticks = list(ticks)
        if len(ticks) < 3:
            return None
        if len(ticks) < 10:
            # Return minimal micro for early ticks so frontend doesn't show loading
            cur = ticks[-1]
            op = open_price
            return {
                "buy_pct": 50, "sell_pct": 50, "pressure": "FIGHT",
                "is_fight": False, "crosses": 0,
                "hold_price": cur, "hold_visits": len(ticks),
                "phases": ["FLAT"], "reaction": None,
                "net": round(cur - op, 6), "tick_count": len(ticks),
                "last_react": None, "round": {},
                "ending_direction": {"direction": "FLAT", "buy_pct": 50,
                                    "dominance": "FIGHT", "move": 0, "tick_count": len(ticks)},
            }
        op = open_price
        hi = max(ticks)
        lo = min(ticks)
        cur = ticks[-1]
        rng = hi - lo

        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        moves = up_t + dn_t
        buy_pct = round(up_t / moves * 100) if moves else 50
        sell_pct = 100 - buy_pct

        pressure = "BUYER" if buy_pct >= 62 else ("SELLER" if sell_pct >= 62 else "FIGHT")

        mid = (hi + lo) / 2
        crosses = sum(1 for i in range(1, len(ticks))
                      if (ticks[i - 1] < mid) != (ticks[i] < mid))
        is_fight = crosses >= 4

        hold_price = None
        hold_visits = 0
        if rng > 0:
            bin_size = rng / 8
            bins = {}
            for t in ticks:
                b = int((t - lo) / bin_size)
                bins[b] = bins.get(b, 0) + 1
            top_bin = max(bins, key=bins.get)
            hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
            hold_visits = bins[top_bin]
        else:
            hold_price = round(cur, 6)
            hold_visits = len(ticks)

        n = len(ticks)
        t3 = max(n // 3, 1)
        early = ticks[t3] - ticks[0]
        mid_m = ticks[2 * t3] - ticks[t3]
        late = ticks[-1] - ticks[2 * t3]

        def _dir(v):
            return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

        phases = [_dir(early), _dir(mid_m), _dir(late)]

        reaction = None
        if rng > 0:
            from_hi = (hi - cur) / rng
            from_lo = (cur - lo) / rng
            net = cur - op
            late_q = max(n // 4, 2)
            late_move = ticks[-1] - ticks[-late_q]
            if from_hi > 0.50 and late_move <= 0 and net < 0:
                reaction = "SELLER"
            elif from_lo > 0.50 and late_move >= 0 and net > 0:
                reaction = "BUYER"

        last_react = None
        if n >= 15:
            last_n2 = max(n // 6, 6)
            fin2 = ticks[-last_n2:]
            fi2_up = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i - 1])
            fi2_dn = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i - 1])
            fi2_tot = fi2_up + fi2_dn
            if fi2_tot >= 3:
                fbp2 = fi2_up / fi2_tot
                net_run = cur - op
                if net_run > 0:
                    if fbp2 <= 0.30 or (fi2_tot >= 5 and fbp2 >= 0.90):
                        last_react = "EXHAUST"
                    elif 0.55 <= fbp2 <= 0.85 and fi2_dn >= 2:
                        last_react = "RECOVERY"
                elif net_run < 0:
                    if fbp2 >= 0.70 or (fi2_tot >= 5 and fbp2 <= 0.10):
                        last_react = "EXHAUST"
                    elif 0.15 <= fbp2 <= 0.45 and fi2_up >= 2:
                        last_react = "RECOVERY"

        cur_lvl, _, cur_str = _round_level(cur)
        hi_lvl, _, hi_str = _round_level(hi)
        lo_lvl, _, lo_str = _round_level(lo)
        round_info = {
            "near_level": cur_lvl,
            "near_strength": cur_str,
            "hi_level": hi_lvl if hi_str in ("BIG", "MID") else None,
            "hi_strength": hi_str if hi_str in ("BIG", "MID") else None,
            "lo_level": lo_lvl if lo_str in ("BIG", "MID") else None,
            "lo_strength": lo_str if lo_str in ("BIG", "MID") else None,
        }

        return {
            "buy_pct": buy_pct, "sell_pct": sell_pct, "pressure": pressure,
            "is_fight": is_fight, "crosses": crosses,
            "hold_price": hold_price, "hold_visits": hold_visits,
            "phases": phases, "reaction": reaction,
            "net": round(cur - op, 6), "tick_count": n,
            "last_react": last_react, "round": round_info,
        }

    # ── EOC analysis (same logic as feed.py) ──────────────────────────────

    async def _analyze_core(self, asset, period, candles, ticks, running_ticks=None,
                      stream=None):
        """Analyze a candle for prediction + micro history.

        FIX (H2, 2026-07-19): converted from sync → async to match feed.py.
        All SQLite I/O is now wrapped in asyncio.to_thread() so the shared
        event loop is NOT blocked when ~20 always-on sim streams re-evaluate
        concurrently. Previously each _db.get_micro_history call blocked the
        loop for several ms, causing visible tick stutter under load.
        """
        if len(candles) < 5:
            return None, []
        # FIX M3 (2026-07-19): removed dead `acc, n_acc` block — fetched
        # but never used. Saves a wasted DB round-trip on every LIVE re-eval.
        # Wrap micro-history fetch in asyncio.to_thread (sqlite3 blocks).
        micro_hist = await asyncio.to_thread(
            _db.get_micro_history, asset, period, 5, candles[-1]["time"])
        # 6-MODULE ENGINE (2026-07-14)
        # Runs 6 independent modules + Smart Blender with per-pair adaptation.
        # FIX (2026-07-17): predict_from_candle now auto-routes to
        # engines.otc or engines.real based on asset name — sim pairs
        # follow the same path as real feed pairs. Real sim pairs (no _otc
        # suffix) get the trend-following REAL engine, OTC sim pairs get
        # the mean-reversion OTC engine.
        # FIX (HTF sim parity, 2026-07-19): sim_feed was passing the
        # default htf_trend="SIDEWAYS" — meaning HTF confluence was NEVER
        # applied in sim mode. Now we derive it from the sim's own 1m
        # candle buffer using the same graceful-degradation logic as
        # feed.py (>=9 5m closes with progressive threshold). Sim uses
        # synthetic candles with proper `time` fields, so the same
        # _aggregate_5m_closes helper works.
        from candle_reaction import predict_from_candle
        try:
            from feed import _aggregate_5m_closes, _ema_simple
            closes_5m_sim = _aggregate_5m_closes(candles[-105:] if len(candles) > 105 else candles, period)
            n5_sim = len(closes_5m_sim)
            if n5_sim >= 9:
                e9 = _ema_simple(closes_5m_sim, 9)
                e21 = _ema_simple(closes_5m_sim, 21)
                sep_sim = abs(e9 - e21) / e21 if e21 > 0 else 0
                thresh_sim = 0.0003 if n5_sim >= 21 else (0.0005 if n5_sim >= 14 else 0.0008)
                if e9 > e21 and sep_sim > thresh_sim:
                    htf_sim = "UPTREND"
                elif e9 < e21 and sep_sim > thresh_sim:
                    htf_sim = "DOWNTREND"
                else:
                    htf_sim = "SIDEWAYS"
            else:
                htf_sim = "SIDEWAYS"
        except Exception:
            htf_sim = "SIDEWAYS"
        _micro_for_pred = None
        if ticks and len(ticks) >= 10:
            # FIX (Bug 3, deep audit 2026-07-19): use core.microstructure.build_micro
            # directly (same as feed.py). The rich micro dict includes
            # `last_velocity` which the blender's exhaustion gate Check 3
            # reads via `micro.get("last_velocity")`. The previous call to
            # `_build_micro` from analyze_eoc returned the same dict via
            # the shim, but build_micro does NOT include `ending_direction`
            # which running_tick module needs. Fix: merge in ending_direction
            # from sim_feed's own _analyze_microstructure method.
            from core.microstructure import build_micro as _build_micro_for_pred
            _micro_for_pred = _build_micro_for_pred(
                ticks, candles[-1]["open"] if candles else ticks[0])
            if _micro_for_pred is not None:
                _sim_micro = self._analyze_microstructure(
                    ticks, candles[-1]["open"] if candles else ticks[0])
                if _sim_micro and "ending_direction" in _sim_micro:
                    _micro_for_pred["ending_direction"] = _sim_micro["ending_direction"]
        # FIX (BUG-I, 2026-07-20): pass recent_accuracy from stream cache
        # so the blender can apply accuracy-aware self-correction.
        _recent_acc = getattr(stream, 'cached_accuracy', None) if stream is not None else None
        result = predict_from_candle(candles, ticks=ticks, micro=_micro_for_pred,
                                     asset=asset, period=period, htf_trend=htf_sim,
                                     recent_accuracy=_recent_acc)
        return result, micro_hist

    async def _run_eoc(self, stream, actual_open=None):
        closed = stream.candles
        base_ticks = list(stream.ticks)

        # BRAIN-LEARNED: loss cluster cooldown — skip prediction if pair
        # is in cooldown after 5+ consecutive losses.
        try:
            cooldown_until = getattr(stream, '_loss_cooldown_until', 0)
            if cooldown_until and time.time() < cooldown_until:
                remaining = int((cooldown_until - time.time()) / 60)
                print(f"[sim] {stream.asset} in loss cooldown ({remaining} min remaining) — skipping prediction")
                stream.prediction = None
                return None
        except Exception:
            pass  # never let cooldown check break the feed
        # Refresh per-candle accuracy cache ONCE here (at candle open).
        # asyncio.to_thread: sqlite3 I/O would otherwise block the shared
        # event loop for every concurrent stream (2026-07-10).
        try:
            stream.cached_accuracy = await asyncio.to_thread(
                _db.recent_accuracy, stream.asset, stream.period, n=20)
        except Exception:
            stream.cached_accuracy = (None, 0)
        # Reset flip-suppression tracker for new candle.
        stream.live_signal_history = []
        result, micro_hist = await self._analyze_core(stream.asset, stream.period,
                                                  closed, base_ticks, stream=stream)
        if result is None:
            return None
        # FIX (BUG-4, 2026-07-18): removed `stream.inverted = result.get("_flipped")`
        # — the engine never emits an `_flipped` key, so this was always False
        # and the field itself was dead (removed from the dataclass).
        # FIX (2026-07-13): list(closed) makes a SHALLOW COPY — same bug as
        # feed.py had. Without this, base_candles aliases stream.candles and
        # the LIVE re-eval scores against the mutated list, not the snapshot.
        stream.base_candles = list(closed)
        stream.base_ticks = base_ticks
        stream._live_reeval_ticks = 0

        _reg = result.get("regime") or {}
        # FIX (BUG-2, 2026-07-18): use correct regime/zone keys (was
        # `_reg.get("trend")` which never existed — broke chop-guard).
        _regime = _reg.get("regime")
        if _reg.get("is_volatile"):
            _zone = "VOLATILE"
        elif _reg.get("is_trending"):
            _zone = "TREND"
        elif _reg.get("is_ranging"):
            _zone = "RANGE"
        else:
            _zone = "UNKNOWN"
        _key = (_regime, _zone)
        if (result["signal"] != "NEUTRAL"
                and _key == (stream.zone_streak["regime"], stream.zone_streak["zone"])
                and stream.zone_streak["losses"] >= ZONE_LOSS_GUARD):
            # FIX (BACKTEST-2026-07-21): mirror the feed.py fix — convert
            # to NEUTRAL instead of WEAK. Backtest showed WEAK signals win
            # only 4.2% of the time; skipping is +EV.
            _losses = stream.zone_streak['losses']
            result["signal"] = "NEUTRAL"
            result["strength"] = "NEUTRAL"
            result["confidence"] = 0
            result.setdefault("reasons", []).append(
                f"CHOP GUARD (BACKTEST-FIX): {_key[0]}/{_key[1]} wrong "
                f"{_losses}x running → NEUTRAL (skip). "
                f"Backtest: WEAK signals won 4.2% — skipping is +EV.")

        if result["signal"] == "NEUTRAL":
            return {**result, "candle": None, "payout": stream.payout}

        # Prediction candle
        last = closed[-1]
        op = actual_open if actual_open is not None else last["close"]
        atr = _atr(closed[-20:]) if len(closed) >= 20 else (last["high"] - last["low"]) or 0.0001
        t = last["time"] + stream.period
        body = atr * 0.45
        wick = atr * 0.25
        tail = atr * 0.15
        if result["signal"] == "CALL":
            candle = {"time": t, "open": op, "high": round(op + body + wick, 6),
                      "low": round(op - tail, 6), "close": round(op + body, 6)}
        else:
            candle = {"time": t, "open": op, "high": round(op + tail, 6),
                      "low": round(op - body - wick, 6), "close": round(op - body, 6)}
        return {**result, "candle": candle, "payout": stream.payout}

    def _accuracy(self, just_closed, pred):
        if not pred or pred["signal"] not in ("CALL", "PUT"):
            return None
        if just_closed["close"] == just_closed["open"]:
            return "draw"
        actual_up = just_closed["close"] > just_closed["open"]
        pred_up = pred["signal"] == "CALL"
        return "correct" if actual_up == pred_up else "wrong"

    def _grade_and_log(self, asset, period, closed, prediction, micro_snap, candles):
        accuracy = self._accuracy(closed, prediction)
        if not prediction:
            return accuracy
        try:
            import json as _json
            reasons = prediction.get("reasons", [])
            is_draw = closed["close"] == closed["open"]
            actual_up = closed["close"] > closed["open"]
            if not accuracy:
                return accuracy
            _reg = (prediction.get("regime") or {})
            # FIX (BUG-2, 2026-07-18): use correct keys (was `_reg.get("trend")`).
            regime = _reg.get("regime")
            if _reg.get("is_volatile"):
                zone = "VOLATILE"
            elif _reg.get("is_trending"):
                zone = "TREND"
            elif _reg.get("is_ranging"):
                zone = "RANGE"
            else:
                zone = "UNKNOWN"
            sig = prediction["signal"]

            # FIX (AUDIT-CORE #46 + #47, 2026-07-21): port full tag computation
            # and postmortem format from feed.py so sim mode produces the same
            # rich signal_log rows as real mode. Previously sim mode only
            # emitted the DRAW tag, missing NOISE_CANDLE / BIG_MOVE /
            # COUNTER_REGIME / WITH_REGIME / LATE_FLIP. The postmortem was
            # also bare (no actual direction, move magnitude, ATR %, or tags).
            move  = closed["close"] - closed["open"]
            c_rng = closed["high"] - closed["low"]
            _hist = candles[-11:-1] if len(candles) >= 11 else candles[:-1]
            atr   = (sum(x["high"] - x["low"] for x in _hist) / len(_hist)
                     if _hist else c_rng)
            tags = []
            if is_draw:
                tags.append("DRAW")
            if atr > 0 and c_rng < atr * 0.40:
                tags.append("NOISE_CANDLE")
            if atr > 0 and abs(move) >= atr * 0.80:
                tags.append("BIG_MOVE")
            if regime in ("TREND_UP", "TREND_DOWN"):
                if ((regime == "TREND_UP" and sig == "PUT") or
                        (regime == "TREND_DOWN" and sig == "CALL")):
                    tags.append("COUNTER_REGIME")
                elif ((regime == "TREND_UP" and sig == "CALL") or
                        (regime == "TREND_DOWN" and sig == "PUT")):
                    tags.append("WITH_REGIME")
            if micro_snap and micro_snap.get("last_react") == "EXHAUST":
                tags.append("LATE_FLIP")

            _atr_note = (f" ({abs(move) / atr * 100:.0f}% of ATR)"
                         if atr > 0 else "")
            _actual_lbl = ("FLAT" if is_draw
                           else "UP" if actual_up else "DOWN")
            pm = (
                f"{sig} s={prediction['score']:+d}"
                f" {prediction.get('strength')}"
                f" agree={prediction.get('agree')}"
                f" | actual {_actual_lbl}"
                f" move={move:+.5f}{_atr_note}"
                f" | {accuracy.upper()}"
                f" | regime {regime}/{zone}"
                f"{' | ' + ','.join(tags) if tags else ''}"
            )
            # Log ANY CALL/PUT signal so the history DB actually populates.
            if sig in ("CALL", "PUT"):
                _db.log_signal(asset, period, closed["time"], sig, prediction["score"],
                               prediction["confidence"], "",
                               _actual_lbl, accuracy,
                               strength=prediction.get("strength"),
                               agree=prediction.get("agree"),
                               reasons=_json.dumps(reasons),
                               a_open=closed["open"], a_close=closed["close"],
                               regime=regime, zone=zone,
                               tags=",".join(tags), postmortem=pm)
        except Exception as _e:
            print(f"[db] log_signal error: {_e}")
        return accuracy

    def _save_micro(self, asset, period, closed, micro_snap, candles, ticks):
        try:
            import json as _tick_json
            _gap_pct = 0.0
            _gap_type = "NONE"
            if len(candles) >= 2:
                _pc = candles[-2]["close"]
                if _pc > 0:
                    _raw_gap = closed["open"] - _pc
                    _gp = _raw_gap / _pc
                    if abs(_gp) >= 0.0001:
                        _gap_pct = _gp
                        _gap_type = "SIM"
            micro_snap["gap_pct"] = _gap_pct
            micro_snap["gap_type"] = _gap_type
            micro_snap["key_levels"] = _key_levels(candles)
            _tl = list(ticks)
            if len(_tl) > 240:
                _st = len(_tl) / 240
                _tl = [_tl[int(i * _st)] for i in range(240)]
            micro_snap["ticks_json"] = _tick_json.dumps([round(x, 6) for x in _tl])
            _db.save(asset, period, closed, micro_snap)
        except Exception as _me:
            print(f"[db] micro save error: {_me}")

    def _running_confirmation(self, stream):
        if not stream.prediction or len(stream.ticks) < 5:
            return None
        pred = stream.prediction.get("signal")
        if pred == "NEUTRAL":
            return None
        ticks = list(stream.ticks)
        open_p = stream.candle_open_price
        net = ticks[-1] - open_p
        mid = len(ticks) // 2
        first_half = ticks[mid] - ticks[0]
        second_half = ticks[-1] - ticks[mid]
        if first_half > 0 and second_half > 0:
            running_dir = "UP"
        elif first_half < 0 and second_half < 0:
            running_dir = "DOWN"
        else:
            running_dir = "UP" if net >= 0 else "DOWN"
        if (pred == "CALL" and running_dir == "UP") or (pred == "PUT" and running_dir == "DOWN"):
            return "CONFIRMING"
        return "OPPOSING"

    def _apply_strength_gate(self, stream, prediction):
        if not prediction or prediction.get("signal") not in ("CALL", "PUT"):
            return prediction
        conf = self._running_confirmation(stream)
        if conf is None or len(stream.ticks) < 10:
            return prediction
        current = prediction.get("strength", "WEAK")
        new_strength = current
        if current == "WEAK" and conf == "CONFIRMING":
            new_strength = "MEDIUM"
        elif current == "MEDIUM" and conf == "OPPOSING":
            new_strength = "WEAK"
        elif current == "STRONG" and conf == "OPPOSING":
            new_strength = "MEDIUM"
        else:
            return prediction
        new_pred = dict(prediction)
        new_pred["strength"] = new_strength
        return new_pred

    def _running_candle(self, stream):
        """Build the current running candle OHLC from ticks."""
        op = stream.candle_open_price
        ticks = list(stream.ticks)
        if not ticks:
            return {"time": stream.candle_open_time, "open": op, "high": op, "low": op, "close": op}
        return {"time": stream.candle_open_time, "open": op,
                "high": max(ticks), "low": min(ticks), "close": ticks[-1]}

    async def _close_running_and_start_new(self, stream, new_open_time, first_tick, open_is_real=True):
        """Close the running candle (grade + log + save micro), then start a
        new candle at new_open_time with first_tick as its open price."""
        if new_open_time <= stream.candle_open_time:
            return None
        closed = self._running_candle(stream)
        if stream.candles and stream.candles[-1]["time"] == closed["time"]:
            stream.candles[-1] = closed
        elif not stream.candles or stream.candles[-1]["time"] < closed["time"]:
            stream.candles.append(closed)
        if len(stream.candles) > 500:
            stream.candles = stream.candles[-400:]
        _micro_snap = self._analyze_microstructure(stream.ticks, stream.candle_open_price) if len(stream.ticks) >= 10 else None
        # FIX (H2, 2026-07-19): wrap _grade_and_log + _save_micro in
        # asyncio.to_thread — they do synchronous sqlite3 I/O that would
        # otherwise block the shared event loop (parity with feed.py).
        accuracy = await asyncio.to_thread(
            self._grade_and_log, stream.asset, stream.period, closed,
            stream.prediction, _micro_snap, stream.candles)

        # BRAIN-LEARNED: loss cluster protection (sim_feed parity with feed.py)
        try:
            if accuracy == "wrong":
                stream._consecutive_losses = getattr(stream, '_consecutive_losses', 0) + 1
                if stream._consecutive_losses >= 5:
                    stream._loss_cooldown_until = time.time() + 1800  # 30 min
                    print(f"[sim] {stream.asset} hit {stream._consecutive_losses} consecutive losses — cooling down")
            elif accuracy == "correct":
                stream._consecutive_losses = 0
        except Exception:
            pass

        # BRAIN: record full prediction context for learning
        if accuracy in ("correct", "wrong"):
            try:
                from core.brain import record_prediction
                actual_dir = "UP" if closed["close"] > closed["open"] else (
                    "DRAW" if closed["close"] == closed["open"] else "DOWN")
                await asyncio.to_thread(
                    record_prediction,
                    stream.prediction or {}, stream.asset, stream.period,
                    closed["time"], actual_dir, accuracy, closed, _micro_snap)
            except Exception:
                pass

            # BRAIN: run analysis every 50 graded signals
            try:
                _brain_counter = getattr(self, '_brain_analyze_counter', 0) + 1
                self._brain_analyze_counter = _brain_counter
                if _brain_counter % 50 == 0:
                    from core.brain import analyze_and_learn
                    await asyncio.to_thread(analyze_and_learn)
            except Exception:
                pass

        # FIX (BUG-I, 2026-07-20): invalidate DB-adaptation cache after each
        # signal_log write so the next prediction reflects fresh accuracy data.
        if accuracy in ("correct", "wrong"):
            try:
                from engines.otc.config import weight_adapter as _otc_adapter
                from engines.real.config import weight_adapter as _real_adapter
                _otc_adapter.invalidate_cache(stream.asset, stream.period)
                _real_adapter.invalidate_cache(stream.asset, stream.period)
            except Exception:
                pass

        if accuracy in ("correct", "wrong"):
            _reg = (stream.prediction or {}).get("regime") or {}
            # FIX (BUG-2, 2026-07-18): use correct regime/zone keys.
            _regime = _reg.get("regime")
            if _reg.get("is_volatile"):
                _zone = "VOLATILE"
            elif _reg.get("is_trending"):
                _zone = "TREND"
            elif _reg.get("is_ranging"):
                _zone = "RANGE"
            else:
                _zone = "UNKNOWN"
            _key = (_regime, _zone)
            if _key == (stream.zone_streak["regime"], stream.zone_streak["zone"]):
                stream.zone_streak["losses"] = stream.zone_streak["losses"] + 1 if accuracy == "wrong" else 0
            else:
                stream.zone_streak = {"regime": _key[0], "zone": _key[1], "losses": 1 if accuracy == "wrong" else 0}
        stream.prediction = await self._run_eoc(stream, actual_open=first_tick)
        # Signal delay (2026-07-10): withhold prediction broadcast for
        # SIGNAL_DELAY_SEC seconds after the new candle opens.
        stream.signal_delay_until = time.time() + SIGNAL_DELAY_SEC
        if _micro_snap:
            await asyncio.to_thread(
                self._save_micro, stream.asset, stream.period, closed,
                _micro_snap, stream.candles, list(stream.ticks))
        stream.candle_open_time = new_open_time
        stream.candle_open_price = first_tick
        stream.candle_open_is_real = open_is_real
        stream.ticks.clear()
        stream.ticks.append(first_tick)
        return accuracy

    # ── Simulated stream lifecycle ─────────────────────────────────────────

    def _floor_to_period(self, ts, period):
        return (int(ts) // period) * period

    async def _run_stream(self, stream):
        """Simulated stream main loop: generate history, then tick ~10x/sec,
        closing+opening candles at period boundaries, running LIVE re-eval
        re-eval + strength gates, and broadcasting snapshot/tick/eoc msgs."""
        key = (stream.asset, stream.period)
        try:
            # Generate history (pass stream.period — see _gen_history fix)
            history = self._gen_history(stream.asset, stream.period, 120)
            stream.candles = history
            last = history[-1]
            stream._sim_price = last["close"]
            stream._sim_momentum = 0.0
            pip = _PIP.get(stream.asset, 0.0001)
            stream._sim_volatility = pip * 1.0
            # FIX (2026-07-13): floor candle_open_time to the period boundary.
            # `last["time"] + stream.period` was not period-aligned (last["time"]
            # was already floored, so this is OK — but be explicit and robust).
            stream.candle_open_time = self._floor_to_period(last["time"] + stream.period, stream.period)
            stream.candle_open_price = last["close"]
            stream.ticks.clear()
            stream.ticks.append(last["close"])
            stream.candle_open_is_real = False
            pair = next((p for p in self._pairs_list if p["asset"] == stream.asset), None)
            stream.payout = pair["payout"] if pair else None
            stream.prediction = await self._run_eoc(stream, actual_open=last["close"])
            # Initial subscription joins mid-candle — no signal delay.
            stream.signal_delay_until = 0.0
            await self._broadcast({
                "type": "snapshot", "asset": stream.asset, "period": stream.period,
                # FIX (2026-07-13): cap to last 300 candles for consistency
                # with feed.py (history gen is 120, but cap guards future growth).
                "candles": history[-300:] if len(history) > 300 else history,
                "prediction": stream.prediction,
            })
            print(f"[sim] started {stream.asset} with {len(history)} candles, price={stream._sim_price}")

            # Stream loop
            while True:
                now = time.time()
                current_period = self._floor_to_period(now, stream.period)

                # Check candle boundary
                if stream.candle_open_time > 0 and current_period != stream.candle_open_time:
                    last_px = list(stream.ticks)[-1] if stream.ticks else stream._sim_price
                    accuracy = await self._close_running_and_start_new(stream, current_period, last_px)
                    running = self._running_candle(stream)
                    all_c = stream.candles + [running]
                    # Signal delay (2026-07-10): withhold prediction at EOC;
                    # it will be delivered via the tick loop once the delay
                    # gate passes. Candle data + accuracy still flow.
                    await self._broadcast({
                        "type": "eoc", "asset": stream.asset, "period": stream.period,
                        "candles": all_c[-300:], "prediction": None,
                        "accuracy": accuracy,
                    })

                # Generate tick
                tick_price = self._gen_tick(stream)
                stream.ticks.append(tick_price)
                stream.last_real_tick_wall = time.time()

                # Build running candle + micro
                running = self._running_candle(stream)
                if not stream.candles or stream.candles[-1]["time"] < running["time"]:
                    stream.candles.append(running)

                # Micro analysis
                micro = self._analyze_microstructure(stream.ticks, stream.candle_open_price)

                # LIVE re-eval
                # Priority 1 (2026-07-10): ADAPTIVE refresh rate.
                #   - Last 5s: every 2 ticks (Priority 3 critical zone)
                #   - Last 10s: every 3 ticks
                #   - Last 30s: every 10 ticks
                #   - Earlier: every 30 ticks (cheap baseline)
                # Priority 3 (2026-07-10): volatility speedup — if last 3
                # ticks moved >0.5 ATR, cut interval in half.
                pred_changed = False
                if ENABLE_LIVE_THEORY and stream.base_candles and len(stream.ticks) >= 15:
                    if stream.candle_open_time > 0:
                        time_to_close = (stream.candle_open_time + stream.period) - time.time()
                        if time_to_close < 5:
                            reeval_interval = 2   # Priority 3: critical zone
                        elif time_to_close < 10:
                            reeval_interval = 3
                        elif time_to_close < 30:
                            reeval_interval = 10
                        else:
                            reeval_interval = 30
                    else:
                        reeval_interval = 30

                    # Priority 3: volatility speedup
                    if len(stream.ticks) >= 4 and reeval_interval > 2:
                        try:
                            recent = list(stream.ticks)[-4:]
                            recent_range = max(recent) - min(recent)
                            _atr_val = (_atr(stream.candles[-20:])
                                        if len(stream.candles) >= 20
                                        else 0.0001)
                            if _atr_val > 0 and recent_range > _atr_val * 0.5:
                                reeval_interval = max(2, reeval_interval // 2)
                        except Exception:
                            pass

                    if len(stream.ticks) - stream._live_reeval_ticks >= reeval_interval:
                        try:
                            fresh, _ = await self._analyze_core(stream.asset, stream.period,
                                                           stream.base_candles, stream.base_ticks,
                                                           running_ticks=list(stream.ticks),
                                                           stream=stream)
                            stream._live_reeval_ticks = len(stream.ticks)
                            if fresh and fresh.get("signal") in ("CALL", "PUT"):
                                # Flip suppression (2026-07-10) — track signal
                                # history for chop detection. Used to demote
                                # strength (NOT change direction — direction is
                                # locked below per ONE SIGNAL PER CANDLE rule).
                                sig = fresh["signal"]
                                stream.live_signal_history.append(
                                    (len(stream.ticks), sig, time.time()))
                                cutoff = time.time() - 10
                                stream.live_signal_history = [
                                    h for h in stream.live_signal_history
                                    if h[2] >= cutoff]
                                dirs = [h[1] for h in stream.live_signal_history]
                                flips = 0
                                for i in range(1, len(dirs)):
                                    if dirs[i] != dirs[i-1]:
                                        flips += 1
                                # Compute strength demotion flag (do NOT
                                # change CALL/PUT direction — see lock below).
                                demote_to_weak = (
                                    (flips >= 3 and len(dirs) >= 4)
                                    or (flips >= 2 and len(dirs) >= 3
                                        and fresh.get("strength") != "WEAK"))
                                flip_neutralize = (flips >= 3 and len(dirs) >= 4)

                                # ── ONE SIGNAL PER CANDLE (2026-07-19 fix H1) ──
                                # Ported from feed.py: the signal direction
                                # is LOCKED at EOC. LIVE re-eval can only
                                # update score/confidence/strength — NEVER
                                # CALL↔PUT. This brings sim_feed in parity
                                # with feed.py and prevents the "signal
                                # flipping on same candle" issue.
                                locked_dir = (stream.prediction or {}).get("signal")
                                fresh_dir = fresh.get("signal")

                                if locked_dir in ("CALL", "PUT"):
                                    if fresh_dir == locked_dir:
                                        # Same direction — update score/
                                        # confidence/strength in place.
                                        merged = {
                                            **stream.prediction,
                                            "score": fresh.get("score",
                                                stream.prediction.get("score")),
                                            "confidence": fresh.get("confidence",
                                                stream.prediction.get("confidence")),
                                            "agree": fresh.get("agree",
                                                stream.prediction.get("agree")),
                                            "total": fresh.get("total",
                                                stream.prediction.get("total")),
                                        }
                                        # Flip-suppression: demote strength
                                        # without changing direction.
                                        if demote_to_weak:
                                            # FIX (BACKTEST-2026-07-21): convert to
                                            # NEUTRAL instead of WEAK.
                                            merged["signal"] = "NEUTRAL"
                                            merged["strength"] = "NEUTRAL"
                                            merged["confidence"] = 0
                                            merged.setdefault("reasons", []).append(
                                                f"FLIP_DEMOTE (BACKTEST-FIX): {flips} flips in last 10s → NEUTRAL (skip). Backtest: WEAK won 4.2%.")
                                        # Reason refresh — keep latest module
                                        # breakdown for transparency.
                                        merged["modules"] = fresh.get("modules",
                                            stream.prediction.get("modules"))
                                        merged["reasons"] = fresh.get("reasons",
                                            stream.prediction.get("reasons"))
                                        if demote_to_weak and "FLIP_DEMOTE" not in " ".join(merged.get("reasons", [])):
                                            merged.setdefault("reasons", []).append(
                                                f"FLIP_DEMOTE: {flips} flips in last 10s → WEAK")
                                        stream.prediction = merged
                                        pred_changed = True
                                    # else: different direction → IGNORE.
                                    # The original EOC signal stays locked.
                                elif locked_dir == "NEUTRAL" and fresh_dir in ("CALL", "PUT"):
                                    # Original was NEUTRAL but live data
                                    # now shows a clear direction → allow
                                    # one-time upgrade to CALL/PUT.
                                    # FIX H1/M5: preserve candle/payout keys
                                    # that _run_eoc added to stream.prediction.
                                    new_pred = {**(stream.prediction or {}), **fresh}
                                    if flip_neutralize:
                                        # Even on initial upgrade, if chop is
                                        # severe, stay NEUTRAL (skip upgrade).
                                        pass
                                    else:
                                        if demote_to_weak:
                                            # FIX (BACKTEST-2026-07-21): convert to
                                            # NEUTRAL instead of WEAK. Backtest showed
                                            # WEAK signals win 4.2% — skip is +EV.
                                            new_pred["signal"] = "NEUTRAL"
                                            new_pred["strength"] = "NEUTRAL"
                                            new_pred["confidence"] = 0
                                            new_pred.setdefault("reasons", []).append(
                                                f"FLIP_DEMOTE (BACKTEST-FIX): {flips} flips in last 10s → NEUTRAL (skip). Backtest: WEAK won 4.2%.")
                                        stream.prediction = new_pred
                                        pred_changed = True
                                elif locked_dir is None:
                                    # No prior prediction — first LIVE re-eval.
                                    # FIX M5: merge with stream.prediction
                                    # (which may carry candle/payout from
                                    # _run_eoc) instead of overwriting.
                                    new_pred = {**(stream.prediction or {}), **fresh}
                                    if demote_to_weak:
                                        # FIX (BACKTEST-2026-07-21): convert to
                                        # NEUTRAL instead of WEAK.
                                        new_pred["signal"] = "NEUTRAL"
                                        new_pred["strength"] = "NEUTRAL"
                                        new_pred["confidence"] = 0
                                        new_pred.setdefault("reasons", []).append(
                                            f"FLIP_DEMOTE (BACKTEST-FIX): {flips} flips in last 10s → NEUTRAL (skip). Backtest: WEAK won 4.2%.")
                                    stream.prediction = new_pred
                                    pred_changed = True
                                # If flip_neutralize fires on a LOCKED CALL/PUT,
                                # we used to force NEUTRAL — but per the lock
                                # rule, we CANNOT change direction. Instead we
                                # demote to WEAK (handled above). The original
                                # NEUTRALize-on-flip behavior was a violation
                                # of ONE SIGNAL PER CANDLE and is removed.
                        except Exception as exc:
                            import traceback as tb
                            print(f"[sim] LIVE re-eval error: {exc}")
                            tb.print_exc()

                # Strength gate
                if ENABLE_STRENGTH_GATE and stream.prediction and stream.prediction.get("signal") != "NEUTRAL":
                    gated = self._apply_strength_gate(stream, stream.prediction)
                    if gated is not stream.prediction:
                        stream.prediction = gated
                        pred_changed = True

                # Broadcast tick — ALWAYS send prediction if gate has opened
                msg = {
                    "type": "tick", "asset": stream.asset, "period": stream.period,
                    "candle": running, "running_conf": self._running_confirmation(stream),
                    "micro": micro,
                }
                # Signal delay gate: withhold prediction until gate opens.
                # Once open, send prediction on EVERY tick so frontend stays updated.
                now_ts = time.time()
                if stream.signal_delay_until > 0 and now_ts < stream.signal_delay_until:
                    pass   # still in delay — no prediction
                else:
                    if stream.signal_delay_until > 0:
                        stream.signal_delay_until = 0.0
                    # Always include prediction (not just on pred_changed)
                    if stream.prediction:
                        msg["prediction"] = stream.prediction
                await self._broadcast(msg)

                # Tick interval: ~100ms (simulating real feed speed)
                await asyncio.sleep(random.uniform(0.08, 0.15))

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            import traceback as tb
            print(f"[sim] stream {key} error: {exc}")
            tb.print_exc()
        finally:
            # DON'T pop the stream immediately — let the watchdog restart it.
            # If we pop here, there's a gap (up to 30s) where the stream
            # doesn't exist and no ticks flow. Instead, mark it as needing
            # restart and let the watchdog handle it on next cycle (within 5s).
            # BUT: if the stream was idle-evicted (not always_on), do pop it.
            if key in self._streams:
                s = self._streams[key]
                if not s.always_on:
                    self._streams.pop(key, None)
                    print(f"[sim] stream {key} stopped (idle eviction)")
                else:
                    # always_on stream died — restart IMMEDIATELY (don't wait for watchdog)
                    print(f"[sim] stream {key} stopped — restarting immediately (always_on)")
                    try:
                        new_stream = _AssetStream(
                            asset=stream.asset, period=60, always_on=True,
                            candles=stream.candles)
                        new_stream.ticks.extend(list(stream.ticks))
                        new_stream._sim_price = getattr(stream, '_sim_price', 0)
                        new_stream._sim_momentum = getattr(stream, '_sim_momentum', 0)
                        new_stream._sim_volatility = getattr(stream, '_sim_volatility', 0)
                        new_stream.candle_open_time = stream.candle_open_time
                        new_stream.candle_open_price = stream.candle_open_price
                        self._streams[key] = new_stream
                        new_stream.task = asyncio.create_task(self._run_stream(new_stream))
                    except Exception as restart_exc:
                        print(f"[sim] FAILED to restart {key}: {restart_exc}")
                        self._streams.pop(key, None)

    # ── Manager loop ───────────────────────────────────────────────────────

    async def run(self, broadcast):
        self._broadcast = broadcast
        _db.init()
        _db.cleanup()
        self._connected = True
        print("[sim] connected (simulation mode)")
        # FIX (2026-07-17): broadcast structured payload (real_pairs + otc_pairs
        # + payout_floor_real + payout_floor_otc) so the frontend 3-dot menu
        # can populate both category counts. Backward-compat `pairs` and
        # `payout_floor` keys are kept for any older client.
        await self._broadcast({
            "type": "pairs",
            "pairs":  self._pairs_list,                  # backward compat
            "real_pairs": self._real_pairs_list,
            "otc_pairs":  self._otc_pairs_list,
            "payout_floor_real": PAYOUT_FLOOR_REAL,
            "payout_floor_otc":  PAYOUT_FLOOR_OTC,
            "payout_floor": PAYOUT_FLOOR_OTC,            # backward compat
        })

        # FIX (2026-07-17): pre-warm all eligible pairs as always_on streams
        # so the chart shows ticks immediately when a user opens the app —
        # matches feed.py's _reconcile_always_on behavior. Without this,
        # sim mode only ticks when a viewer is subscribed.
        for p in self._pairs_list:
            if p["status"] in ("live", "otc") and not p.get("locked"):
                key = (p["asset"], 60)
                if key not in self._streams:
                    s = _AssetStream(asset=p["asset"], period=60, always_on=True)
                    self._streams[key] = s
                    s.task = asyncio.create_task(self._run_stream(s))

        _last_watchdog_run = 0.0
        WATCHDOG_INTERVAL = 30.0

        while True:
            try:
                # NOTE (refactor 2026-07-14): mute refresh removed.
                # Sweep idle
                now = time.time()
                for key, s in list(self._streams.items()):
                    if s.always_on:
                        continue
                    if s.interested_cids:
                        s.idle_since = None
                        continue
                    if s.idle_since is None:
                        s.idle_since = now
                    elif now - s.idle_since > 300:
                        if s.task:
                            s.task.cancel()
                        self._streams.pop(key, None)

                # ── Always-on watchdog (every 30s) ──────────────────────────
                # Restart dead always_on streams so the sim stays ticking
                # even with no viewers — mirrors feed.py's _watchdog_always_on.
                if now - _last_watchdog_run > WATCHDOG_INTERVAL:
                    _last_watchdog_run = now
                    eligible_assets = {
                        p["asset"] for p in self._pairs_list
                        if p["status"] in ("live", "otc") and not p.get("locked")
                    }
                    for asset in eligible_assets:
                        key = (asset, 60)
                        stream = self._streams.get(key)
                        if stream is None:
                            s = _AssetStream(asset=asset, period=60, always_on=True)
                            self._streams[key] = s
                            s.task = asyncio.create_task(self._run_stream(s))
                            print(f"[sim] watchdog: created always_on stream for {asset}")
                            continue
                        task = stream.task
                        if task is None or task.done():
                            print(f"[sim] watchdog: restarting dead stream {asset}")
                            new_stream = _AssetStream(
                                asset=asset, period=60, always_on=True,
                                candles=stream.candles)
                            new_stream.ticks.extend(list(stream.ticks))
                            new_stream.prediction = stream.prediction
                            new_stream.candle_open_time = stream.candle_open_time
                            new_stream.candle_open_price = stream.candle_open_price
                            # FIX (BUG-4, 2026-07-18): removed dead
                            # `stream._evicting = True` assignment — the
                            # old stream is already being replaced and
                            # its task is done; setting a flag on it has
                            # no effect (the field was never declared on
                            # the dataclass either).
                            self._streams[key] = new_stream
                            new_stream.task = asyncio.create_task(self._run_stream(new_stream))
            except Exception as exc:
                print(f"[sim] manager error: {exc}")
            await asyncio.sleep(5)