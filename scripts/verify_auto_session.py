#!/usr/bin/env python3
"""
Verify the automatic Quotex session refresh end to end.

Run:  py scripts/verify_auto_session.py
      py scripts/verify_auto_session.py --no-ws     (skip the live WS check)

What it proves, in order:

  1. Cookie parsing accepts every shape an operator might paste.
  2. `window.settings` extraction survives braces inside strings.
  3. The configured cookies actually mint a fresh SSID from Quotex.
  4. The minted token is persisted where the app reads it back
     (token_store on the volume + session.json for pyquotex).
  5. The minted token really authorises a Quotex WebSocket — the only
     test that proves the token is usable, not merely well-formed.

Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

env_path = ROOT / ".env"
if env_path.exists():
    load_dotenv(env_path)

from core import qx_session, token_store  # noqa: E402

PASS, FAIL = "\033[92m✔\033[0m", "\033[91m✘\033[0m"
_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label))
    print(f"  {PASS if ok else FAIL} {label}" + (f"\n      {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ── 1. cookie parsing ─────────────────────────────────────────────────────

def test_cookie_parsing() -> None:
    section("1. Cookie parsing")

    doc = "cf_clearance=abc123; __cf_bm=def456; remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d=xyz"
    jar = qx_session.parse_cookie_blob(doc)
    check(jar.get("cf_clearance") == "abc123" and len(jar) == 3,
          "document.cookie string", f"parsed {sorted(jar)}")

    jar = qx_session.parse_cookie_blob(
        '[{"name":"cf_clearance","value":"aaa"},{"name":"remember_web","value":"bbb"}]')
    check(jar.get("cf_clearance") == "aaa", "JSON array export (DevTools extensions)")

    jar = qx_session.parse_cookie_blob('{"cookies":{"remember_web":"zzz"}}')
    check(jar.get("remember_web") == "zzz", "JSON object with a `cookies` key")

    jar = qx_session.parse_cookie_blob("cf_clearance=a\nremember_web=b\n__cf_bm=c")
    check(len(jar) == 3, "newline-separated blob (Application tab paste)")

    # A bare `remember_web` is silently ignored by Laravel — it must be
    # renamed to the real recaller, or the refresh returns the logged-out
    # page with HTTP 200 and no token.
    fixed = qx_session._normalize_recaller({"remember_web": "v"})
    check(fixed.get(qx_session.RECALLER_NAME) == "v",
          "bare `remember_web` is renamed to the real Laravel recaller")


# ── 2. settings extraction ────────────────────────────────────────────────

def test_extraction() -> None:
    section("2. window.settings extraction")

    html = ('<script>window.settings = {"token":"T' + "x" * 39 +
            '","isDemo":0,"defaultError":"oops {code} }","email":"a@b.c"};</script>')
    got = qx_session.extract_settings(html)
    check(bool(got) and got.get("token", "").startswith("T"),
          "brace inside a string does not truncate the JSON",
          "a lazy regex stops at the `}` inside defaultError and fails to parse")

    check(qx_session.extract_settings("<html>nothing here</html>") is None,
          "returns None when the page has no settings blob")

    html_multi = ('<script>window.settings = {"nope":1};</script>'
                  '<script>window.settings = {"token":"' + "y" * 40 + '"};</script>')
    got = qx_session.extract_settings(html_multi)
    check(bool(got) and got.get("token") == "y" * 40,
          "skips a settings blob that carries no token")


# ── 3-4. live refresh + persistence ───────────────────────────────────────

async def test_live_refresh() -> str | None:
    section("3. Live SSID refresh from the stored session")

    print(f"      state dir      : {token_store.state_dir()}")
    print(f"      persistent     : {token_store.is_persistent()}")
    st = qx_session.status()
    print(f"      cookies stored : {st['cookie_names']}")
    print(f"      web hosts      : {st['web_hosts']}")

    if not check(st["enabled"], "auto-refresh enabled (QX_AUTO_REFRESH)"):
        return None
    if not check(st["configured"], "session cookies configured",
                 "set QX_REMEMBER_WEB or QX_COOKIES in .env"):
        return None
    check(st["has_recaller"], "remember_web recaller cookie present")

    before = (token_store.load_token() or {}).get("token")
    t0 = time.time()
    token, detail = await qx_session.refresh_token(reason="verify", force=True)
    elapsed = time.time() - t0

    if not check(bool(token), f"minted a fresh SSID ({elapsed:.1f}s)", detail):
        print("\n      Quotex said: " + str(detail))
        return None

    print(f"      token   : {token_store.mask(token)}  (len={len(token)})")
    print(f"      via     : {detail}")
    print(f"      account : {qx_session.status().get('account_email')}")

    check(len(token) >= 20 and not any(c.isspace() for c in token),
          "token passes token_store validation",
          token_store.validate_token(token) or "no complaints")

    # Quotex mints ONE SSID per logged-in session and serves that same value
    # on every page load — it does not rotate per request. So "the token
    # changed" is the wrong thing to assert: a repeat run legitimately
    # returns the identical string, and it is only a NEW token after Quotex
    # has actually expired the session. What matters is that the refresher
    # is deterministic for a given session, which is what makes it safe to
    # call on every reconnect attempt.
    token2, _ = await qx_session.refresh_token(reason="verify-repeat", force=True)
    check(token2 == token,
          "refresh is idempotent within a session (same SSID, no churn)",
          f"repeat call returned {token_store.mask(token2 or '')}")
    if before:
        print(f"      (previously stored: {token_store.mask(before)}"
              f"{' — unchanged, same session' if before == token else ' — replaced'})")

    section("4. Persistence — the app must find it after a restart")
    stored = token_store.load_token() or {}
    check(stored.get("token") == token,
          f"token_store has it ({stored.get('source')})")
    check(bool(token_store.is_persistent()) or not os.environ.get("RAILWAY_PUBLIC_DOMAIN"),
          "stored on something that survives a redeploy")
    check(os.environ.get("QX_TOKEN") == token, "QX_TOKEN updated in-process")

    try:
        with open(ROOT / "session.json", encoding="utf-8") as f:
            sess = json.load(f)
        found = any(isinstance(v, dict) and v.get("token") == token
                    for v in sess.values())
        check(found, "session.json carries the token (pyquotex fast path)")
    except Exception as exc:
        check(False, "session.json readable", str(exc))

    cookie_store = qx_session._read_store()
    check(bool(cookie_store.get("cookies")), "rotated cookie jar written back",
          f"{len(cookie_store.get('cookies') or {})} cookies, "
          f"source={cookie_store.get('source')}")
    check("laravel_session" in (cookie_store.get("cookies") or {}),
          "picked up the server-issued laravel_session (session is rolling)")

    return token


# ── 5. the token actually authorises a WebSocket ──────────────────────────

async def test_websocket(token: str) -> None:
    section("5. Live WebSocket authorization with the minted token")
    os.environ["QX_TOKEN"] = token
    try:
        import feed as feed_mod
    except Exception as exc:
        check(False, "import feed", str(exc))
        return

    f = feed_mod.QuotexFeed()
    try:
        ok = await asyncio.wait_for(f._connect(), timeout=75)
    except asyncio.TimeoutError:
        check(False, "Quotex authorised the minted token", "connect timed out (75s)")
        return
    except Exception as exc:
        check(False, "Quotex authorised the minted token",
              f"{type(exc).__name__}: {exc}")
        return

    check(ok, "Quotex authorised the minted token",
          getattr(f, "_last_error", None) or "")
    if ok:
        try:
            pairs = await asyncio.wait_for(
                f._client.get_instruments(), timeout=25)
            check(bool(pairs), "instrument list fetched over the live socket",
                  f"{len(pairs)} instruments")
        except Exception as exc:
            check(False, "instrument list fetched",
                  f"{type(exc).__name__}: {exc}")
    try:
        await f._close_client(f._client)
    except Exception:
        pass


# ── 6. the reconnect path calls the refresher ─────────────────────────────

async def test_relogin_wiring() -> None:
    section("6. Feed wiring — a dead token triggers the refresher")
    import feed as feed_mod

    f = feed_mod.QuotexFeed()
    calls: list[str] = []
    real = qx_session.refresh_token

    async def spy(reason="", force=False):
        calls.append(reason)
        return "S" * 40, "spy"

    qx_session.refresh_token = spy  # type: ignore[assignment]
    saved_token = os.environ.get("QX_TOKEN", "")
    try:
        got = await f._auto_relogin()
        check(got is True, "_auto_relogin() reports success so run() retries now")
        check(calls == ["feed-relogin"],
              "_auto_relogin() actually called qx_session.refresh_token",
              f"calls={calls}")
    finally:
        qx_session.refresh_token = real  # type: ignore[assignment]
        if saved_token:
            os.environ["QX_TOKEN"] = saved_token

    check(hasattr(f, "_session_keeper"), "feed has the proactive session keeper")


# ── main ──────────────────────────────────────────────────────────────────

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ws", action="store_true",
                    help="skip the live WebSocket check")
    args = ap.parse_args()

    print("\033[1m" + "═" * 66)
    print(" Quotex auto-session verification")
    print("═" * 66 + "\033[0m")

    test_cookie_parsing()
    test_extraction()
    token = await test_live_refresh()
    await test_relogin_wiring()
    if token and not args.no_ws:
        await test_websocket(token)
    elif not token:
        section("5. Live WebSocket authorization")
        print("  — skipped: no token was minted")

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "═" * 66)
    failed = [name for ok, name in _results if not ok]
    if failed:
        print(f"\033[91m{total - passed}/{total} CHECKS FAILED\033[0m")
        for name in failed:
            print(f"   ✘ {name}")
        return 1
    print(f"\033[92mALL {total} CHECKS PASSED\033[0m — the app can mint and use "
          f"its own Quotex tokens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
