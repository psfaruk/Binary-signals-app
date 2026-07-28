"""Async WebSocket client for the Quotex API.

Resilience layer
----------------
The client supports automatic reconnect with exponential backoff and a
stale-connection watchdog. Both are governed by
:class:`pyquotex.types.ReconnectPolicy`; pass ``ReconnectPolicy(enabled=False)``
to restore the original single-connection behavior.

Reconnect flow:

1. ``run_forever`` enters an outer loop that keeps trying to connect
   until :attr:`ReconnectPolicy.max_attempts` is reached (``0`` = infinite).
2. On every successful open, the :class:`QuotexAPI` ``_on_open`` hook
   runs as before AND a re-subscription pass replays any streams the
   user had opened (candle, all-size, mood, realtime price).
3. On unexpected close or watchdog timeout, the loop sleeps using
   :func:`pyquotex._api._waits.backoff_sleep` and reconnects.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from pyquotex._api._waits import backoff_sleep
from pyquotex.global_value import WebsocketStatus
from pyquotex.types import ReconnectPolicy

logger = logging.getLogger(__name__)


class WebsocketClient:
    """Pure-async WebSocket client with optional auto-reconnect."""

    def __init__(
        self,
        api: Any,
        reconnect_policy: ReconnectPolicy | None = None,
    ) -> None:
        """Initialize the WebSocket client.

        Args:
            api: The :class:`QuotexAPI` instance this client belongs to.
            reconnect_policy: Resilience configuration. Defaults to
                :class:`ReconnectPolicy` with auto-reconnect enabled.
        """
        self.api = api
        self.state = api.state
        self.policy = reconnect_policy or ReconnectPolicy()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._closing = False
        self._watchdog_task: asyncio.Task[None] | None = None
        # Counter of successful opens; the very first open does NOT
        # replay subscriptions (there are none yet).
        self._open_count = 0

    @property
    def wss(self) -> "WebsocketClient":
        """Returns the low-level WebSocket wrapper (self)."""
        return self

    async def send(self, data: str) -> None:
        """Send a frame; log instead of crashing if the socket is closed."""
        if self._ws and self._ws.state is State.OPEN:
            try:
                await self._ws.send(data)
                logger.debug("Sent: %s", data)
            except ConnectionClosed as e:
                logger.warning("Cannot send, connection closed: %s", e)
            except Exception as e:
                logger.error("Error sending WebSocket message: %s", e)

    async def run_forever(
            self,
            url: str,
            extra_headers: dict[str, str] | None = None,
            ssl: Any = None,
            **kwargs: Any,
    ) -> None:
        """Connect to the WebSocket and stay connected.

        With ``ReconnectPolicy.enabled=False`` this method connects once
        and returns when the connection ends. With auto-reconnect on, it
        keeps reconnecting until :meth:`close` is called or
        ``max_attempts`` is exceeded.
        """
        attempt = 0
        while True:
            try:
                await self._connect_once(url, extra_headers, ssl)
                if self._closing:
                    return
                attempt = 0  # successful run resets the backoff
            except ConnectionClosed as e:
                self._handle_close_exception(e)
            except Exception as e:
                logger.error("WebSocket error: %s", e)
                self.api._on_error(e)

            if not self.policy.enabled or self._closing:
                return
            if self.policy.max_attempts and attempt >= self.policy.max_attempts:
                logger.error(
                    "WebSocket auto-reconnect giving up after %d attempts",
                    attempt,
                )
                return

            logger.info("WebSocket reconnecting (attempt #%d)", attempt + 1)
            await backoff_sleep(
                attempt,
                base=self.policy.base_delay,
                cap=self.policy.max_delay,
                jitter=self.policy.jitter,
            )
            attempt += 1

    async def _connect_once(
            self,
            url: str,
            extra_headers: dict[str, str] | None,
            ssl: Any,
    ) -> None:
        """One ``connect()`` cycle. Returns when the connection ends."""
        headers = extra_headers or {}
        # FIX (CRASH-FIX-2026-07-26 / PQ-003): reset auth_status BEFORE the
        # fresh socket handshake. Previously, _on_open set state.status to
        # CONNECTED but left auth_status = AUTHENTICATED from the prior
        # session, so check_connect() / wait_until predicates returned True
        # immediately even though the new socket had not been authorized
        # by the server. After a reconnect, no SSID had been sent, so all
        # replayed subscriptions silently failed.
        # FIX (CRASH-FIX-2-2026-07-26 / LOG-FEEDBACK): the previous fix
        # imported from `pyquotex.types` but AuthStatus is actually
        # defined in `pyquotex.global_value`. The ImportError caused
        # every connect attempt to silently fall through the except,
        # so the auth reset NEVER happened — defeating the fix.
        # Now: import from the correct module.
        try:
            from pyquotex.global_value import AuthStatus
            self.api.state.auth_status = AuthStatus.NOT_AUTHENTICATED
        except Exception as _e:
            print(f"[silent-except] pyquotex/ws/client.py:142 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass
        async with websockets.connect(
            url,
            additional_headers=headers,
            ssl=ssl,
            ping_interval=24,
            ping_timeout=20,
            max_size=2 ** 23,
            compression=None,
        ) as ws:
            self._ws = ws
            self.api.last_message_at = time.monotonic()
            await self.api._on_open()
            self._open_count += 1
            if self._open_count > 1:
                asyncio.create_task(self._replay_subscriptions())

            self._start_watchdog()
            try:
                async for raw in ws:
                    await self.api._on_message(raw)
            finally:
                self._stop_watchdog()

    def _handle_close_exception(self, exc: ConnectionClosed) -> None:
        rcvd = getattr(exc, "rcvd", None)
        sent = getattr(exc, "sent", None)
        if rcvd:
            code, reason = rcvd.code, rcvd.reason
        elif sent:
            code, reason = sent.code, sent.reason
        else:
            code, reason = 1006, str(exc)
        logger.info("WebSocket closed: code=%s, reason=%s", code, reason)
        self.api._on_close(code, reason)

    # ------------------------------------------------------------------
    # Stale-connection watchdog
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        if self.policy.stale_timeout <= 0:
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        # FIX (2026-07-28 / TICK-KEEPALIVE): start a periodic keepalive
        # task that re-sends `chart_notification/get` for every active
        # subscription every 15s. Without this, Quotex's server stops
        # sending `quotes/stream` ticks after ~30s (server-side rate limit
        # / subscription expiry). The user reported "No ticks for 10s —
        # feed may be stale" after exactly this interval. The keepalive
        # refreshes the subscription so ticks continue flowing indefinitely.
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _stop_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None
        # FIX (2026-07-28 / TICK-KEEPALIVE): cancel the keepalive too
        _kt = getattr(self, "_keepalive_task", None)
        if _kt and not _kt.done():
            _kt.cancel()
        self._keepalive_task = None

    async def _watchdog_loop(self) -> None:
        timeout = self.policy.stale_timeout
        try:
            while self._ws is not None and self._ws.state is State.OPEN:
                await asyncio.sleep(min(timeout / 3.0, 10.0))
                silent_for = time.monotonic() - self.api.last_message_at
                if silent_for > timeout:
                    logger.warning(
                        "WebSocket idle for %.1fs (>%ds); recycling.",
                        silent_for, timeout,
                    )
                    try:
                        await self._ws.close(code=4000, reason="watchdog-stale")
                    except Exception as _e:
                        print(f"[silent-except] pyquotex/ws/client.py:205 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
                        pass
                    return
        except asyncio.CancelledError as _e:
            print(f"[silent-except] pyquotex/ws/client.py:208 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass

    # ------------------------------------------------------------------
    # FIX (2026-07-28 / TICK-KEEPALIVE): periodic subscription refresh
    # ------------------------------------------------------------------
    async def _keepalive_loop(self) -> None:
        """Send Engine.IO PING (`2`) every 20s and re-send chart_notification
        for active subscriptions every 15s.

        FIX (2026-07-28 / TICK-KEEPALIVE): Quotex uses Socket.IO v3 over
        Engine.IO. In v3, the CLIENT sends PING (`2`) and the server
        responds with PONG (`3`). The previous code only handled the
        reverse direction (server PING → client PONG), which never
        happened — so the server thought we were dead after 25s+5s
        (pingInterval+pingTimeout) and stopped sending ticks. This is
        the ROOT CAUSE of "ticks stop after 30s" — not a subscription
        expiry, but an Engine.IO keepalive failure.

        Now: send `2` every 20s (well under the 25s pingInterval) to
        keep the connection alive. Also re-send chart_notification/get
        for candle subscriptions every 15s as a belt-and-suspenders
        measure against subscription expiry.
        """
        try:
            while self._ws is not None and self._ws.state is State.OPEN:
                await asyncio.sleep(15)
                if not self.api.websocket:
                    continue
                # ── Engine.IO keepalive: send PING (`2`) ──────────────
                # This is the critical fix. Without it, Quotex's server
                # stops sending ticks after ~30s (pingInterval 25s +
                # pingTimeout 5s = 30s total).
                try:
                    await self.api.websocket.send("2")
                except Exception as _e:
                    logger.debug("PING send failed: %s", _e)
                # ── Subscription refresh: chart_notification/get ──────
                # Belt-and-suspenders. Re-send for every active candle
                # subscription in case the server expires them.
                subs = list(getattr(self.api, "_subscriptions", {}).values())
                for sub in subs:
                    if sub.kind != "candle":
                        continue
                    try:
                        asset = sub.asset
                        if asset and self.api.websocket:
                            payload = f'42["chart_notification/get", {{"asset":"{asset}","version":"1.0.0"}}]'
                            await self.api.websocket.send(payload)
                    except Exception as _e:
                        logger.debug("keepalive refresh failed for %s: %s",
                                     getattr(sub, "asset", "?"), _e)
        except asyncio.CancelledError:
            pass
        except Exception as _e:
            logger.warning("keepalive loop error: %s", _e)

    # ------------------------------------------------------------------
    # Subscription replay after reconnect
    # ------------------------------------------------------------------
    async def _replay_subscriptions(self) -> None:
        """Re-issue every tracked subscription after a successful reconnect."""
        try:
            for _ in range(40):  # ~2 s
                if self.state.status == WebsocketStatus.CONNECTED:
                    break
                await asyncio.sleep(0.05)
        except Exception as _e:  # pragma: no cover
            print(f"[silent-except] pyquotex/ws/client.py:221 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass

        # FIX (CRASH-FIX-2026-07-26 / PQ-002): the fresh socket is open but
        # unauthenticated. The server silently drops every replayed
        # `instruments/update`, `chart_notification/get`, and `depth/follow`
        # because no SSID has been sent on this new connection.
        # Send the SSID first; if the cached token is rejected, fall back
        # to a full HTTP `authenticate()` (which fetches a fresh SSID).
        try:
            if self.api.state.SSID:
                await self.api.send_ssid()
            else:
                # No cached token — try a full authenticate (this requires
                # email/password to be set; if not, the original failure
                # path will surface in connect()).
                ok, _ = await self.api.authenticate()
                if not ok:
                    logger.warning(
                        "_replay_subscriptions: authenticate() failed; "
                        "subscriptions will not be re-established."
                    )
                    return
        except Exception as e:
            logger.warning(
                "_replay_subscriptions: SSID send/auth failed (%s); "
                "subscriptions may be delayed.", e,
            )

        subs = list(getattr(self.api, "_subscriptions", {}).values())
        for sub in subs:
            try:
                await self._replay_one(sub)
            except Exception as e:
                logger.warning(
                    "Failed to replay subscription kind=%s asset=%s: %s",
                    sub.kind, sub.asset, e,
                )

    async def _replay_one(self, sub: Any) -> None:
        api = self.api
        if sub.kind == "candle":
            await api.subscribe_realtime_candle(sub.asset, sub.period or 0)
            await api.chart_notification(sub.asset)
            await api.follow_candle(sub.asset)
        elif sub.kind == "candle_all_size":
            await api.subscribe_all_size(sub.asset)
        elif sub.kind == "mood":
            instrument = sub.extra.get("instrument", "turbo-option")
            await api.subscribe_Traders_mood(sub.asset, instrument)
        elif sub.kind == "realtime_price":
            await api.subscribe_realtime_candle(sub.asset, sub.period or 0)

    async def close(self) -> None:
        """Close the websocket gracefully and stop auto-reconnect."""
        self._closing = True
        self.policy = ReconnectPolicy(enabled=False)
        self._stop_watchdog()
        if self._ws and self._ws.state is not State.CLOSED:
            await self._ws.close()

    def is_alive(self) -> bool:
        """Return True iff the underlying socket is currently OPEN."""
        return self._ws is not None and self._ws.state is State.OPEN
