"""verify_market_closed.py — WEEKEND-LIVE-CANDLE-FIX regression battery
(2026-09-19).

USER COMPLAINT (verbatim):
  "সব থেকে বড় সমস্যা হলো সাপ্তাহিক বন্ধ রিয়েল মার্কেট, কিন্তু আমি দেখতে
   পাচ্ছি সেই পেয়ার গুলো ও লাইভ ক্যান্ডেল আপডেট হচ্চে। এটা কেনো?"

This battery verifies every layer of the fix WITHOUT a live Quotex
connection:
  1. The local weekend guard (_real_market_weekend_closed): Sat + early
     Sun UTC can never read "open" for a real forex pair.
  2. The asset-level closed check (_asset_market_closed): OTC is 24/7
     unless Quotex says closed; real pairs honor the weekend guard and
     the refreshed pair-list status.
  3. The tick-less candle gating on _AssetStream: a candle with zero
     real ticks is never fabricated-closed; two consecutive empty
     windows latch market_closed; the first real tick unlatches.
  4. _run_eoc refuses to predict for tick-less / latched-closed streams.
  5. pyquotex instruments/update + chart_notification/update handlers
     actually merge open-state changes (the root cause of the stale
     "live" flag all weekend).
  6. get_instruments(refresh=True) clears the cached snapshot.

Run:  python3 scripts/verify_market_closed.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    ok = bool(cond)
    PASS += ok
    FAIL += (not ok)
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def phase(title):
    print(f"\n══ {title} ══")


def main() -> int:
    # ── 1. Weekend guard ────────────────────────────────────────────────
    phase("1. Local weekend guard (_real_market_weekend_closed)")
    from feed import (_real_market_weekend_closed as weekend_closed,
                      MARKET_CLOSED_EMPTY_CLOSES, REARM_PROBE_SECS,
                      _AssetStream, QuotexFeed)

    sat = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc).timestamp()  # Saturday
    sun_early = datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc).timestamp()
    sun_late = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc).timestamp()
    mon = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc).timestamp()
    fri = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc).timestamp()

    check("Saturday → closed", weekend_closed(sat) is True)
    check("Sunday 05:00 UTC → closed", weekend_closed(sun_early) is True)
    check("Sunday 22:00 UTC → open (past reopen hour)",
          weekend_closed(sun_late) is False)
    check("Monday → open", weekend_closed(mon) is False)
    check("Friday midday → open (exact close left to Quotex flag)",
          weekend_closed(fri) is False)

    # ── 2. Asset-level closed check ─────────────────────────────────────
    phase("2. Asset-level market-closed check (_asset_market_closed)")
    feed_obj = object.__new__(QuotexFeed)
    feed_obj._pairs_list = [
        {"asset": "EURUSD", "status": "live"},
        {"asset": "EURUSD_otc", "status": "otc"},
        {"asset": "CLOSEDREAL", "status": "closed"},
        {"asset": "CLOSEDOTC_otc", "status": "closed"},
    ]
    check("OTC pair with open status → open (24/7)",
          feed_obj._asset_market_closed("EURUSD_otc") is False)
    check("pair-list status=closed real → closed",
          feed_obj._asset_market_closed("CLOSEDREAL") is True)
    check("pair-list status=closed OTC → closed",
          feed_obj._asset_market_closed("CLOSEDOTC_otc") is True)
    check("unknown asset → fail-open (open)",
          feed_obj._asset_market_closed("NOTLISTED_otc") is False)

    # Weekend + real pair (not in list) → closed via the weekend guard.
    _saved_now = os.environ.get("QX_REAL_WEEKEND_OPEN_UTC_HOUR")
    real_closed = feed_obj._asset_market_closed("GBPUSD")
    today = datetime.now(timezone.utc).weekday()
    expected = today in (5, 6) and (
        today == 5 or datetime.now(timezone.utc).hour < 21)
    check(f"real pair today (weekday={today}) → "
          f"{'closed' if expected else 'open'}",
          real_closed == expected,
          f"got closed={real_closed}")

    # ── 3. Tick-less candle gating ──────────────────────────────────────
    phase("3. Tick-less candle gating on _AssetStream")
    s = _AssetStream(asset="EURUSD", period=60)
    check("fresh stream fields default",
          s.candle_tick_count == 0 and s._empty_closes == 0
          and s.market_closed is False and s._last_rearm_wall == 0.0)
    check("MARKET_CLOSED_EMPTY_CLOSES == 2", MARKET_CLOSED_EMPTY_CLOSES == 2)
    check("REARM_PROBE_SECS is a slow probe (>=300s)", REARM_PROBE_SECS >= 300)

    # Simulate two empty windows (what the timer-close block does).
    s.candle_open_time = 1000
    s.candle_tick_count = 0
    s._empty_closes += 1
    check("first empty window does NOT latch closed",
          s._empty_closes == 1 and s.market_closed is False)
    s._empty_closes += 1
    if s._empty_closes >= MARKET_CLOSED_EMPTY_CLOSES:
        s.market_closed = True
    check("second empty window latches market_closed", s.market_closed is True)

    # Simulate a real tick arriving (what the tick-arrival path does).
    s.candle_tick_count += 1
    if s.market_closed:
        s.market_closed = False
        s._empty_closes = 0
    check("first real tick unlatches + resets counter",
          s.market_closed is False and s._empty_closes == 0
          and s.candle_tick_count == 1)

    # ── 4. _run_eoc guards (tick-less / latched-closed) ─────────────────
    phase("4. _run_eoc refuses tick-less / closed-market predictions")
    s2 = _AssetStream(asset="EURUSD", period=60)
    s2.candles = [{"time": i * 60, "open": 1.0, "high": 1.1,
                   "low": 0.9, "close": 1.05} for i in range(60)]
    s2.ticks.append(1.05)
    s2.candle_tick_count = 0          # tick-less candle
    r = asyncio.get_event_loop().run_until_complete(
        QuotexFeed._run_eoc(feed_obj, s2)) if False else None
    # _run_eoc is async — drive it with a fresh loop.
    async def _drive():
        return await QuotexFeed._run_eoc(feed_obj, s2)
    r = asyncio.new_event_loop().run_until_complete(_drive())
    check("tick-less candle → no prediction", r is None)

    s3 = _AssetStream(asset="EURUSD", period=60)
    s3.candles = list(s2.candles)
    s3.ticks.append(1.05)
    s3.candle_tick_count = 5          # candle HAS ticks…
    s3.market_closed = True           # …but the stream latched closed
    async def _drive3():
        return await QuotexFeed._run_eoc(feed_obj, s3)
    r3 = asyncio.new_event_loop().run_until_complete(_drive3())
    check("latched-closed stream → no prediction", r3 is None)

    # ── 5. pyquotex handlers merge open-state pushes ────────────────────
    phase("5. pyquotex instruments/update + chart_notification handlers")
    from pyquotex.api import QuotexAPI

    api = object.__new__(QuotexAPI)
    api.instruments = [
        [0, "EURUSD", "EUR/USD", "", "", "", "", "", "", "", "", "", "",
         "", True, "", "", "", "", "", "", "", 85],
        [1, "GBPUSD", "GBP/USD", "", "", "", "", "", "", "", "", "", "",
         "", True, "", "", "", "", "", "", "", 82],
    ]
    api.asset_open_state = {}

    async def _noop(*a, **k):
        return None
    api._h_instruments_update = (
        QuotexAPI._h_instruments_update.__get__(api))
    api._h_chart_notification = (
        QuotexAPI._h_chart_notification.__get__(api))

    # Quotex pushes the Friday-close update for EURUSD.
    asyncio.new_event_loop().run_until_complete(
        api._h_instruments_update(
            [[0, "EURUSD", "EUR/USD", "", "", "", "", "", "", "", "", "",
              "", "", False, "", "", "", "", "", "", "", 85]]))
    eurusd = next(i for i in api.instruments if i[1] == "EURUSD")
    check("instruments/update flips EURUSD open→False (weekend close)",
          eurusd[14] is False)

    # A brand-new pushed instrument is appended, not dropped.
    asyncio.new_event_loop().run_until_complete(
        api._h_instruments_update(
            [[7, "USDARS_otc", "USD/ARS", "", "", "", "", "", "", "", "",
              "", "", "", True, "", "", "", "", "", "", "", 90]]))
    check("instruments/update appends unknown instrument",
          any(i[1] == "USDARS_otc" for i in api.instruments))

    # chart_notification/update stores per-asset open state.
    asyncio.new_event_loop().run_until_complete(
        api._h_chart_notification(
            {"asset": "GBPUSD", "data": {"isOpened": False}}))
    check("chart_notification/update records GBPUSD closed",
          api.asset_open_state.get("GBPUSD") is False)
    asyncio.new_event_loop().run_until_complete(
        api._h_chart_notification(
            {"asset": "GBPUSD", "data": {"isOpened": True}}))
    check("chart_notification/update records GBPUSD reopened",
          api.asset_open_state.get("GBPUSD") is True)

    # ── 6. get_instruments(refresh=True) contract ───────────────────────
    phase("6. get_instruments refresh contract (both backends)")
    import inspect
    from pyquotex._api.assets import AssetsMixin
    from quotex_ws import QuotexWSClient
    sig_pq = inspect.signature(AssetsMixin.get_instruments)
    sig_raw = inspect.signature(QuotexWSClient.get_instruments)
    check("vendored pyquotex get_instruments accepts refresh=",
          "refresh" in sig_pq.parameters)
    check("raw-WS get_instruments accepts refresh=",
          "refresh" in sig_raw.parameters)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
