"""smart_streaming.py — Smart streaming system for unlimited smooth pairs."""
import os
import time
import asyncio
from collections import defaultdict
from typing import Optional

TIER1_MAX_STREAMS = int(os.environ.get("QX_TIER1_MAX", "15"))
TIER2_REFRESH_INTERVAL = int(os.environ.get("QX_TIER2_REFRESH", "300"))  # 5 min
TIER2_STREAM_DURATION = int(os.environ.get("QX_TIER2_DURATION", "60"))   # 60s
ROTATION_INTERVAL = int(os.environ.get("QX_ROTATION_INTERVAL", "300"))   # 5 min

ALWAYS_TIER1_PAIRS = set(os.environ.get("QX_ALWAYS_TIER1", "").split(","))

class SmartStreamingManager:
    """Manages 3-tier streaming for unlimited smooth pairs."""
    def __init__(self, feed):
        self.feed = feed
        self.tier1_pairs = set()       # actively streaming
        self.tier2_pairs = set()       # warm cache (periodic refresh)
        self.tier3_pairs = set()       # cold (not streaming)
        self.last_active = {}          # pair -> timestamp of last prediction/view
        self.viewing_pairs = set()     # pairs user is currently viewing
        self.rotation_task = None
        self.tier2_refresh_task = None
    async def start(self):
        """Start the rotation and refresh loops."""
        self.rotation_task = asyncio.create_task(self._rotation_loop())
        self.tier2_refresh_task = asyncio.create_task(self._tier2_refresh_loop())
        print(f"[smart-stream] started: tier1_max={TIER1_MAX_STREAMS}, "
              f"tier2_refresh={TIER2_REFRESH_INTERVAL}s")
    async def stop(self):
        """Stop all loops."""
        if self.rotation_task:
            self.rotation_task.cancel()
        if self.tier2_refresh_task:
            self.tier2_refresh_task.cancel()
    def mark_active(self, pair: str):
        """Mark a pair as active (recently predicted or viewed)."""
        self.last_active[pair] = time.time()
    def boost_to_tier1(self, pair: str):
        """Boost a pair to Tier 1 immediately (user opened it)."""
        self.viewing_pairs.add(pair)
        self.last_active[pair] = time.time()
        if pair not in self.tier1_pairs:
            self._promote_to_tier1(pair)
    def remove_view(self, pair: str):
        """User closed the pair view."""
        self.viewing_pairs.discard(pair)
    def _promote_to_tier1(self, pair: str):
        """Promote a pair to Tier 1, evicting if necessary."""
        if len(self.tier1_pairs) >= TIER1_MAX_STREAMS:
            evict = self._find_eviction_candidate()
            if evict and evict != pair:
                self.tier1_pairs.discard(evict)
                self.tier2_pairs.add(evict)
                print(f"[smart-stream] evicted {evict} from tier1 → tier2 "
                      f"(promoting {pair})")
                asyncio.create_task(self._unsubscribe_pair(evict))
        self.tier1_pairs.add(pair)
        self.tier2_pairs.discard(pair)
        self.tier3_pairs.discard(pair)
        asyncio.create_task(self._subscribe_pair(pair))
    def _find_eviction_candidate(self) -> Optional[str]:
        """Find the best pair to evict from Tier 1."""
        candidates = [
            p for p in self.tier1_pairs
            if p not in self.viewing_pairs
            and p not in ALWAYS_TIER1_PAIRS
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: self.last_active.get(p, 0))
        return candidates[0] if candidates else None
    async def _subscribe_pair(self, pair: str):
        """Subscribe to a pair's candle stream."""
        try:
            if self.feed._client and self.feed._connected:
                await self.feed._client.start_candles_stream(pair, 60)
                print(f"[smart-stream] subscribed {pair} (tier1)")
        except Exception as e:
            print(f"[smart-stream] subscribe error for {pair}: {e}")
    async def _unsubscribe_pair(self, pair: str):
        """Unsubscribe from a pair's candle stream."""
        try:
            if self.feed._client:
                await self.feed._client.stop_candles_stream(pair)
                print(f"[smart-stream] unsubscribed {pair} (tier1→tier2)")
        except Exception as e:
            print(f"[smart-stream] unsubscribe error for {pair}: {e}")
    async def _rotation_loop(self):
        """Every ROTATION_INTERVAL, rotate pairs between tiers."""
        try:
            while True:
                await asyncio.sleep(ROTATION_INTERVAL)
                await self._rotate_tiers()
        except asyncio.CancelledError:
            pass
    async def _rotate_tiers(self):
        """Rotate pairs: promote active tier2→tier1, demote inactive tier1→tier2."""
        now = time.time()
        active_threshold = now - 600  # 10 minutes
        to_demote = [
            p for p in self.tier1_pairs
            if self.last_active.get(p, 0) < active_threshold
            and p not in self.viewing_pairs
            and p not in ALWAYS_TIER1_PAIRS
        ]
        to_promote = []
        all_non_tier1 = self.tier2_pairs | self.tier3_pairs
        for pair in all_non_tier1:
            if self.last_active.get(pair, 0) > active_threshold:
                to_promote.append(pair)
        to_promote.sort(key=lambda p: self.last_active.get(p, 0), reverse=True)
        for i in range(min(len(to_demote), len(to_promote))):
            demote_pair = to_demote[i]
            promote_pair = to_promote[i]
            self.tier1_pairs.discard(demote_pair)
            self.tier2_pairs.add(demote_pair)
            await self._unsubscribe_pair(demote_pair)
            self.tier2_pairs.discard(promote_pair)
            self.tier3_pairs.discard(promote_pair)
            self.tier1_pairs.add(promote_pair)
            await self._subscribe_pair(promote_pair)
            print(f"[smart-stream] rotated: {demote_pair} →tier2, "
                  f"{promote_pair} →tier1")
    async def _tier2_refresh_loop(self):
        """Periodically refresh tier2 pairs for a brief period."""
        try:
            while True:
                await asyncio.sleep(TIER2_REFRESH_INTERVAL)
                await self._refresh_tier2()
        except asyncio.CancelledError:
            pass
    async def _refresh_tier2(self):
        """Subscribe to each tier2 pair for TIER2_STREAM_DURATION seconds,"""
        if not self.tier2_pairs:
            return
        for pair in list(self.tier2_pairs):
            try:
                total_streaming = len(self.tier1_pairs) + 1
                if total_streaming > TIER1_MAX_STREAMS + 2:
                    break  # too many, wait for next cycle
                await self._subscribe_pair(pair)
                print(f"[smart-stream] tier2 refresh: {pair} for {TIER2_STREAM_DURATION}s")
                await asyncio.sleep(TIER2_STREAM_DURATION)
                await self._unsubscribe_pair(pair)
            except Exception as e:
                print(f"[smart-stream] tier2 refresh error for {pair}: {e}")
    def get_status(self) -> dict:
        """Get current streaming status for debugging."""
        return {
            "tier1_count": len(self.tier1_pairs),
            "tier2_count": len(self.tier2_pairs),
            "tier3_count": len(self.tier3_pairs),
            "tier1_pairs": sorted(list(self.tier1_pairs)),
            "tier2_pairs": sorted(list(self.tier2_pairs)),
            "viewing_pairs": sorted(list(self.viewing_pairs)),
            "tier1_capacity": TIER1_MAX_STREAMS,
            "rotation_interval": ROTATION_INTERVAL,
        }
