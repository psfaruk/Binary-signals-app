"""Trading-related methods extracted from Quotex.

This mixin is composed into Quotex via multiple inheritance. It uses
self.api, self.account_is_demo, etc. — all set up in Quotex.__init__
inside pyquotex/stable_api.py.

NOTE: All trade-placement and result-querying methods were removed
2026-07-13 because the app (Binary-signals-app) is read-only signal
analysis — it never places trades. feed.py only uses Quotex methods
related to instrument discovery, candle streaming, and tick polling
(connect, check_connect, set_session, close, session_data,
get_instruments, get_payout_by_asset, get_candles,
get_historical_candles, start_candles_stream, stop_candles_stream,
get_realtime_price).

The previously removed methods were:
  - buy(amount, asset, direction, duration, time_mode)
  - open_pending(amount, asset, direction, duration, open_time)
  - sell_option(options_ids, timeout)
  - check_win(order_id, duration)
  - get_result(operation_id)
  - get_profit()
  - get_history()

Cross-check before removal confirmed none of these are referenced
anywhere outside the trading.py file itself (the api.py side still
retains buy/sell_option/open_pending/etc. on the QuotexAPI class
because the WS message handlers update buy_id, pending_id,
sold_options_respond, listinfodata, profit_in_operation — those are
internal QuotexAPI fields, not mixin method calls).
"""
from __future__ import annotations


class TradingMixin:
    """Trade methods removed 2026-07-13 — app is read-only signal analysis."""

    pass
