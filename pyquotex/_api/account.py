"""Account-related methods extracted from Quotex.

This mixin is composed into Quotex via multiple inheritance. It uses
self.api, self.session_data, self.account_is_demo, etc. — all set up in
Quotex.__init__ inside pyquotex/stable_api.py.

NOTE: Several account methods were removed 2026-07-13 because feed.py
(the only consumer of Quotex in this app) only uses connect() and
check_connect() (the latter is defined on Quotex in stable_api.py,
not here). The app is read-only signal analysis — it never queries
balance, profile, server time, or modifies account mode.

Methods removed (with cross-check confirming zero external callers):
  - reconnect()                  — only self.api.authenticate(); no caller
  - set_account_mode(mode)       — only sets self.account_is_demo; no caller
  - change_account(mode, tid)    — wraps self.api.change_account; no caller
  - change_time_offset(offset)   — wraps self.api.change_time_offset; no caller
  - edit_practice_balance(...)   — wraps self.api.edit_training_balance; no caller
  - get_balance(timeout)         — relied on get_profit() (also removed); no caller
  - get_profile()                — wrapped self.api.get_profile; no external caller
  - get_server_time()            — called get_profile(); no external caller
  - start_remaing_time()         — debug helper; no caller
  - store_settings_apply(...)    — wraps self.api.settings_apply; no caller

Kept: connect() — feed.py's only entry point. It only references
self.check_connect() (defined on Quotex in stable_api.py) and
self.api.* fields, so it has no dependency on the removed methods.
"""
from __future__ import annotations

import logging

from pyquotex.api import QuotexAPI
from pyquotex.utils.account_type import AccountType

logger = logging.getLogger(__name__)


class AccountMixin:
    """Methods related to account state, profile, balance, and session."""

    async def connect(self) -> tuple[bool, str]:
        """Establishes a connection to the Quotex API."""
        if self.api and await self.check_connect():
            return True, "Already connected"
        self.api = QuotexAPI(
            self.host,
            self.email,
            self.password,
            self.lang,
            resource_path=self.resource_path,
            user_data_dir=self.user_data_dir,
            proxies=self.proxies,
            on_otp_callback=self.on_otp_callback,
            reconnect_policy=getattr(self, "reconnect_policy", None),
            wss_url_override=getattr(self, "wss_url_override", None),
        )

        self.api.trace_ws = self.debug_ws_enable
        self.api.session_data = self.session_data
        self.api.current_asset = self.asset_default
        self.api.current_period = self.period_default
        self.api.state.SSID = self.session_data.get("token")

        if not self.session_data.get("token"):
            check, reason = await self.api.authenticate()
            if not check:
                return check, reason

        check, reason = await self.api.connect(self.account_is_demo == AccountType.DEMO)
        if not await self.check_connect():
            logger.error(
                "Websocket failed to connect or connection was rejected."
            )
            if "token" in self.session_data:
                self.session_data["token"] = None
            return False, "Websocket connection rejected."

        return check, reason
