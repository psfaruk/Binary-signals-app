"""Module for Quotex websocket."""
import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

import httpx

from .global_value import AuthStatus, ConnectionState, WebsocketStatus
from .network.login import Login
from .network.navigator import Browser
from .network.settings import Settings
from .utils import json_utils as json
from .utils.account_type import AccountType
from .utils.async_utils import EventRegistry
from .ws.channels.candles import GetCandles
from .ws.channels.ssid import Ssid
from .ws.client import WebsocketClient
from .ws.objects.candles import Candles
from .ws.objects.listinfodata import ListInfoData
from .ws.objects.profile import Profile
from .ws.objects.timesync import TimeSync

# FIX (DEEP-AUDIT-2026-07-26 / F-16-03): removed imports of dead stub
# channels (Buy, SellOption) and dead network stubs (Logout, GetHistory)
# — the properties that returned them are deleted below.

logger = logging.getLogger(__name__)


class QuotexAPI:
    """Class for communication with Quotex API."""

    def __init__(
            self,
            host: str,
            username: str,
            password: str,
            lang: str,
            proxies: dict[str, str] | None = None,
            resource_path: str | None = None,
            user_data_dir: str = ".",
            on_otp_callback: Callable | None = None,
            reconnect_policy: Any = None,
            wss_url_override: str | None = None,
    ):
        """
        :param str host: The hostname or ip address of a Quotex server.
        :param str username: The username of a Quotex server.
        :param str password: The password of a Quotex server.
        :param str lang: The lang of a Quotex platform.
        :param proxies: The proxies of a Quotex server.
        :param user_data_dir: The path browser user data dir.
        :param on_otp_callback: Callback function for OTP (2FA) input.
        :param wss_url_override: Replace the computed ``wss://ws2.{host}``
            URL with this value. Primarily a test hook so the offline
            integration tests can point at a local replay server, but
            also useful for routing through a custom proxy.
        """
    # FIX (DEEP-AUDIT-2026-07-26 / F-16-04): removed ~15 dead-state
    # attributes that were only ever written by handlers of trading/
    # pending/sell-option events (all removed for the read-only signal
    # analysis app). Kept: state, on_otp_callback, reconnect_policy,
    # _ws_send_lock, current_asset, current_period, account_balance,
    # account_type, tournament_id, instruments, listinfodata, timesync,
    # candles, profile, host, https_url, wss_url, wss_message,
    # websocket_client, _websocket_task, is_logged, _temp_status,
    # username, password, resource_path, user_data_dir, proxies, lang,
    # settings_list, signal_data, get_candle_data, historical_candles,
    # candle_v2_data, realtime_price (deque now), realtime_price_data,
    # candle_generated_check, candle_generated_all_size_check,
    # session_data, browser, settings, event_registry, slots, profit_today,
    # heartbeat_task, last_message_at, _subscriptions, _control_handlers.

        self.state = ConnectionState()
        self.on_otp_callback = on_otp_callback
        self.reconnect_policy = reconnect_policy
        self._ws_send_lock = asyncio.Lock()

        self.current_asset: str | None = None
        self.current_period: int | None = None
        self.account_balance: dict[str, Any] | None = None
        self.account_type: int | None = AccountType.DEMO
        self.tournament_id: int = 0
        self.instruments: list[Any] = []
        self.listinfodata = ListInfoData()
        self.timesync = TimeSync()
        self.candles = Candles()
        self.profile = Profile()

        self.host = host
        self.https_url = f"https://{host}"
        self.wss_url = (
            wss_url_override
            if wss_url_override
            else f"wss://ws2.{host}/socket.io/?EIO=3&transport=websocket"
        )
        self.wss_message: str | None = None
        self.websocket_client: WebsocketClient | None = None
        self._websocket_task: asyncio.Task | None = None
        self.is_logged: bool = False
        self._temp_status: str = ""
        self.username = username
        self.password = password
        self.resource_path = resource_path
        self.user_data_dir = user_data_dir
        self.proxies = proxies
        self.lang = lang
        self.settings_list: dict[str, Any] = {}
        self.signal_data: dict[str, Any] = {}
        self.get_candle_data: dict[str, Any] = {}
        self.historical_candles: dict[str, Any] = {}
        self.candle_v2_data: dict[str, Any] = {}
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-05): use deque(maxlen=1000)
        # instead of defaultdict(list) — old code did
        # `if len(price_list) > 1000: price_list.pop(0)` which is O(N)
        # per tick (N=1000) and a real CPU bottleneck at 5-10 Hz tick
        # rate across N assets. deque(maxlen=1000) is O(1) and
        # auto-evicts oldest entries.
        self.realtime_price: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=1000)
        )
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-04): removed dead attributes
        # `realtime_price_data`, `realtime_candles`, `realtime_sentiment`,
        # `traders_mood`, `top_list_leader` — all set, never read.
        self.candle_generated_check = defaultdict(lambda: defaultdict(dict))
        self.candle_generated_all_size_check = defaultdict(dict)
        self.session_data: dict[str, Any] = {}
        self.browser = Browser()
        self.browser.set_headers()
        self.settings = Settings(self)
        self.event_registry = EventRegistry()
        from pyquotex._api._waits import SlotRegistry
        self.slots = SlotRegistry()
        self.profit_today: float | None = None
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-11): heartbeat_task removed.

        # Last time an inbound frame arrived; used by the stale watchdog
        # in :class:`pyquotex.ws.client.WebsocketClient` to decide when
        # to recycle a silent connection.
        self.last_message_at: float = time.monotonic()

        # Active stream subscriptions, replayed after auto-reconnect.
        from pyquotex.types import Subscription  # local import to avoid cycle
        self._subscriptions: dict[str, Subscription] = {}

        # Dispatch table for "control" events: maps Socket.IO event name
        # to an async handler taking the event payload. Refactor of the
        # previous if/elif chain in :meth:`_on_message`.
        self._control_handlers: dict[
            str, Callable[[Any], Awaitable[None]]
        ] = {
            "s_authorization": self._h_auth_ok,
            "instruments/list": self._h_instruments_list,
            "trader/history": self._h_trader_history,
            "balance": self._h_balance,
            "candle-generated": self._h_candle_generated,
            "sentiment": self._h_sentiment,
        }

    # ------------------------------------------------------------------
    # Subscription tracking (replayed by WebsocketClient after reconnect)
    # ------------------------------------------------------------------
    def _track_subscription(
        self,
        kind: str,
        asset: str,
        period: int | None = None,
        **extra: Any,
    ) -> None:
        """Record an active stream so it can be replayed after reconnect."""
        from pyquotex.types import Subscription
        key = f"{kind}:{asset}:{period or 0}"
        self._subscriptions[key] = Subscription(
            kind=kind,  # type: ignore[arg-type]
            asset=asset,
            period=period,
            extra=dict(extra),
        )

    def _forget_subscription(
        self, kind: str, asset: str, period: int | None = None
    ) -> None:
        key = f"{kind}:{asset}:{period or 0}"
        self._subscriptions.pop(key, None)

    # ------------------------------------------------------------------
    # Control-event handlers (dispatch table targets)
    # ------------------------------------------------------------------
    async def _h_auth_ok(self, data: Any) -> None:
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-06): also flip state.status to
        # CONNECTED (was previously done by the substring-matched branch).
        self.state.auth_status = AuthStatus.AUTHENTICATED
        self.state.status = WebsocketStatus.CONNECTED
        await self.event_registry.set_event(
            "auth_changed", self.state.auth_status
        )
        await self.event_registry.set_event(
            "status_changed", self.state.status
        )

    async def _h_instruments_list(self, data: Any) -> None:
        if isinstance(data, dict) and data.get("_placeholder"):
            self._temp_status = (
                '451-["instruments/list",'
                f'{json.dumps_str(data)}]'
            )
        else:
            self.instruments = data
            await self.event_registry.set_event("instruments_ready", data)

    async def _h_trader_history(self, data: Any) -> None:
        await self.event_registry.set_event("history_ready", data)

    async def _h_balance(self, data: Any) -> None:
        self.account_balance = data
        if data is not None:
            self.slots.balance.set(data)
        await self.event_registry.set_event("balance_ready", data)

    async def _h_candle_generated(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        asset = data.get("asset")
        period = data.get("period")
        if asset and period:
            self.candle_generated_check[str(asset)][int(period)] = data
            self.candle_generated_all_size_check[str(asset)] = data

    async def _h_sentiment(self, data: Any) -> None:
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-04): removed writes to dead
        # `traders_mood` and `realtime_sentiment` dicts (never read in
        # current app). Now only fires the event so consumers using
        # `wait_event('sentiment_ready')` (if any) still unblock.
        if not isinstance(data, dict):
            return
        await self.event_registry.set_event("sentiment_ready", data)

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------
    async def _on_open(self) -> None:
        """Called when WebSocket connection is established."""
        logger.info("Websocket client connected.")
        self.state.status = WebsocketStatus.CONNECTED
        await self.event_registry.set_event("status_changed", self.state.status)

        # FIX (DEEP-AUDIT-2026-07-26 / F-16-11): removed the legacy
        # `heartbeat()` task that sent `42["tick"]` every 5s — Quotex's
        # server ignores Socket.IO "tick" events, and the websockets
        # library already handles Engine.IO-level ping/pong via
        # `ping_interval` in ws/client.py. Also removed three unused
        # subscribe sends (`indicator/list`, `drawing/load`,
        # `pending/list`) whose responses have no handler and were
        # silently dropped. Fix also covers Problem 61: standardized on
        # `instruments/list` (the response event name) — old code sent
        # `instruments/get` which has no matching handler in the dispatch
        # table.
        await self.websocket.send('42["chart_notification/get"]')
        await self.websocket.send('42["instruments/list"]')

    async def _on_message(self, msg: bytes | str) -> None:
        """Called for every WebSocket message received."""
        # Stale-detection watchdog reads this timestamp.
        self.last_message_at = time.monotonic()
        try:
            message: Any = None
            msg_str = (
                msg.decode("utf-8", errors="ignore")
                if isinstance(msg, bytes)
                else str(msg)
            )

            # FIX (CRASH-FIX-2026-07-26 / PQ-006): Engine.IO keep-alive.
            # Quotex uses Socket.IO over Engine.IO. Engine.IO sends `2` as a
            # PING probe and expects `3` as the PONG reply. Without this
            # reply, the server closes the socket after a few minutes of
            # silence. The legacy heartbeat handler was removed in the
            # 2026-07-13 cleanup and never replaced — causing periodic
            # disconnects every ~3-5 minutes, which manifested as the
            # "auto-reconnect stopped working" complaint. Now we reply
            # immediately and return; no further processing needed.
            if msg_str == "2":
                if self.websocket:
                    try:
                        await self.websocket.send("3")
                    except Exception as _pong_err:
                        logger.debug("PONG send failed: %s", _pong_err)
                return
            # Engine.IO `0` = open handshake, `1` = close, `4` = message
            # (already prefixed to Socket.IO payloads like `42[...]`).
            # We don't need to handle `0`/`1` specially — the websockets
            # library handles the underlying socket lifecycle.

            # FIX (DEEP-AUDIT-2026-07-26 / F-16-07): removed DEBUG `print()`
            # of pre-auth WS messages that could leak session data to
            # stdout in production. Use logger.debug so it is filtered by
            # log level in production.
            if self.state.auth_status != AuthStatus.AUTHENTICATED:
                logger.debug("Pre-auth WS message: %s", msg_str[:200])

            # FIX (DEEP-AUDIT-2026-07-26 / F-16-06): removed substring
            # matching on raw WS messages (`if "authorization/reject" in
            # msg_str` / `elif "s_authorization" in msg_str`) which could
            # false-positive on tick asset names containing the substring.
            # Now we parse the Socket.IO frame and dispatch via the event
            # name only. The control-handler dispatch table below already
            # handles `s_authorization` via `_h_auth_ok` — we keep an
            # explicit check here for the reject path (Quotex sends
            # `authorization/reject` as the event name).
            try:
                _evt_name = None
                if msg_str and msg_str[0].isdigit():
                    # Find the JSON payload start (see F-16-08 fix below)
                    # using C-level str.find rather than a Python loop.
                    _lb = msg_str.find('[')
                    _lb = _lb if _lb != -1 else msg_str.find('{')
                    if _lb != -1:
                        _maybe = json.loads(msg_str[_lb:])
                        if isinstance(_maybe, list) and _maybe and isinstance(_maybe[0], str):
                            _evt_name = _maybe[0]
            except Exception:
                _evt_name = None

            if _evt_name == "authorization/reject":
                logger.warning(
                    "Websocket authorization rejected: %s", msg_str[:200]
                )
                # FIX (2026-07-27 / lost-error-detail): this used to be set to
                # the same generic "Websocket connection rejected." string as
                # every other WS failure (see _on_error below), so an actual
                # dead/expired token and a Cloudflare block on the connecting
                # IP were indistinguishable once they reached feed.py's logs.
                # This branch is the ONE case where Quotex itself responded
                # and explicitly rejected the session token — say so plainly.
                self.state.websocket_error_reason = (
                    "authorization rejected by Quotex "
                    "(token invalid, expired, or revoked)"
                )
                self.state.auth_status = AuthStatus.FAILED
                await self.event_registry.set_event(
                    "auth_changed", self.state.auth_status
                )
                return
            # `s_authorization` is handled by the dispatch table below
            # via `_h_auth_ok` — no need for a separate branch here.

            # Detect Socket.IO prefix
            is_control = msg_str and msg_str[0].isdigit()

            # Clean JSON extraction
            try:
                # FIX (DEEP-AUDIT-2026-07-26 / F-16-08): replaced
                # O(N) Python loop with C-level `str.find` to find the
                # first `[` or `{`. ~10x faster on hot paths.
                start_idx = msg_str.find('[')
                _brace_idx = msg_str.find('{')
                if start_idx == -1 or (
                    _brace_idx != -1 and _brace_idx < start_idx
                ):
                    start_idx = _brace_idx

                if start_idx != -1:
                    clean_json = msg_str[start_idx:]
                    data_json = json.loads(clean_json)
                    message = data_json
                else:
                    pass
            except Exception as e:
                logger.debug("Failed to parse raw data payload: %s", e)

            # 1. Handle Control Messages (Placeholders)
            if is_control:
                if "51-" in msg_str and "_placeholder" in msg_str:
                    self._temp_status = msg_str
                    return

                # Standard Event Processing — dispatch via table for O(1) lookup.
                # FIX (2026-07-27 / AUTH-FIX-ROOT-CAUSE): previously required
                # `len(message) > 1` to dispatch — but Quotex sends
                # `42["s_authorization"]` (length 1, no data payload) when
                # authentication succeeds. The dispatch never fired, so
                # `_h_auth_ok` was never called, leaving auth_status at
                # NOT_AUTHENTICATED even though auth actually succeeded.
                # This was the ROOT CAUSE of "connect returns False but
                # WS is actually connected and authorized" — the user saw
                # "Websocket connection rejected" but the truth was the
                # OPPOSITE: auth was successful but we failed to detect it.
                # Now: dispatch when message is a list with a string head,
                # regardless of length. Pass None as data if no payload.
                if (
                        isinstance(message, list)
                        and len(message) >= 1
                        and isinstance(message[0], str)
                ):
                    event = message[0]
                    data = message[1] if len(message) > 1 else None
                    handler = self._control_handlers.get(event)
                    if handler is not None:
                        await handler(data)
                    elif event in ('history/load', 'history/list/v2'):
                        # History response arrived as a direct control message
                        # (not via placeholder+binary). Fire the same candles_ready
                        # events as block 2 so _fetch_historical_batch() unblocks.
                        if isinstance(data, dict) and data.get("asset"):
                            _asset_key = data["asset"]
                            self.candle_v2_data[_asset_key] = data
                            self.slots.candle_v2(_asset_key).set(data)
                            await self.event_registry.set_event(
                                f'candles_ready_{_asset_key}', data
                            )
                            if data.get("index") is not None:
                                await self.event_registry.set_event(
                                    f'candles_ready_{_asset_key}_{data["index"]}',
                                    data,
                                )
                        elif isinstance(data, list):
                            await self.event_registry.set_event('history_ready', data)

            # 2. Handle Data Payloads (Placeholder fulfillment)
            elif message is not None and not is_control:
                data = (
                    message[0]
                    if isinstance(message, list) and len(message) == 1
                    else message
                )

                if self._temp_status and 'instruments/list' in self._temp_status:
                    if isinstance(data, list):
                        self.instruments = data
                    elif isinstance(data, dict) and "list" in data:
                        self.instruments = data["list"]

                    if self.instruments:
                        await self.event_registry.set_event(
                            'instruments_ready', self.instruments
                        )

                elif (
                        any(x in self._temp_status for x in ['history/list/v2', 'history/load'])
                        or (isinstance(data, dict) and (data.get("candles") or data.get("data")))
                ):
                    if isinstance(data, dict) and data.get("asset"):
                        asset = data["asset"]
                        self.candle_v2_data[asset] = data
                        if data is not None:
                            self.slots.candle_v2(asset).set(data)
                        await self.event_registry.set_event(
                            f'candles_ready_{asset}', data
                        )
                        if data.get("index") is not None:
                            await self.event_registry.set_event(
                                f'candles_ready_{asset}_{data["index"]}',
                                data
                            )
                    elif isinstance(data, list):
                        # Fallback for old history format if needed
                        await self.event_registry.set_event(
                            'history_ready', data
                        )

                elif self._temp_status and any(
                        x in self._temp_status
                        for x in [
                            'orders/open', 'orders/close', 'orders/opened',
                            'pending/create', 'pending/opened'
                        ]
                ):
                    logger.debug(
                        "Order event via placeholder! status=%s",
                        self._temp_status
                    )

                    # Handle both single dict and list of dicts
                    orders_to_process = []
                    if isinstance(data, list):
                        orders_to_process = data
                    elif isinstance(data, dict):
                        if data.get("deals"):
                            orders_to_process = data["deals"]
                        else:
                            orders_to_process = [data]

                    for order in orders_to_process:
                        order_id = order.get("id")
                        if order_id:
                            profit = order.get("profit", 0)
                            win = "win" if profit > 0 else "loss"
                            # Check if it's in a closed list or has a
                            # close status
                            is_closed = (
                                    any(
                                        x in self._temp_status
                                        for x in ['closed', 'close']
                                    )
                                    or order.get("status") == "closed"
                            )
                            game_state = 1 if is_closed else 0

                            logger.debug(
                                "Processing order %s: win=%s, state=%s, "
                                "profit=%s",
                                order_id, win, game_state, profit
                            )
                            self.listinfodata.set(
                                win, game_state, order_id, profit
                            )
                            self.listinfodata.set(
                                win, game_state, str(order_id), profit
                            )
                            # Fire keyed win_result slot when the order is
                            # closed (game_state == 1) so check_win() can
                            # resolve event-driven instead of polling.
                            if game_state == 1:
                                self.slots.win_result(str(order_id)).set(
                                    {"win": win, "profit": profit}
                                )

                    # FIX (DEEP-AUDIT-2026-07-26 / F-16-04): removed writes
                    # to dead attributes `buy_id`, `buy_successful`,
                    # `pending_id`, `pending_successful`. Slot writes are
                    # kept (those drive event-driven waiters); the attribute
                    # writes were only consumed by the now-removed trading
                    # methods. Slot writes retained so callers using
                    # `slots.buy_confirm.wait()` / `slots.pending_confirm.wait()`
                    # still resolve.
                    if (
                            any(x in self._temp_status for x in ['orders/open', 'pending/create'])
                            and isinstance(data, dict)
                    ):
                        if 'pending' in self._temp_status:
                            _pending_id = data.get("id")
                            if _pending_id is not None:
                                self.slots.pending_confirm.set({"id": _pending_id})
                            await self.event_registry.set_event(
                                'pending_confirmed', data
                            )
                        else:
                            _buy_id = data.get("id")
                            if _buy_id is not None:
                                self.slots.buy_confirm.set({"id": _buy_id})
                            await self.event_registry.set_event(
                                'buy_confirmed', data
                            )

                self._temp_status = ""  # Clear after consuming data

            if isinstance(message, dict):
                if message.get("liveBalance") or message.get("demoBalance"):
                    self.account_balance = message
                    if message is not None:
                        self.slots.balance.set(message)
                    await self.event_registry.set_event(
                        'balance_ready', message
                    )
                elif message.get("deals"):
                    # Handle real-time deals update (usually closed deals)
                    for order in message["deals"]:
                        order_id = order.get("id")
                        if order_id:
                            profit = order.get("profit", 0)
                            win = "win" if profit > 0 else "loss"
                            logger.debug(
                                "Real-time deal update for %s: "
                                "win=%s, profit=%s",
                                order_id, win, profit
                            )
                            self.listinfodata.set(win, 1, order_id, profit)
                            self.listinfodata.set(
                                win, 1, str(order_id), profit
                            )
                            # Always closed here; fire keyed win_result slot.
                            self.slots.win_result(str(order_id)).set(
                                {"win": win, "profit": profit}
                            )
                    await self.event_registry.set_event(
                        'history_ready', message
                    )

            elif (
                    isinstance(message, list)
                    and len(message) > 0
                    and isinstance(message[0], list)
            ):
                if len(message[0]) == 4:  # Price
                    asset, ts, price = (
                        message[0][0], message[0][1], message[0][2]
                    )
                    self.timesync.server_timestamp = ts  # Sync server clock

                    # FIX (DEEP-AUDIT-2026-07-26 / F-16-09): deque(maxlen=1000)
                    # auto-evicts; no manual pop(0) needed. O(1) per tick.
                    price_list = self.realtime_price[asset]
                    price_list.append({"time": ts, "price": price})

        except asyncio.CancelledError:
            # FIX (DEEP-AUDIT-2026-07-26 / F-16-10): re-raise CancelledError
            # explicitly. On Python 3.7 CancelledError was an Exception
            # subclass and would have been swallowed here, orphaning
            # coroutines on cancellation.
            raise
        except Exception as e:
            logger.error("Error in _on_message: %s", e)

    def _on_error(self, error: Exception | str) -> None:
        """
        Handles WebSocket errors.

        Args:
            error (Exception): The error that occurred.
        """
        logger.error(error)
        self.state.websocket_error_reason = str(error)
        self.state.status = WebsocketStatus.ERROR
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(
                    self.event_registry.set_event("status_changed", self.state.status)
                )
        except RuntimeError as _e:
            print(f"[silent-except] pyquotex/api.py:602 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass

    def _on_close(self, code: int, msg: str) -> None:
        """
        Handles WebSocket connection closure.

        Args:
            code (int): The closure code.
            msg (str): The closure message.
        """
        logger.info("Websocket connection closed.")
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-11): heartbeat task removed.
        self.state.status = WebsocketStatus.DISCONNECTED
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(
                    self.event_registry.set_event("status_changed", self.state.status)
                )
        except RuntimeError as _e:
            print(f"[silent-except] pyquotex/api.py:622 {type(_e).__name__}: {_e}")  # FIX (CRASH-FIX-2026-07-26 / EXC-003): was silent `pass`
            pass

    @property
    def websocket(self) -> Any:
        """
        Returns the active WebSocket instance.

        Returns:
            websockets.WebSocketClientProtocol: The active WebSocket
                connection or None.
        """
        return self.websocket_client.wss if self.websocket_client else None

    async def get_instruments(self) -> None:
        """Sends a request to the WebSocket to retrieve the list of
        available instruments."""
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-12): send `instruments/list` to
        # match the response event name in the dispatch table (old code
        # sent `instruments/get` which has no matching handler).
        if self.websocket:
            await self.websocket.send('42["instruments/list"]')

    async def authenticate(self) -> tuple[bool, str]:
        """
        Authenticates the user using the provided credentials.

        Performs HTTP login, retrieves cookies and SSID token,
        and updates the browser session.

        Returns:
            tuple[bool, str]: (Success status, Error message or "Success").
        """
        async with self.login as login:
            status, msg = await login(
                self.username, self.password, self.user_data_dir
            )
        if status:
            self.state.SSID = self.session_data.get("token")
            self.is_logged = True
            # Sync session to browser client
            # FIX (DEEP-AUDIT-2026-07-26 / F-16-13): guard against
            # `cookies` being None — common on first login attempt before
            # a session is established. Old code crashed with
            # AttributeError: 'NoneType' object has no attribute 'split'.
            cookie_str = self.session_data.get("cookies") or ""
            for item in cookie_str.split("; "):
                if "=" in item:
                    k, v = item.split("=", 1)
                    self.browser._client.cookies.set(
                        k, v, domain=self.host
                    )

            self.browser.headers.update({
                "User-Agent": self.session_data.get("user_agent", ""),
                "Referer": f"{self.https_url}/{self.lang}/trade"
            })
        return status, msg

    # FIX (DEEP-AUDIT-2026-07-26 / F-16-14): removed the duplicate
    # `send_http_request_v2` method (identical body to `_v1`). Use
    # `send_http_request_v1` going forward.
    async def send_http_request_v1(
            self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Sends an HTTP request using the internal browser client."""
        # Browser.send_request uses self._client.request internally
        return await self.browser.send_request(method, url, **kwargs)

    async def send_websocket_request(self, data: str) -> None:
        """
        Sends a raw string request to the WebSocket.
        Uses a lock to ensure thread-safe sending.

        Args:
            data (str): The raw Socket.IO string to send.
        """
        async with self._ws_send_lock:
            if self.websocket:
                await self.websocket.send(data)

    async def check_connect(self) -> bool:
        """Checks if the WebSocket is currently connected."""
        return self.state.status == WebsocketStatus.CONNECTED

    async def settings_apply(
            self,
            asset: str,
            period: int,
            is_fast_option: bool = False,
            end_time: int | None = None,
            deal=5,
            percent_mode=False,
            percent_deal=1
    ) -> None:
        """Apply asset and time settings before placing an order."""
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-15): stripped the Quotex
        # web-app UI preferences from the payload (chartType, chartPeriod,
        # gridOpacity, upColor, etc.) — server ignores them, and including
        # them wastes bandwidth. Sending only the API-required fields:
        # currentAsset, currentExpirationTime, dealValue.
        payload = {
            "chartId": "graph",
            "settings": {
                "chartId": "graph",
                "currentExpirationTime": (
                    int(time.time()) if not is_fast_option else (end_time or 0)
                ),
                "isFastOption": is_fast_option,
                "isFastAmountOption": percent_mode,
                "currentAsset": {
                    "symbol": asset
                },
                "dealValue": deal,
                "dealPercentValue": percent_deal,
                "timePeriod": period,
            }
        }
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-15): use `is not None` so a
        # legitimate `end_time=0` (Unix epoch) is still serialized.
        if end_time is not None:
            payload["endTime"] = end_time

        # FIX (DEEP-AUDIT-2026-07-26 / F-16-16): build the Socket.IO frame
        # by serializing the whole `[event, payload]` list with json.dumps
        # so any nested characters in payload are correctly escaped. Old
        # code used string concatenation which could mis-parse.
        data = "42" + json.dumps_str(["settings/apply", payload])
        await self.send_websocket_request(data)

    async def subscribe_realtime_candle(self, asset: str, period: int) -> None:
        """Subscribes to real-time price updates for a specific asset
        and period."""
        payload = {"asset": asset, "period": period}
        data = f'42["instruments/update", {json.dumps_str(payload)}]'
        await self.send_websocket_request(data)

    async def chart_notification(self, asset: str) -> None:
        """Requests chart notifications for a specific asset."""
        payload = {"asset": asset, "version": "1.0.0"}
        payload_json = json.dumps_str(payload)
        data = f'42["chart_notification/get", {payload_json}]'
        await self.send_websocket_request(data)

    async def follow_candle(self, asset: str) -> None:
        """Starts following the depth of market for a specific asset."""
        data = f'42["depth/follow", {json.dumps_str(asset)}]'
        await self.send_websocket_request(data)

    async def unfollow_candle(self, asset: str) -> None:
        """Stops following the depth of the market for a specific asset."""
        data = f'42["depth/unfollow", {json.dumps_str(asset)}]'
        await self.send_websocket_request(data)

    # FIX (DEEP-AUDIT-2026-07-26 / F-16-19): removed dead
    # `signals_subscribe` method — no caller anywhere in this codebase.

    async def change_account(
            self,
            account_type: AccountType,
            tournament_id: int = 0
    ) -> None:
        """
        Change active trading account.

        Args:
            account_type:
                REAL or DEMO account.

            tournament_id:
                Tournament/training id.
                Default 0 disables tournament mode.
        """

        self.account_type = account_type
        self.tournament_id = tournament_id

        payload = {
            "demo": int(account_type),
            "tournamentId": tournament_id
        }

        data = f'42["account/change",{json.dumps_str(payload)}]'

        await self.send_websocket_request(data)

    async def edit_training_balance(self, amount: float | int) -> None:
        """Refills the demo account balance."""
        data = f'42["demo/refill",{json.dumps_str(amount)}]'
        await self.send_websocket_request(data)

    async def change_time_offset(self, time_offset: int) -> dict[str, Any]:
        """Changes the account time offset."""
        return await self.settings.set_time_offset(time_offset)

    async def unsubscribe_realtime_candle(self, asset: str) -> None:
        """Unsubscribes from real-time price updates for a specific asset."""
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-17): use `depth/unfollow` as
        # the inverse of `depth/follow` (subscribe_realtime_candle path).
        # Old code sent `instruments/unsubscribe` which is NOT a real
        # Quotex event and was silently ignored by the server.
        data = f'42["depth/unfollow", {json.dumps_str(asset)}]'
        await self.send_websocket_request(data)

    async def subscribe_Traders_mood(self, asset: str, instrument: str) -> None:
        """Subscribes to traders' mood/sentiment for a specific asset."""
        payload = {"asset": asset, "instrument": instrument}
        data = f'42["sentiment/subscribe", {json.dumps_str(payload)}]'
        await self.send_websocket_request(data)

    async def subscribe_all_size(self, asset: str) -> None:
        """Subscribes to all candle sizes for a specific asset."""
        payload = {"asset": asset}
        data = f'42["history/subscribe_all", {json.dumps_str(payload)}]'
        await self.send_websocket_request(data)

    async def get_history_line(
            self,
            asset: str,
            index: int,
            time_from: float,
            offset: int
    ) -> None:
        """Requests historical price line data."""
        payload = {
            "asset": asset,
            "index": index,
            "time": time_from,
            "offset": offset
        }
        data = f'42["history/load", {json.dumps_str(payload)}]'
        await self.send_websocket_request(data)

    async def open_pending(
            self,
            amount: float | int,
            asset: str,
            direction: str,
            duration: int,
            open_time: int
    ) -> None:
        """Places a pending order to be executed at a specific future time."""
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-18): use the monotonically-
        # increasing `_request_counter` instead of `int(time.time())` —
        # two pending orders created within the same wall-clock second
        # used to share the same requestId, which Quotex may deduplicate
        # as a replay.
        from pyquotex._api._constants import _request_counter
        payload = {
            "asset": asset,
            "amount": amount,
            "action": direction,
            "time": duration,
            "openTime": open_time,
            "isDemo": int(self.account_type) if self.account_type is not None else AccountType.DEMO,
            "tournamentId": self.tournament_id,
            "requestId": next(_request_counter)
        }
        data = "42" + json.dumps_str(["pending/create", payload])
        await self.send_websocket_request(data)

    # FIX (DEEP-AUDIT-2026-07-26 / F-16-19): removed dead `instruments_follow`
    # method — was an alias for `open_pending` with no caller anywhere in
    # this codebase.

    async def start_websocket(self) -> tuple[bool, str]:
        """
        Initializes and starts the WebSocket connection.
        Attempts to authenticate if no SSID is present.

        Returns:
            tuple[bool, str]: (Success status, Connection status message).
        """
        self.state.status = WebsocketStatus.CONNECTING
        self.state.auth_status = AuthStatus.NOT_AUTHENTICATED
        await self.event_registry.set_event("status_changed", self.state.status)
        if not self.state.SSID:
            await self.authenticate()

        self.websocket_client = WebsocketClient(
            self, reconnect_policy=self.reconnect_policy
        )

        # Ensure we have a valid User-Agent, fallback to a modern one if missing
        ua = (
                self.session_data.get("user_agent")
                or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
        )

        extra_headers = {
            "User-Agent": ua,
            "Origin": self.https_url,
            "Referer": f"{self.https_url}/{self.lang}/trade",
            "Cookie": self.session_data.get("cookies", ""),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            # FIX (DEEP-AUDIT-2026-07-26 / F-16-20): removed the
            # `Sec-WebSocket-Extensions: permessage-deflate` header —
            # `WebsocketClient._connect_once` passes `compression=None`
            # which contradicts this header and could close the handshake
            # on strict servers. Choose one (we keep no-compression).
        }
        # Skip SSL for plain ws:// (test / proxy / dev) — the websockets
        # library expects no SSLContext on a non-TLS URL.
        ssl_ctx = (
            self.browser._ssl_context
            if self.wss_url.startswith("wss://")
            else None
        )
        self._websocket_task = asyncio.create_task(
            self.websocket_client.run_forever(
                url=self.wss_url,
                extra_headers=extra_headers,
                ssl=ssl_ctx,
            )
        )
        for _ in range(100):
            if self.state.status == WebsocketStatus.ERROR:
                # FIX (DEEP-AUDIT-2026-07-26 / F-16-21): cancel the orphan
                # `_websocket_task` before returning on error so we don't
                # leak background tasks.
                if self._websocket_task and not self._websocket_task.done():
                    self._websocket_task.cancel()
                return False, self.state.websocket_error_reason
            if self.state.status == WebsocketStatus.CONNECTED:
                return True, "Connected"
            await asyncio.sleep(0.1)
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-21): same cleanup on the
        # timeout path — old code returned "Timeout" but left
        # `_websocket_task` running, leaking tasks on repeated connect.
        if self._websocket_task and not self._websocket_task.done():
            self._websocket_task.cancel()
        return False, "Timeout"

    async def send_ssid(self) -> bool:
        """Sends the SSID token to the WebSocket to authorize the
        connection."""
        if not self.state.SSID: return False
        await self.ssid(self.state.SSID)
        return True

    async def connect(self, is_demo: bool) -> tuple[bool, str]:
        """
        Connects to the Quotex platform.

        Args:
            is_demo (bool): True to connect to a DEMO account, False for REAL.

        Returns:
            tuple[bool, str]: (Connection success, Status message).
        """
        self.account_type = (
            AccountType.DEMO if is_demo else AccountType.REAL
        )
        ok, reason = await self.start_websocket()
        if ok: await self.send_ssid()
        return ok, reason

    async def close(self) -> bool:
        """Closes the WebSocket connection and the HTTP client session."""
        if self.websocket_client:
            await self.websocket_client.close()
            # Explicitly trigger cleanup to ensure connection state is reset.
            self._on_close(1000, "Graceful closure")
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-22): removed the commented-out
        # `_http_client` block — dead code. The HTTP client lives inside
        # `self.browser` and is closed via `await self.browser.close()`
        # when the Quotex wrapper tears down.
        return True

    # FIX (DEEP-AUDIT-2026-07-26 / F-16-23): removed dead `logout`,
    # `buy`, `sell_option`, `get_history` properties — they returned
    # instances of the now-deleted stub classes (Logout, Buy, SellOption,
    # GetHistory). `login`, `ssid`, `get_candles` are kept because their
    # underlying classes are real (not stubs) and used by feed.py and
    # the auth flow.
    @property
    def login(self) -> Login:
        """Returns the Login action handler."""
        return Login(self)

    @property
    def ssid(self) -> Ssid:
        """Returns the SSID authorization handler."""
        return Ssid(self)

    @property
    def get_candles(self) -> GetCandles:
        """Returns the Candles retrieval handler."""
        return GetCandles(self)

    async def get_profile(self) -> Profile:
        """
        Retrieves and parses the user profile data.

        Updates the internal profile object with nickname, balances,
        country, and timezone.

        Returns:
            Profile: The updated profile object.
        """
        user_settings = await self.settings.get_settings()
        d = user_settings.get("data", {})
        self.profile.nick_name = d.get("nickname")
        self.profile.profile_id = d.get("id")
        self.profile.demo_balance = float(d.get("demoBalance", 0))
        self.profile.live_balance = float(d.get("liveBalance", 0))
        self.profile.currency_code = d.get("currencyCode")
        self.profile.currency_symbol = d.get("currencySymbol")
        self.profile.country_name = d.get("countryName")
        self.profile.offset = d.get("timeOffset")
        return self.profile

    # FIX (DEEP-AUDIT-2026-07-26 / F-16-24): removed dead `get_trader_history`
    # method — it called the now-deleted `GetHistory` stub class which is
    # not callable (`TypeError: 'GetHistory' object is not callable`).
    # feed.py never uses this method.
