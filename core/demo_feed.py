"""
core/demo_feed.py — DEMO FEED for preview deployments (2026-09-29).

WHY: the app is live-data-only by contract ("sim mode disabled"), so a
preview/sandbox deployment shows an empty UI. QX_DEMO_FEED=1 swaps the
Quotex client for this LOCAL tick generator — the ENTIRE production
pipeline (candle building → EOC module engine → ML engine → live-candle
prediction → grading → history) runs unchanged on demo ticks.

HONESTY CONTRACT (repo lesson: fabricated data must be unmistakable):
  * Every demo asset carries the suffix "_otc" and the server adds a
    DEMO banner — the UI can never be mistaken for a live market.
  * The generator is a regime-switching random walk (trend/chop/lean
    + AR(1) tick momentum + spike prints + stop-hunt wicks) — the SAME
    properties the backtest generator uses. It is NOT a prediction of
    any real market.
  * Signals graded in demo mode land in the same DB the deploy owns —
    a preview deploy should use a THROWAWAY DB (the sandbox does).

Interface parity with quotex_ws.QuotexWSClient (everything feed.py
touches): set_session / connect / close / get_instruments /
get_payout_by_asset / register_tick_callback / unregister_tick_callback /
start_candles_stream / stop_candles_stream / get_historical_candles /
get_candles / get_realtime_price / _server_time_offset.
"""
from __future__ import annotations

import asyncio
import random
import time

# Demo assets: 6 OTC-style pairs with realistic base prices.
DEMO_ASSETS = {
    "EURUSD_otc": 1.08650,
    "GBPUSD_otc": 1.26720,
    "USDJPY_otc": 149.820,
    "NZDCAD_otc": 0.89340,
    "EURGBP_otc": 0.85710,
    "USDBDT_otc": 109.8500,
}

_TICKS_PER_SEC = 2.4          # OTC-like tick density
_SIGMA = 0.00007              # per-tick vol (forex-ish)


def _is_jpy_like(price: float) -> bool:
    return price > 20.0


class _DemoAssetState:
    """Per-asset price state — OTC-like composite dynamics.

    Regimes (documented design, mirrors the backtest families in
    scripts/backtest_next_candle.py — NOT a prediction of any market):
      * MR    — mean-reversion pull toward a slowly wandering anchor
                (OU-like; the property broker OTC feeds are known for)
      * TREND — multi-candle directional drift (2–8 minutes)
      * CHOP  — balanced noise
    plus AR(1) tick momentum, occasional spike prints and stop-hunt wicks.
    The same _step() drives BOTH live ticks and synthetic history, so the
    NEXT-CANDLE engine's replay-graded accuracy reflects the real dynamics.
    """

    def __init__(self, asset: str, price: float):
        self.asset = asset
        self.price = price
        self.scale = 100.0 if _is_jpy_like(price) else 1.0   # JPY: 2-decimal
        self.sigma = _SIGMA * self.scale
        self.rng = random.Random(hash(asset) & 0xFFFFFFFF)
        self.innov = 0.0
        self.drift = 0.0
        self.drift_left = 0
        self.mode = "CHOP"
        self.anchor = price
        self.anchor_left = 3600

    def _regime(self):
        if self.drift_left <= 0:
            r = self.rng.random()
            if r < 0.45:      # mean-reversion regime (~45% of the time)
                self.mode = "MR"
                self.drift = 0.0
                self.drift_left = self.rng.randint(240, 960)    # ~2–7 min
            elif r < 0.75:    # multi-candle trend regime (~30%)
                self.mode = "TREND"
                self.drift = self.rng.choice((-1, 1)) * self.rng.uniform(0.25, 0.6)
                self.drift_left = self.rng.randint(300, 1200)  # ~2–8 min
            else:             # chop (~25%)
                self.mode = "CHOP"
                self.drift = 0.0
                self.drift_left = self.rng.randint(60, 240)
        self.drift_left -= 1
        # slow anchor wander (keeps MR non-stationary)
        self.anchor_left -= 1
        if self.anchor_left <= 0:
            self.anchor_left = self.rng.randint(1800, 5400)
            self.anchor += self.rng.gauss(0.0, 1.0) * self.sigma * 30.0

    def _step(self) -> float:
        """One tick of the composite dynamics (returns the new price)."""
        self._regime()
        # AR(1) tick momentum
        self.innov = 0.30 * self.innov + self.rng.gauss(0.0, 1.0) * self.sigma
        step = self.drift * self.sigma + self.innov
        # mean-reversion pull toward the anchor (MR regime only)
        if self.mode == "MR":
            step += 0.0012 * (self.anchor - self.price)
        # occasional spike print
        if self.rng.random() < 0.002:
            step += self.rng.choice((-1, 1)) * self.sigma * self.rng.uniform(5, 10)
        self.price += step
        # stop-hunt wick: sharp pull-back after an excursion
        if self.rng.random() < 0.0015:
            self.price -= step * self.rng.uniform(3.0, 6.0)
        return self.price

    def next_price(self) -> float:
        return self._step()

    def simulate_history(self, n_candles: int, period_s: int = 60):
        """Generate n_candles of 1m OHLC with the SAME dynamics (used by
        get_historical_candles so history and live ticks match)."""
        ticks_per_candle = max(1, int(period_s * _TICKS_PER_SEC))
        candles = []
        now = int(time.time())
        now -= now % period_s
        for i in range(n_candles, 0, -1):
            t = now - i * period_s
            o = self.price
            hi = lo = c = o
            for _ in range(ticks_per_candle):
                c = self._step()
                hi = max(hi, c)
                lo = min(lo, c)
            candles.append({"time": t, "open": o, "high": hi,
                            "low": lo, "close": c})
        return candles


class DemoClient:
    """Drop-in local client for QX_DEMO_FEED=1 preview deployments."""

    def __init__(self):
        self._states = {a: _DemoAssetState(a, p) for a, p in DEMO_ASSETS.items()}
        self._callbacks: dict[str, list] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._hist_cache: dict[tuple, list] = {}   # master series per (asset, period)
        self._running = False
        self._loop = None
        # feed._srv_now reads this — demo clock is the local clock.
        self._server_time_offset = 0

    # ── lifecycle (quotex_ws parity) ─────────────────────────────────────
    def set_session(self, **kwargs):
        return True

    async def connect(self):
        self._running = True
        self._loop = asyncio.get_running_loop()
        print("[demo-feed] connected (QX_DEMO_FEED=1 — local synthetic ticks, "
              "NOT a live market)")
        return True, "demo-mode"

    async def close(self):
        self._running = False
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()
        return True

    # ── instruments / payout ─────────────────────────────────────────────
    async def get_instruments(self, refresh=False):
        out = []
        for asset in DEMO_ASSETS:
            # Tuple parity with pyquotex: i[1]=name, i[2]=display,
            # i[14]=open, i[-9]=payout (needs >= 24 slots for -9 to clear 14)
            row = [None] * 24
            row[1] = asset
            row[2] = asset.replace("_otc", " (OTC ডেমো)")
            row[14] = True
            row[-9] = 85
            out.append(tuple(row))
        return out

    def get_payout_by_asset(self, asset):
        return 85

    # ── tick callbacks (the feed's event-driven path) ────────────────────
    def register_tick_callback(self, asset, callback):
        cbs = self._callbacks.setdefault(asset, [])
        if callback not in cbs:
            cbs.append(callback)
        self._ensure_generator(asset)

    def unregister_tick_callback(self, asset, callback):
        cbs = self._callbacks.get(asset, [])
        if callback in cbs:
            cbs.remove(callback)

    # ── stream control ───────────────────────────────────────────────────
    async def start_candles_stream(self, asset, period):
        self._ensure_generator(asset)
        return True

    async def stop_candles_stream(self, asset, period):
        t = self._tasks.pop(asset, None)
        if t:
            t.cancel()
        return True

    async def get_realtime_price(self, asset):
        st = self._states.get(asset)
        if not st:
            return []
        return [{"time": time.time(), "price": st.price}]

    # ── history (synthetic — SAME dynamics as live, see _DemoAssetState) ──
    # One MASTER series per (asset, period), simulated on first request;
    # every history call returns its tail — so the base seed (200), the
    # engine's deep seed (3000) and the live ticks all describe ONE
    # continuous price path (no chart discontinuity, no overlapping
    # conflicting windows).
    async def get_historical_candles(self, asset, amount_of_seconds=7200,
                                     period=60, max_workers=1):
        st = self._states.get(asset)
        if st is None:
            return []
        key = (asset, period)
        cache = self._hist_cache.get(key)
        if cache is None:
            # deep master series: the NEXT-CANDLE engine (CSE) seeds its
            # online model from history — a deep window (QX_NC_SEED_CANDLES,
            # default 3000) means the local model is adapted from minute one
            cache = st.simulate_history(5000, period)
            self._hist_cache[key] = cache
        n = max(10, min(5000, int(amount_of_seconds // max(period, 1))))
        if n < len(cache):
            return list(cache[-n:])
        return list(cache)

    async def get_candles(self, asset, end_from_time=None, offset=7200,
                          period=60):
        return await self.get_historical_candles(
            asset, amount_of_seconds=offset, period=period)

    # ── generator core ───────────────────────────────────────────────────
    def _ensure_generator(self, asset: str):
        if asset not in self._states:
            self._states[asset] = _DemoAssetState(asset, 1.10)
        if self._running and asset not in self._tasks and self._loop:
            self._tasks[asset] = self._loop.create_task(
                self._generate(asset))

    async def _generate(self, asset: str):
        st = self._states[asset]
        rng = random.Random((hash(asset) ^ 0x9E37) & 0xFFFFFFFF)
        # JPY-like pairs quote 3 decimals, others 5 — match broker prints
        digits = 3 if _is_jpy_like(st.price) else 5
        while self._running:
            try:
                px = round(st.next_price(), digits)
                ts = time.time()
                tick = {"time": ts, "price": px}
                for cb in list(self._callbacks.get(asset, [])):
                    try:
                        res = cb(tick)
                        if asyncio.iscoroutine(res):
                            asyncio.create_task(res)
                    except Exception as exc:
                        print(f"[demo-feed] callback error {asset}: {exc}")
                await asyncio.sleep(rng.uniform(0.25, 0.65))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                print(f"[demo-feed] generator error {asset}: {exc}")
                await asyncio.sleep(1.0)
