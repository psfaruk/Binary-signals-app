"""Real-time streaming and indicator methods extracted from Quotex.

This mixin is composed into Quotex via multiple inheritance. It uses
self.api, self.codes_asset, etc. — all set up in Quotex.__init__ inside
pyquotex/stable_api.py.

NOTE: Many realtime/indicator/sentiment/signal methods were removed
2026-07-13. feed.py (the only consumer of Quotex in this app) uses
only:
  - start_candles_stream(asset, period)
  - stop_candles_stream(asset)
  - get_realtime_price(asset)

Methods removed (with cross-check confirming zero external callers in
this codebase — stable_api.py's re_subscribe_stream is the only other
internal caller of realtime-mixin methods, and it calls
start_candles_one_stream / start_candles_all_size_stream /
start_mood_stream; see KEEP note below):
  - calculate_indicator(asset, indicator, params, history_size, timeframe)
  - subscribe_indicator(asset, indicator, params, callback, timeframe)
  - start_signals_data()
  - opening_closing_current_candle(asset, period)
        (called get_realtime_candles — also removed)
  - start_realtime_price(asset, period, timeout)
  - start_realtime_sentiment(asset, period, timeout)
  - start_realtime_candle(asset, period, timeout)
  - get_realtime_candles(asset)
  - get_realtime_sentiment(asset)
  - get_signal_data()

KEPT (because stable_api.Quotex.re_subscribe_stream still references
them — it is itself currently dead code but was kept conservative per
task instructions to avoid breaking inheritance):
  - start_candles_one_stream(asset, size)
  - start_candles_all_size_stream(asset)
  - start_mood_stream(asset, instrument)

KEPT (used by feed.py):
  - start_candles_stream(asset, period)
  - stop_candles_stream(asset)
  - get_realtime_price(asset)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pyquotex._api._constants import DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)


class RealtimeMixin:
    """Real-time streaming and indicator methods."""

    async def start_candles_stream(
            self, asset: str = "EURUSD", period: int = 0
    ) -> None:
        """Start streaming candle data for a specified asset."""
        if self.api:
            self.api.current_asset = asset
            await self.api.subscribe_realtime_candle(asset, period)
            await self.api.chart_notification(asset)
            await self.api.follow_candle(asset)
            self.api._track_subscription("candle", asset, period)

    async def stop_candles_stream(self, asset: str) -> None:
        """Stops streaming candle data for a specified asset."""
        if self.api:
            await self.api.unsubscribe_realtime_candle(asset)
            await self.api.unfollow_candle(asset)
            self.api._forget_subscription("candle", asset)

    async def get_realtime_price(self, asset: str) -> list[dict[str, Any]]:
        """Retrieves current real-time price history for an asset from
        shared state."""
        if self.api:
            # Convert deque to list for compatibility with existing strategies
            return list(self.api.realtime_price.get(asset, []))
        return []

    # ------------------------------------------------------------------
    # Re-subscribe helpers — retained because stable_api.Quotex's
    # re_subscribe_stream() still references them. They are themselves
    # unused by feed.py today, but removing them would require editing
    # stable_api.py too, which is out of scope for this cleanup pass.
    # ------------------------------------------------------------------

    async def start_candles_one_stream(self, asset: str, size: int) -> bool:
        """Internal helper to start a single candle stream."""
        if self.api is None:
            return False

        if not (str(asset + "," + str(size)) in self.subscribe_candle):
            self.subscribe_candle.append((asset + "," + str(size)))
        start = time.time()
        # This part assumes api has these attributes, might need check
        if not hasattr(self.api, "candle_generated_check"):
            return False

        self.api.candle_generated_check[str(asset)][int(size)] = {}
        # Send the subscribe request exactly once before polling.
        # Calling follow_candle() inside the loop would spam the server
        # with up to 100 subscribe messages (20 s / 0.2 s) before data
        # arrives — a ban/rate-limit risk explicitly warned about in README.
        try:
            await self.api.follow_candle(self.codes_asset[asset])
        except Exception as e:
            logger.error('**error** start_candles_stream reconnect: %s', e)
            await self.connect()
        while True:
            if time.time() - start > 20:
                logger.error(
                    '**error** start_candles_one_stream late for 20 sec'
                )
                return False
            try:
                if self.api.candle_generated_check[str(asset)][int(size)]:
                    return True
            except (KeyError, TypeError):
                pass
            await asyncio.sleep(0.2)

    async def start_candles_all_size_stream(self, asset: str) -> bool:
        """Internal helper to subscribe to all candle sizes for an asset."""
        if self.api is None:
            return False

        if not hasattr(self.api, "candle_generated_all_size_check"):
            return False

        self.api.candle_generated_all_size_check[str(asset)] = {}
        if not (str(asset) in self.subscribe_candle_all_size):
            self.subscribe_candle_all_size.append(str(asset))
        self.api._track_subscription("candle_all_size", asset)
        start = time.time()
        while await self.check_connect():
            if self.api is None: break
            if time.time() - start > 20:
                logger.error(
                    f'**error** fail {asset} '
                    'start_candles_all_size_stream late for 10 sec'
                )
                return False
            try:
                if self.api.candle_generated_all_size_check[str(asset)]:
                    return True
            except (KeyError, TypeError):
                pass
            try:
                # Assuming api has subscribe_all_size
                if hasattr(self.api, "subscribe_all_size"):
                    self.api.subscribe_all_size(self.codes_asset[asset])
            except Exception as e:
                logger.error(
                    '**error** start_candles_all_size_stream reconnect: %s', e
                )
                await self.connect()
            await asyncio.sleep(0.2)
        return False

    async def start_mood_stream(
            self, asset: str, instrument: str = "turbo-option"
    ) -> None:
        """Internal helper to start the mood (sentiment) stream."""
        if self.api is None:
            return

        if asset not in self.subscribe_mood:
            self.subscribe_mood.append(asset)
        self.api._track_subscription("mood", asset, instrument=instrument)
        while True:
            if self.api is None: break
            if hasattr(self.api, "subscribe_Traders_mood"):
                self.api.subscribe_Traders_mood(asset, instrument)
            try:
                if hasattr(self.api, "traders_mood"):
                    asset_code = self.codes_asset[asset]
                    self.api.traders_mood[asset_code] = asset_code
                break
            finally:
                await asyncio.sleep(0.2)
