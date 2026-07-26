import asyncio
import logging
from typing import Any, Callable

from ._api.account import AccountMixin
from ._api.assets import AssetsMixin
from ._api.history import HistoryMixin
from ._api.realtime import RealtimeMixin
from .api import QuotexAPI
from .config import load_session, resource_path, update_session
from .global_value import AuthStatus
from .types import ReconnectPolicy
from .utils.account_type import AccountType

# FIX (DEEP-AUDIT-2026-07-26 / F-16-25): removed `TradingMixin` and
# `OptimizedQuotexMixin` from the inheritance chain — both classes were
# reduced to `pass` after the 2026-07-13 trading-cleanup. The classes
# themselves have been removed and the modules kept as empty markers.

logger = logging.getLogger(__name__)


class Quotex(
    AccountMixin,
    HistoryMixin,
    RealtimeMixin,
    AssetsMixin,
):

    def __init__(
            self,
            email: str,
            password: str,
            host: str = "qxbroker.com",
            lang: str = "pt",
            user_agent: str = "Quotex/1.0",
            root_path: str = ".",
            user_data_dir: str = "browser",
            asset_default: str = "EURUSD",
            period_default: int = 60,
            proxies: dict[str, str] | None = None,
            on_otp_callback: Callable | None = None,
            reconnect_policy: ReconnectPolicy | None = None,
            wss_url_override: str | None = None,
    ):
        """
        Initializes the Quotex stable API wrapper.

        Args:
            email (str): User email.
            password (str): User password.
            host (str): Broker hostname. Defaults to "qxbroker.com".
            lang (str): Language code. Defaults to "pt".
            user_agent (str): Browser User-Agent. Defaults to "Quotex/1.0".
            root_path (str): Root directory for local storage. Defaults to ".".
            user_data_dir (str): Directory for browser profile data.
                Defaults to "browser".
            asset_default (str): Default asset to use. Defaults to "EURUSD".
            period_default (int): Default candle period in seconds.
                Defaults to 60.
            proxies (dict, optional): Proxy configuration.
            on_otp_callback (callable, optional): Callback for 2FA/OTP input.
            reconnect_policy (ReconnectPolicy, optional): Auto-reconnect /
                stale-detection configuration. Defaults to enabled with
                exponential backoff. Pass ``ReconnectPolicy(enabled=False)``
                to opt out.
        """
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-26): removed dead attributes
        # `size`, `subscribe_candle`, `subscribe_candle_all_size`,
        # `subscribe_mood`, `suspend`, `websocket_thread`,
        # `debug_ws_enable`. These were only consumed by the now-deleted
        # `re_subscribe_stream` method (also removed below) and the
        # removed trading/realtime one_stream/all_size/mood helpers.
        # Kept: email, password, host, lang, proxies, resource_path,
        # user_data_dir, asset_default, period_default, account_is_demo,
        # codes_asset (still populated by AssetsMixin.get_all_assets),
        # api, duration, websocket_client, session_data, on_otp_callback,
        # reconnect_policy, wss_url_override.
        self.email = email
        self.password = password
        self.host = host
        self.lang = lang
        self.proxies = proxies
        self.resource_path = root_path
        self.user_data_dir = user_data_dir
        self.asset_default = asset_default
        self.period_default = period_default
        self.account_is_demo: int = AccountType.DEMO
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-26): `codes_asset` is still
        # populated by AssetsMixin.get_all_assets; keep it. (Note: the
        # broken `get_history_line` method that wrongly indexed this is
        # deleted — see history.py.)
        self.codes_asset: dict[str, str] = {}
        self.api: QuotexAPI | None = None
        self.duration: int | None = None
        self.websocket_client: Any = None
        self.resource_path = resource_path(root_path)
        session = load_session(self.email, user_agent)
        self.session_data = session
        self.on_otp_callback = on_otp_callback
        self.reconnect_policy = reconnect_policy or ReconnectPolicy()
        self.wss_url_override = wss_url_override

    @property
    def websocket(self) -> Any:
        """Property to get websocket.
        :returns: The active WebSocket instance.
        """
        return self.api.websocket if self.api else None

    @staticmethod
    async def _check_connect(state: Any) -> bool:
        """Check connection using the per-instance state object.

        Waits up to ~2s for the state to settle on AUTHENTICATED; returns
        as soon as the predicate is satisfied (event-driven path) or False
        on timeout. Replaces an unconditional ``await asyncio.sleep(2)``.
        """
        from pyquotex._api._waits import wait_until
        try:
            await wait_until(
                lambda: state.auth_status == AuthStatus.AUTHENTICATED,
                timeout=2,
                poll_interval=0.05,
            )
            return True
        except asyncio.TimeoutError:
            return state.auth_status == AuthStatus.AUTHENTICATED

    async def check_connect(self) -> bool:
        """Check connection using the current API's state."""
        if self.api is None:
            return False
        return await self._check_connect(self.api.state)

    def set_session(
            self,
            user_agent: str,
            cookies: str | None = None,
            ssid: str | None = None
    ) -> None:
        """
        Manually sets the session data.

        Args:
            user_agent (str): The User-Agent string.
            cookies (str, optional): The raw cookie string.
            ssid (str, optional): The SSID token.
        """
        session = {
            "cookies": cookies,
            "token": ssid,
            "user_agent": user_agent
        }
        self.session_data = update_session(self.email, session)
        # FIX (DEEP-AUDIT-2026-07-26 / F-16-27): propagate session_data
        # to the live QuotexAPI instance too. Old code only updated
        # `self.session_data`; the api's `session_data` attribute was
        # a separate reference and went stale, so callers using
        # `set_session` after `connect()` saw the api ignore the new
        # cookies/token.
        if self.api is not None:
            self.api.session_data = self.session_data

    # FIX (DEEP-AUDIT-2026-07-26 / F-16-28): removed dead
    # `re_subscribe_stream` method — it was only called by the now-deleted
    # trading/realtime one_stream/all_size/mood helpers. The
    # WebsocketClient's `_replay_subscriptions` (event-driven via
    # `_subscriptions` dict on QuotexAPI) replaces this functionality.

    async def close(self) -> bool:
        """Closes the API connection and stops all tasks."""
        if self.api:
            return await self.api.close()
        return True

    async def __aenter__(self) -> "Quotex":
        """Async context manager: connects on enter."""
        ok, reason = await self.connect()
        if not ok:
            raise ConnectionError(f"Quotex connect failed: {reason}")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Async context manager: closes the connection on exit."""
        await self.close()
