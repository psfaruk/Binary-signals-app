"""Async utilities for improved performance with event-driven architecture."""
import asyncio
from typing import Any, Dict, Optional


class AsyncEvent:
    """Enhanced asyncio.Event with timeout support and automatic reset.

    Prevents race conditions by tracking event state separately from asyncio.Event.
    This ensures data isn't lost if set() is called before wait() starts.

    Note on auto_reset=True:
        When auto_reset=True, the event is reset after any wait() returns.
        With multiple concurrent waiters on the same event:
        - The first waiter gets the data and resets the event
        - Subsequent waiters may see None or race conditions
        Use auto_reset=False for shared/broadcast events.
    """

    def __init__(self, auto_reset: bool = False):
        self.event = asyncio.Event()
        self.auto_reset = auto_reset
        self.data: Optional[Any] = None
        self._has_fired = False  # Track if event has ever been set

    async def wait(self, timeout: Optional[float] = None):
        """Wait for event with optional timeout.

        If the event was already set before this wait started, returns data immediately.
        This prevents race conditions where set() fires before wait() starts listening.
        """
        # Check if event already fired (prevents race condition)
        if self._has_fired:
            data = self.data
            if self.auto_reset:
                self._has_fired = False
                self._reset_state()
            return data

        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Event wait timeout after {timeout}s")

        data = self.data
        if self.auto_reset:
            self._has_fired = False
            self._reset_state()
        return data

    def set(self, data: Optional[Any] = None):
        """Set event and store data."""
        self.data = data
        self._has_fired = True
        self.event.set()

    def _reset_state(self):
        """Reset internal state (called by wait when auto_reset=True)."""
        self.event.clear()
        self.data = None

    def reset(self):
        """Manually reset event and state."""
        self._has_fired = False
        self._reset_state()

    def is_set(self) -> bool:
        """Check if event is set."""
        return self.event.is_set() or self._has_fired


class EventRegistry:
    """Registry for managing multiple events by key."""

    def __init__(self):
        self._events: Dict[str, AsyncEvent] = {}
        self._lock = asyncio.Lock()

    async def get_event(self, key: str, auto_reset: bool = False) -> AsyncEvent:
        """Get or create an event by key.

        Events don't auto-reset by default to support multiple concurrent waiters.
        All pending waiters see the data when an event fires.
        """
        async with self._lock:
            if key not in self._events:
                self._events[key] = AsyncEvent(auto_reset=auto_reset)
            return self._events[key]

    async def set_event(self, key: str, data: Optional[Any] = None):
        """Set event data by key."""
        event = await self.get_event(key)
        event.set(data)

    async def wait_event(self, key: str, timeout: Optional[float] = None):
        """Wait for event by key."""
        event = await self.get_event(key)
        return await event.wait(timeout=timeout)

    async def clear_event(self, key: str):
        """Clear event by key."""
        async with self._lock:
            if key in self._events:
                self._events[key].reset()
