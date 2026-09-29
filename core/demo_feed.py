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
    """Per-asset price state (regime drift + AR(1) momentum)."""

    def __init__(self, asset: str, price: float):
        self.asset = asset
        self.price = price
        self.scale = 100.0 if _is_jpy_like(price) else 1.0   # JPY: 2-decimal
        self.sigma = _SIGMA * self.scale
        self.rng = random.Random(hash(asset) & 0xFFFFFFFF)
        self.innov = 0.0
        self.drift = 0.0
        self.drift_left = 0

    def next_price(self) -> float:
        # regime switching
        if self.drift_left <= 0:
            r = self.rng.random()
            if r < 0.30:
                self.drift = self.rng.choice((-1, 1)) * self.rng.uniform(0.12, 0.4)
                self.drift_left = self.rng.randint(60, 220)
            elif r < 0.60:
                self.drift = self.rng.choice((-1, 1)) * self.rng.uniform(0.03, 0.12)
                self.drift_left = self.rng.randint(40, 120)
            else:
                self.drift = 0.0
                self.drift_left = self.rng.randint(30, 90)
        self.drift_left -= 1
        # AR(1) tick momentum
        self.innov = 0.30 * self.innov + self.rng.gauss(0.0, 1.0) * self.sigma
        step = self.drift * self.sigma + self.innov
        # occasional spike print
        if self.rng.random() < 0.002:
            step += self.rng.choice((-1, 1)) * self.sigma * self.rng.uniform(5, 10)
        self.price += step
        return self.price


class DemoClient:
    """Drop-in local client for QX_DEMO_FEED=1 preview deployments."""

    def __init__(self):
        self._states = {a: _DemoAssetState(a, p) for a, p in DEMO_ASSETS.items()}
        self._callbacks: dict[str, list] = {}
        self._tasks: dict[str, asyncio.Task] = {}
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

    # ── history (synthetic, generated once per call) ─────────────────────
    async def get_historical_candles(self, asset, amount_of_seconds=7200,
                                     period=60, max_workers=1):
        st = self._states.get(asset)
        if st is None:
            return []
        n = max(10, min(400, int(amount_of_seconds // max(period, 1))))
        now = int(time.time())
        now -= now % period
        candles = []
        price = st.price
        rng = random.Random((hash(asset) ^ now) & 0xFFFFFFFF)
        for i in range(n, 0, -1):
            t = now - i * period
            o = price
            hi = lo = c = o
            for _ in range(int(period * 1.5)):
                step = rng.gauss(0.0, 1.0) * st.sigma * 1.2
                c += step
                hi = max(hi, c)
                lo = min(lo, c)
            candles.append({"time": t, "open": o, "high": hi,
                            "low": lo, "close": c})
            price = c
        # move the live state to the generated close for continuity
        st.price = price
        return candles

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
