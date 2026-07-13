"""Public, immutable dataclasses for the pyquotex API surface.

These types are returned by selected public methods (and accepted as
inputs where applicable). They exist so consumers get real IDE
completion and ``mypy`` coverage instead of opaque ``dict[str, Any]``.

Existing methods continue to return ``dict``/``list`` to preserve
backward compatibility; helper ``from_dict`` constructors are provided
so callers can opt into typed objects when they want them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# NOTE: The following public types were removed as dead code (no internal
# caller and no external user in this codebase):
#   - Candle, TradeResult, Balance, ProfileInfo, AssetInfo
#   - TradeStatus, TradeDirection (Literal type aliases)
# Only ReconnectPolicy and Subscription remain — both are actively used
# by feed.py, pyquotex/ws/client.py, and pyquotex/api.py.


@dataclass(slots=True, frozen=True)
class ReconnectPolicy:
    """Configures auto-reconnect behavior for :class:`WebsocketClient`.

    Parameters
    ----------
    enabled:
        Master toggle. When ``False``, the client behaves as it did before
        the resilience patch (one connection, no auto-reconnect).
    max_attempts:
        Stop after this many consecutive failed reconnects. ``0`` means
        infinite retries (recommended for long-lived bots).
    base_delay / max_delay / jitter:
        Exponential backoff parameters. Delay = ``base_delay * 2**attempt``,
        capped at ``max_delay`` seconds, with multiplicative ``jitter``.
    stale_timeout:
        Seconds without a single inbound frame before the connection is
        considered stale and forcibly recycled. ``0`` disables the
        watchdog (rely on websockets ping/pong only).
    """

    enabled: bool = True
    max_attempts: int = 0
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.1
    stale_timeout: float = 60.0


@dataclass(slots=True)
class Subscription:
    """Tracks an active stream so it can be resumed after reconnect."""

    kind: Literal["candle", "candle_all_size", "mood", "realtime_price"]
    asset: str
    period: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ReconnectPolicy",
    "Subscription",
]
