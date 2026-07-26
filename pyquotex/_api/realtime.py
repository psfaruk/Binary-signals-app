"""Real-time streaming and indicator methods extracted from Quotex.

This mixin is composed into Quotex via multiple inheritance. It uses
self.api (set up in Quotex.__init__ inside pyquotex/stable_api.py).

NOTE: Many realtime/indicator/sentiment/signal methods were removed
2026-07-13. feed.py (the only consumer of Quotex in this app) uses
only:
  - start_candles_stream(asset, period)
  - stop_candles_stream(asset)
  - get_realtime_price(asset)

FIX (DEEP-AUDIT-2026-07-26 / F-16-28): removed dead
`start_candles_one_stream`, `start_candles_all_size_stream`, and
`start_mood_stream` helpers — they were only called by the equally-dead
`stable_api.re_subscribe_stream` method (also removed). The
WebsocketClient's event-driven `_replay_subscriptions` is the canonical
re-subscribe path now.
"""
from __future__ import annotations

import logging
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
            # FIX (DEEP-AUDIT-2026-07-26 / F-16-29): `realtime_price` is
            # now a defaultdict(lambda: deque(maxlen=1000)) on QuotexAPI.
            # `list(...)` materialises the deque for backward-compat
            # with strategies expecting a list of {"time":..,"price":..}.
            return list(self.api.realtime_price.get(asset, []))
        return []
