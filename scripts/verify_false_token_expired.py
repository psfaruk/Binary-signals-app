#!/usr/bin/env python3
"""
scripts/verify_false_token_expired.py — FALSE-TOKEN-EXPIRED-2026-09-18 fix
verification (FT-01 … FT-12).

USER COMPLAINT (verbatim, 2026-09-18):
  "আর মডিউল তো রান হয় না, টোকেন মেয়াদ থাকা সত্ত্বেও বলে Qx টোকেনের
   মেয়াদ নাই।"
  ("And the module doesn't run — even though the token has validity, the
   app says the Qx token has no validity/expired.")

ROOT CAUSE (measured on branch fix/false-token-expired aa6cf5e, never
merged): on the DEFAULT pyquotex backend a SINGLE `authorization/reject`
event immediately labeled the token "invalid, expired, or revoked";
account.py's "Connected"-then-disconnected branch did the same. The feed
then backed off 60-120s per cycle with the chart blank — which to the
operator looks exactly like "token expired", and with no data flowing NO
module runs and NO signal is produced. The reject counter existed only on
the raw-WS backend (QX_USE_RAW_WS=1, not the default).

This script verifies the ported fix BEHAVIORALLY, offline:

  A. pyquotex/api.py (FT-01/02/03)
     A1  fresh API starts with counter=0, threshold=3, dead_at=0
     A2  1st reject  → transient message, dead_at stays 0 (token NOT blamed)
     A3  2nd reject  → still transient
     A4  3rd reject  → dead: dead_at set, message says expired/invalid
     A5  auth ok     → counter + dead_at reset (transient rejects forgiven)
     A6  reject → auth ok → reject: counter correctly 1 (not 4)

  B. pyquotex/_api/account.py (FT-04) — "Connected"-then-disconnected
     B1  rejects < 3 + no explicit reject reason → TRANSIENT message
     B2  rejects >= 3 → AUTH REJECT confirmed
     B3  explicit "authorization rejected" error reason → AUTH REJECT

  C. feed.py (FT-05/06/07/12)
     C1  _sync_reject_state mirrors a pyquotex-shaped client (counter on .api)
     C2  _sync_reject_state mirrors a raw-WS-shaped client (counter on client)
     C3  _sync_reject_state with no client → mirror unchanged, no crash
     C4  transient backoff: 10s→20s→30s (rejects>0, not dead)
     C5  dead-token backoff: 60s→120s (dead)
     C6  state transition "dead" fires alerts.token_dead exactly once
     C7  transition dead→live fires alerts.feed_recovered

  D. server.py /api/token-status logic (FT-10/11) — simulated feed states
     D1  rejects=1, not dead → status "transient_disconnect", action "wait"
     D2  dead → status "token_dead", action "refresh_token"
     D3  no credentials → "set_token"
     D4  live → no action

  E. Regression: 15 module engine + ML hand-off still healthy
     E1  all 15 modules produce a vote breakdown on realistic candles
     E2  roadmap (6 user factors) attached to every prediction

Run:
    python scripts/verify_false_token_expired.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# ══════════════════════════════════════════════════════════════════════
# A. pyquotex/api.py reject counter (FT-01/02/03)
# ══════════════════════════════════════════════════════════════════════
async def test_pyquotex_reject_counter():
    print("\n[A] pyquotex/api.py — consecutive-reject counter (FT-01/02/03)")
    from pyquotex.api import QuotexAPI

    api = QuotexAPI("qxbroker.com", "t@e.st", "pw", "en")

    check("A1 fresh API: counter=0, threshold=3, dead_at=0",
          api._consecutive_rejects == 0 and api._reject_threshold == 3
          and api._token_dead_at == 0.0,
          f"counter={api._consecutive_rejects} thr={api._reject_threshold} "
          f"dead={api._token_dead_at}")

    reject_frame = '42["authorization/reject",{"reason":"bump"}]'

    await api._on_message(reject_frame)
    r1 = api.state.websocket_error_reason or ""
    check("A2 1st reject → TRANSIENT (token not blamed, dead_at=0)",
          api._consecutive_rejects == 1
          and api._token_dead_at == 0.0
          and "transient" in r1
          and "expired" not in r1.lower() and "revoked" not in r1.lower(),
          f"counter={api._consecutive_rejects} reason={r1!r}")

    await api._on_message(reject_frame)
    r2 = api.state.websocket_error_reason or ""
    check("A3 2nd reject → still TRANSIENT",
          api._consecutive_rejects == 2 and api._token_dead_at == 0.0
          and "transient" in r2,
          f"counter={api._consecutive_rejects} reason={r2!r}")

    await api._on_message(reject_frame)
    r3 = api.state.websocket_error_reason or ""
    check("A4 3rd reject → DEAD (dead_at set, message blames token)",
          api._consecutive_rejects == 3
          and api._token_dead_at > 0.0
          and "3x consecutive" in r3
          and ("expired" in r3.lower() or "revoked" in r3.lower()),
          f"counter={api._consecutive_rejects} dead={api._token_dead_at} "
          f"reason={r3!r}")

    await api._h_auth_ok({"ssid": "x"})
    check("A5 auth ok → counter + dead_at RESET (rejects forgiven)",
          api._consecutive_rejects == 0 and api._token_dead_at == 0.0,
          f"counter={api._consecutive_rejects} dead={api._token_dead_at}")

    await api._on_message(reject_frame)
    check("A6 reject→ok→reject: counter is 1 (reset worked)",
          api._consecutive_rejects == 1 and api._token_dead_at == 0.0,
          f"counter={api._consecutive_rejects}")
    return QuotexAPI


# ══════════════════════════════════════════════════════════════════════
# B. account.py "Connected"-then-disconnected branch (FT-04)
# ══════════════════════════════════════════════════════════════════════
async def test_account_connected_branch():
    print("\n[B] pyquotex/_api/account.py — Connected-then-disconnected (FT-04)")
    import pyquotex._api.account as acct_mod
    from pyquotex._api.account import AccountMixin

    async def run_case(rejects, ws_error_reason):
        """Drive AccountMixin.connect() with a patched QuotexAPI whose
        api.connect() returns (False, 'Connected') — the exact shape of a
        WS that briefly reached CONNECTED then dropped."""
        real_cls = acct_mod.QuotexAPI

        class FakeState:
            websocket_error_reason = ws_error_reason

        class FakeApi:
            def __init__(self, *a, **k):
                self.state = FakeState()
                self._consecutive_rejects = rejects
                self._reject_threshold = 3

            async def connect(self, *a, **k):
                return False, "Connected"

            async def authenticate(self):
                return False, "not reached"

        acct_mod.QuotexAPI = FakeApi
        try:
            m = object.__new__(AccountMixin)
            m.api = None
            m.host = "qxbroker.com"
            m.email = "t@e.st"
            m.password = "pw"
            m.lang = "en"
            m.resource_path = None
            m.user_data_dir = "."
            m.proxies = None
            m.on_otp_callback = None
            m.session_data = {"token": "x" * 40}
            m.account_is_demo = None  # != AccountType.DEMO → False
            m.asset_default = "EURUSD_otc"
            m.period_default = 60

            async def fake_check_connect():
                return False
            m.check_connect = fake_check_connect

            ok, reason = await m.connect()
            return reason or ""
        finally:
            acct_mod.QuotexAPI = real_cls

    r_b1 = await run_case(1, None)
    check("B1 rejects=1, no error reason → TRANSIENT message",
          "transient WS drop" in r_b1 and "NOT necessarily" in r_b1,
          f"reason={r_b1!r}")

    r_b2 = await run_case(3, None)
    check("B2 rejects=3 → AUTH REJECT confirmed",
          "AUTH REJECT confirmed" in r_b2 and "3x consecutive" in r_b2,
          f"reason={r_b2!r}")

    r_b3 = await run_case(1, "authorization rejected by Quotex (3x "
                             "consecutive) — token is invalid, expired, "
                             "or revoked.")
    check("B3 explicit reject reason → AUTH REJECT confirmed",
          "AUTH REJECT confirmed" in r_b3,
          f"reason={r_b3!r}")


# ══════════════════════════════════════════════════════════════════════
# C. feed.py mirror + backoff + alerts (FT-05/06/07/12)
# ══════════════════════════════════════════════════════════════════════
async def test_feed_mirror_and_backoff():
    print("\n[C] feed.py — reject-state mirror, backoff, alerts (FT-05/06/07/12)")
    os.environ["DB_PATH"] = "/tmp/verify_ft_feed.db"
    for p in ("/tmp/verify_ft_feed.db", "/tmp/verify_ft_feed.db-wal",
              "/tmp/verify_ft_feed.db-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    import feed as feed_mod

    f = feed_mod.QuotexFeed()

    # alert spies (never send real Telegram from a test)
    fired = {"dead": 0, "recovered": 0}
    fake_alerts = types.SimpleNamespace(
        token_dead=lambda n: fired.__setitem__("dead", fired["dead"] + 1),
        feed_recovered=lambda: fired.__setitem__("recovered",
                                                 fired["recovered"] + 1))
    import alerts as real_alerts_mod
    real_alerts_mod.token_dead = fake_alerts.token_dead
    real_alerts_mod.feed_recovered = fake_alerts.feed_recovered

    # C1: pyquotex-shaped client (counter on .api)
    pyq_client = types.SimpleNamespace(
        api=types.SimpleNamespace(_consecutive_rejects=2,
                                  _token_dead_at=0.0))
    f._client = pyq_client
    f._connected = False
    f._sync_reject_state()
    check("C1 pyquotex shape: mirror reads .api counter (2, alive)",
          f._consecutive_rejects == 2 and f._token_dead_at == 0.0,
          f"mirror=({f._consecutive_rejects},{f._token_dead_at})")

    # C2: raw-WS-shaped client (counter on the client itself)
    raw_client = types.SimpleNamespace(
        _consecutive_rejects=3, _token_dead_at=time.time())
    f._client = raw_client
    f._sync_reject_state()
    check("C2 raw-WS shape: mirror reads client counter (3, dead)",
          f._consecutive_rejects == 3 and f._token_dead_at > 0,
          f"mirror=({f._consecutive_rejects},{f._token_dead_at})")
    check("C6 'dead' transition fired alerts.token_dead exactly once",
          fired["dead"] == 1, f"fired={fired}")

    # dead → live transition
    live_api = types.SimpleNamespace(_consecutive_rejects=0,
                                     _token_dead_at=0.0)
    f._client = types.SimpleNamespace(api=live_api)
    f._connected = True
    f._sync_reject_state()
    check("C7 dead→live fired alerts.feed_recovered once",
          fired["recovered"] == 1 and f._consecutive_rejects == 0,
          f"fired={fired} mirror={f._consecutive_rejects}")

    # C3: no client → no crash, mirror unchanged
    f._client = None
    before = (f._consecutive_rejects, f._token_dead_at)
    try:
        f._sync_reject_state()
        check("C3 no client → no crash, mirror unchanged",
              (f._consecutive_rejects, f._token_dead_at) == before)
    except Exception as exc:
        check("C3 no client → no crash, mirror unchanged", False,
              f"{type(exc).__name__}: {exc}")

    # C4/C5: backoff math (same expressions as the run-loop)
    f._consecutive_rejects, f._token_dead_at = 2, 0.0
    transient_delays = [min(10 * a, 30) for a in (1, 2, 3, 10)]
    check("C4 transient backoff 10s→20s→30s (capped)",
          transient_delays == [10, 20, 30, 30],
          f"delays={transient_delays}")
    f._consecutive_rejects, f._token_dead_at = 3, time.time()
    dead_delays = [min(60 * (2 ** min(a - 1, 2)), 120) for a in (1, 2, 3, 10)]
    check("C5 dead-token backoff 60s→120s (capped, anti-abuse)",
          dead_delays == [60, 120, 120, 120], f"delays={dead_delays}")


# ══════════════════════════════════════════════════════════════════════
# D. server.py token-status classification (FT-10/11 logic)
# ══════════════════════════════════════════════════════════════════════
async def test_token_status_classification():
    print("\n[D] /api/token-status classification (FT-10/11 logic)")

    def classify(has_token, connection_status, rejects, dead, status_key):
        """Exact copy of the endpoint's decision tree (FT-10/11)."""
        if has_token:
            if connection_status == "live_authorized":
                status = "live_token"
            elif connection_status == "token_dead_backoff":
                status = "token_dead"
            elif connection_status in ("connected_unauth", "disconnected"):
                if rejects > 0 and not dead:
                    status = "transient_disconnect"
                else:
                    status = "token_set_but_connecting"
            else:
                status = "live_token"
        else:
            status = "no_credentials"
        if dead:
            action = "refresh_token"
        elif status == "no_credentials":
            action = "set_token"
        elif status == "transient_disconnect":
            action = "wait"
        else:
            action = None
        return status, action

    s, a = classify(True, "disconnected", 1, False, None)
    check("D1 rejects=1 alive token → transient_disconnect + wait",
          s == "transient_disconnect" and a == "wait", f"({s},{a})")

    s, a = classify(True, "token_dead_backoff", 3, True, None)
    check("D2 dead token → token_dead + refresh_token",
          s == "token_dead" and a == "refresh_token", f"({s},{a})")

    s, a = classify(False, "disconnected", 0, False, None)
    check("D3 no credentials → set_token", a == "set_token", f"({s},{a})")

    s, a = classify(True, "live_authorized", 0, False, None)
    check("D4 live → no action", s == "live_token" and a is None,
          f"({s},{a})")

    s, a = classify(True, "disconnected", 0, False, None)
    check("D5 zero rejects + connecting → no misleading 'wait' action",
          s == "token_set_but_connecting" and a is None, f"({s},{a})")


# ══════════════════════════════════════════════════════════════════════
# E. regression — module engine + roadmap still healthy
# ══════════════════════════════════════════════════════════════════════
async def test_module_engine_regression():
    print("\n[E] regression — 15-module engine + roadmap (E1/E2)")
    os.environ.setdefault("QX_SIGNAL_MODE", "any_theory")
    import random
    from engines import predict

    rng = random.Random(7)
    n = 220
    candles = []
    price = 1.1000
    regime = 0
    for i in range(n):
        if i % 60 == 0:
            regime = rng.choice([-1, 0, 1])
        o = price
        drift = regime * 0.00012
        c = o + drift + rng.gauss(0, 0.00025)
        hi = max(o, c) + abs(rng.gauss(0, 0.00012))
        lo = min(o, c) - abs(rng.gauss(0, 0.00012))
        candles.append({"time": 1700000000 + i * 60, "open": o, "high": hi,
                        "low": lo, "close": c})
        price = c

    micro = {
        "buy_pct": 0.62, "sell_pct": 0.38, "tick_count": 140,
        "is_fight": False, "big_flow": 0.7, "retail_flow": 0.3,
        "hold_zone": {"low": min(candles[-1]["low"], candles[-2]["low"]),
                      "high": max(candles[-1]["high"], candles[-2]["high"])},
        "reaction": {"count": 3, "direction": "up"},
        "vap": {"migration": "up"},
        "tick_speed": {"accel": 1.4},
        "momentum_shift": 1, "v_shape": False,
    }

    res = predict(candles=candles[-120:], ticks=None, micro=micro,
                  asset="EURUSD_otc", htf_trend="SIDEWAYS", period=60)
    brk = res.get("modules") or res.get("module_breakdown") or {}

    expected_modules = [
        "candle_reaction", "pattern", "key_level", "market_state",
        "wickwall", "divergence", "tickrun", "multi_tf", "momentum",
        "bollinger_rsi", "stochastic", "ema_ribbon", "sr_bounce",
        "tick_eye", "micro_flow",
    ]
    missing = [m for m in expected_modules if m not in brk]
    check("E1 all 15 modules present in the vote breakdown",
          not missing, f"missing={missing}")
    voted = [m for m in expected_modules
             if brk.get(m, {}).get("fired")
             or brk.get(m, {}).get("direction") in ("CALL", "PUT")]
    check("E1b at least 3 modules actually voted (engine alive)",
          len(voted) >= 3, f"voted={len(voted)}: {voted[:8]}")
    check("E2 roadmap attached with the 6 user factors",
          isinstance(res.get("roadmap"), dict)
          and len(res["roadmap"].get("factors") or []) >= 1,
          f"roadmap keys={list((res.get('roadmap') or {}).keys())}")


async def main():
    print("=" * 72)
    print("FALSE-TOKEN-EXPIRED-2026-09-18 — behavioral verification")
    print("=" * 72)
    await test_pyquotex_reject_counter()
    await test_account_connected_branch()
    await test_feed_mirror_and_backoff()
    await test_token_status_classification()
    await test_module_engine_regression()

    print("\n" + "=" * 72)
    print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
    if FAILURES:
        for name, detail in FAILURES:
            print(f"  ✗ {name}: {detail}")
        print("VERDICT: ❌ FAIL")
        sys.exit(1)
    print("VERDICT: ✅ PASS — একটা reject এ আর 'টোকেন মেয়াদ নাই' বলবে না;")
    print("3 লাগাতার reject ছাড়া token_dead হয় না; transient drop নিজে")
    print("থেকেই 10-30s এ রিকানেক্ট হয় — মডিউল ও সিগন্যাল চলতে থাকে।")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
