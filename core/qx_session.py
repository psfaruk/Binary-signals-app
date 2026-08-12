"""
core/qx_session.py — automatic Quotex SSID refresh (no manual token paste).

WHY THIS EXISTS
---------------
The Quotex SSID (`QX_TOKEN`) expires roughly every 24 hours. Until now the
only recovery was a human: open DevTools, copy the `authorization` frame,
paste it into the 🔑 Token panel. Until they did, the feed sat in its
reconnect backoff printing "push a fresh token" and the app produced no
signals. That is the single biggest cause of downtime in this project.

This module removes the human from that loop. It holds the operator's
*browser session* — the cookies Quotex itself uses to keep someone logged
in — and replays them over HTTPS to mint a brand-new SSID on demand:

    GET https://<web-host>/en/trade   (with the session cookies)
      → the page embeds `window.settings = {... "token": "<fresh ssid>" ...}`
      → that token is exactly what the WebSocket authorization frame wants.

Verified live on 2026-08-13: the operator's `remember_web_<hash>` cookie
alone re-authenticated the Laravel session and returned a 40-char SSID for
the right account, with no password and no Cloudflare challenge.

WHY COOKIES AND NOT EMAIL/PASSWORD
----------------------------------
Password login is a *form POST* behind Cloudflare, and this project already
learned the hard way (see feed.py `_connect`) that hammering it from a
datacenter IP gets the Quotex account blocked. Cookie replay is a plain
authenticated GET — the same request a browser tab makes — so it is both
far more likely to succeed and far less likely to look like abuse. The
password path is kept as a last resort, disabled by default, and hard
rate-limited (see `_LOGIN_MIN_INTERVAL`).

SELF-SUSTAINING
---------------
Every successful refresh writes the *rotated* cookie jar back to the
persistent volume. Quotex/Cloudflare hand out a new `laravel_session` and
`__cf_bm` on each visit, and Laravel re-issues the recaller cookie
periodically; by storing what comes back, the session keeps rolling
forward instead of decaying toward the moment the operator pasted it.

WHAT STILL NEEDS A HUMAN
------------------------
If the operator logs out in their browser, changes their password, or
Quotex invalidates the recaller, the cookies die. Nothing can be done
automatically at that point — `status()` reports it, an alert fires, and
the UI asks for a fresh cookie blob (a one-line paste, no redeploy).

CONFIG (all optional except the cookies themselves)
---------------------------------------------------
    QX_COOKIES            raw "k=v; k=v" blob copied from DevTools
    QX_CF_CLEARANCE       ) individual cookies, used when QX_COOKIES
    QX_CF_BM              ) is not set — same effect
    QX_REMEMBER_WEB       )
    QX_UA                 User-Agent (must match the browser that made
                          the cookies, or Cloudflare may reject them)
    QX_WEB_HOST           HTTP host for the refresh (default market-qx.info)
                          — deliberately separate from QX_HOST, which
                          drives the *WebSocket* URL and must not change.
    QX_EMAIL/QX_PASSWORD  last-resort form login (off unless
                          QX_ALLOW_PASSWORD_LOGIN=1)
    QX_AUTO_REFRESH=0     disable this module entirely
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Optional

from core import token_store

# ── tunables ──────────────────────────────────────────────────────────────

SESSION_FILE = "qx_session.json"

# Cookie replay is cheap and safe, but there is no point doing it more often
# than the feed's own retry cadence.
_REFRESH_MIN_INTERVAL = float(os.environ.get("QX_REFRESH_MIN_INTERVAL", "60"))
# Password login is the dangerous one — see module docstring.
_LOGIN_MIN_INTERVAL = float(os.environ.get("QX_LOGIN_MIN_INTERVAL", "1800"))
# After this many consecutive cookie failures, stop trying so often: the
# cookies are almost certainly dead and only a human can fix them.
_DEAD_AFTER_FAILURES = int(os.environ.get("QX_COOKIE_DEAD_AFTER", "5"))
_DEAD_BACKOFF = float(os.environ.get("QX_COOKIE_DEAD_BACKOFF", "900"))

_HTTP_TIMEOUT = float(os.environ.get("QX_REFRESH_HTTP_TIMEOUT", "25"))

# Laravel's recaller cookie is `remember_<guard>_<sha1(SessionGuard::class)>`.
# The sha1 is a framework constant, identical on every Laravel deployment.
RECALLER_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"

# Chrome on Windows — the overwhelmingly common case for someone copying
# cookies out of DevTools. Overridable with QX_UA; it must match whatever
# browser produced `cf_clearance`, because Cloudflare binds the clearance
# cookie to the User-Agent that solved the challenge.
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# market-qx.trade 301s to market-qx.info; listing both means the jar is
# seeded for either landing spot and a cross-domain redirect keeps its
# cookies (a manual `Cookie:` header does NOT survive that redirect — this
# was the failure mode during development).
_DEFAULT_HOSTS = "market-qx.info,market-qx.trade,qxbroker.com"

_LOCK = asyncio.Lock()

# Process-local pacing/diagnostic state.
_state: dict = {
    "last_attempt": 0.0,
    "last_success": 0.0,
    "last_login_attempt": 0.0,
    "consecutive_failures": 0,
    "last_error": None,
    "last_source": None,
    "refresh_count": 0,
    "last_token_preview": None,
    "account_email": None,
}


# ── config helpers ────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def enabled() -> bool:
    return _env("QX_AUTO_REFRESH", "1") != "0"


def user_agent() -> str:
    return _env("QX_UA") or DEFAULT_UA


def web_hosts() -> list[str]:
    raw = _env("QX_WEB_HOST") or _DEFAULT_HOSTS
    hosts = [h.strip().lstrip("https://").lstrip("http://").strip("/")
             for h in raw.split(",")]
    return [h for h in hosts if h]


def lang() -> str:
    return _env("QX_LANG", "en") or "en"


def password_login_allowed() -> bool:
    return (_env("QX_ALLOW_PASSWORD_LOGIN", "0") == "1"
            and bool(_env("QX_EMAIL")) and bool(_env("QX_PASSWORD")))


def proxy() -> Optional[str]:
    """Outbound proxy for the refresh, e.g. http://user:pass@host:port.

    MEASURED 2026-08-13, and the reason this option exists: replaying the
    cookies from Railway's own IP (152.55.176.179, Cloudflare colo SJC)
    FAILS. Every Quotex host either serves a Cloudflare JS challenge
    ("Just a moment…", HTTP 403) or returns the logged-out page with
    HTTP 200 and no token — tested with the complete, known-good jar
    (recaller + laravel_session + __vid_l3) that had authenticated from
    the operator's own IP minutes earlier. The session simply is not
    honoured from a foreign datacenter IP, and `cf_clearance` cannot help
    because Cloudflare binds it to the IP that solved the challenge.

    So on Railway the mint needs an exit node in the operator's region.
    Point QX_PROXY at one and everything else in this module works
    unchanged. Without it, use scripts/token_agent.py, which mints from
    the operator's own machine and pushes the token in.
    """
    return _env("QX_PROXY") or None


# ── cookie parsing ────────────────────────────────────────────────────────

def parse_cookie_blob(raw: str) -> dict[str, str]:
    """Parse whatever the operator pasted into {name: value}.

    Accepts the three shapes DevTools / the browser console actually hand
    out: a `document.cookie` string, a JSON object, and the JSON array the
    "Copy all cookies" extensions produce ([{name, value}, …]).
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    s = str(raw).strip()

    if s.startswith("{") or s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                # Either {"cookies": {...}} or a flat {name: value} map.
                inner = data.get("cookies") if isinstance(data.get("cookies"), dict) else data
                for k, v in inner.items():
                    if isinstance(v, str):
                        out[str(k).strip()] = v.strip()
                return out
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("name"):
                        out[str(item["name"]).strip()] = str(item.get("value", "")).strip()
                return out
        except Exception:
            pass  # fall through to the "k=v; k=v" parser

    # `document.cookie` form. Split on ';' and newlines so a blob pasted
    # from the DevTools Application tab (one cookie per line) also works.
    for part in re.split(r"[;\n\r]+", s):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip().strip('"')
        if k:
            out[k] = v
    return out


def _cookies_from_env() -> dict[str, str]:
    """Build a cookie map from env vars. QX_COOKIES wins; singles fill gaps."""
    jar = parse_cookie_blob(_env("QX_COOKIES"))
    singles = {
        "cf_clearance": _env("QX_CF_CLEARANCE"),
        "__cf_bm": _env("QX_CF_BM"),
        RECALLER_NAME: _env("QX_REMEMBER_WEB"),
    }
    for name, val in singles.items():
        if val and name not in jar:
            jar[name] = val
    return {k: v for k, v in jar.items() if v}


def _normalize_recaller(jar: dict[str, str]) -> dict[str, str]:
    """Give a bare `remember_web` value its real Laravel cookie name.

    Operators paste the cookie they see, and some tools truncate the sha1
    suffix. Laravel matches the recaller by exact name, so a bare
    `remember_web` is silently ignored by the server — it returns the
    signed-out page with HTTP 200, which looks like success until you
    notice there is no token in it.
    """
    if RECALLER_NAME in jar:
        return jar
    for key in list(jar.keys()):
        if key.startswith("remember_web"):
            jar[RECALLER_NAME] = jar.pop(key)
            break
    return jar


# ── persistence ───────────────────────────────────────────────────────────

def _store_path():
    return token_store.state_dir() / SESSION_FILE


def _read_store() -> dict:
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[qx_session] store read error: {exc}")
        return {}


def _write_store(data: dict) -> None:
    path = _store_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as exc:
        print(f"[qx_session] store write error: {exc}")


def _env_fingerprint(jar: dict[str, str]) -> str:
    blob = json.dumps(jar, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_cookies() -> dict[str, str]:
    """The cookie jar to replay: stored (rotated) unless the env is newer.

    Same precedence problem as `token_store.resolve_boot_token` — two
    legitimate sources, neither self-dating. Resolved the same way: the
    store remembers which env blob it was seeded from, so an env var the
    operator has *changed* takes over, while an unchanged one never
    clobbers the fresher cookies the store has been collecting.
    """
    env_jar = _normalize_recaller(_cookies_from_env())
    store = _read_store()
    stored_jar = store.get("cookies") if isinstance(store.get("cookies"), dict) else {}

    if not env_jar:
        return _normalize_recaller(dict(stored_jar))

    if not stored_jar:
        return env_jar

    if store.get("env_fingerprint") != _env_fingerprint(env_jar):
        print("[qx_session] QX_COOKIES/env cookies changed — reseeding the "
              "stored session from the environment")
        save_cookies(env_jar, source="env-new")
        return env_jar

    # Stored jar is the env jar plus everything the server rotated since.
    merged = dict(env_jar)
    merged.update(stored_jar)
    return _normalize_recaller(merged)


def save_cookies(jar: dict[str, str], source: str = "refresh") -> dict:
    """Persist the jar (and the env fingerprint it came from)."""
    jar = _normalize_recaller({k: v for k, v in jar.items() if v})
    rec = _read_store()
    rec.update({
        "cookies": jar,
        "updated_at": time.time(),
        "source": source,
        "env_fingerprint": _env_fingerprint(_normalize_recaller(_cookies_from_env())),
    })
    _write_store(rec)
    return rec


def import_cookies(raw: str, source: str = "ui") -> dict:
    """Operator pasted a fresh cookie blob. Validate the essentials, store it."""
    jar = _normalize_recaller(parse_cookie_blob(raw))
    if not jar:
        return {"ok": False, "error": "could not parse any cookies out of that "
                                      "— paste the document.cookie string, or "
                                      "the JSON export of the cookies"}
    if RECALLER_NAME not in jar:
        return {"ok": False,
                "error": f"no `remember_web…` cookie found. That is the one "
                         f"that keeps you logged in — without it a fresh token "
                         f"cannot be minted. Cookies seen: "
                         f"{', '.join(sorted(jar)) or 'none'}"}
    save_cookies(jar, source=source)
    _state["consecutive_failures"] = 0
    _state["last_error"] = None
    return {"ok": True, "cookies": sorted(jar.keys()),
            "has_cf_clearance": "cf_clearance" in jar}


def configured() -> bool:
    jar = load_cookies()
    return RECALLER_NAME in jar or password_login_allowed()


# ── token extraction ──────────────────────────────────────────────────────

def _extract_json_object(text: str, start: int) -> Optional[str]:
    """Slice the balanced {...} beginning at `start`, ignoring braces in strings.

    A non-greedy regex is not enough here: `window.settings` legitimately
    contains strings with braces (error templates, url patterns), so a
    lazy match stops at the first `}` inside one of them and the JSON
    fails to parse.
    """
    depth = 0
    in_str = False
    escaped = False
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_settings(html: str) -> Optional[dict]:
    """Pull `window.settings = {...}` out of the trade page."""
    for m in re.finditer(r"window\.settings\s*=\s*\{", html):
        blob = _extract_json_object(html, m.end() - 1)
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("token"):
            return data
    return None


# ── the refresh itself ────────────────────────────────────────────────────

def _ssl_context():
    """Firefox cipher suite — the same Cloudflare bypass pyquotex relies on."""
    try:
        from pyquotex.network.ssl_utils import (create_ssl_context,
                                                CIPHER_SUITE_FIREFOX)
        return create_ssl_context(cipher_suite=CIPHER_SUITE_FIREFOX)
    except Exception as exc:
        print(f"[qx_session] ssl_utils unavailable ({exc}) — default TLS")
        return True


def _seed_jar(httpx_mod, jar: dict[str, str]):
    """Seed cookies for every candidate host.

    market-qx.trade redirects to market-qx.info, which is a *different*
    registrable domain — cookies scoped to one are not sent to the other.
    Seeding all candidates means the jar survives whichever way the
    redirect goes.
    """
    cookies = httpx_mod.Cookies()
    for host in web_hosts():
        for domain in (host, f".{host}"):
            for name, value in jar.items():
                try:
                    cookies.set(name, value, domain=domain)
                except Exception:
                    pass
    return cookies


def _page_headers() -> dict:
    return {
        "User-Agent": user_agent(),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }


def _harvest(client) -> dict[str, str]:
    """Read the (rotated) jar back out of the client."""
    out: dict[str, str] = {}
    try:
        for c in client.cookies.jar:
            if c.value:
                out[c.name] = c.value
    except Exception:
        pass
    return out


async def _try_trade_page(client, host: str) -> tuple[Optional[str], str]:
    """GET the trade page and read the SSID out of `window.settings`."""
    url = f"https://{host}/{lang()}/trade"
    try:
        resp = await client.get(url, headers=_page_headers())
    except Exception as exc:
        return None, f"{host}: {type(exc).__name__}: {exc}"

    if resp.status_code == 403:
        return None, (f"{host}: HTTP 403 — Cloudflare blocked the request. "
                      f"The cf_clearance cookie is bound to the IP and "
                      f"User-Agent that solved the challenge, so it does not "
                      f"transfer to this server. Re-import cookies, or set "
                      f"QX_UA to the browser that produced them.")
    if resp.status_code >= 400:
        return None, f"{host}: HTTP {resp.status_code}"

    settings = extract_settings(resp.text)
    if not settings:
        if "sign-in" in resp.text or 'name="_token"' in resp.text:
            return None, (f"{host}: served the signed-out page — the "
                          f"remember_web cookie is expired or was revoked. "
                          f"Import a fresh cookie blob from your browser.")
        return None, f"{host}: no window.settings token in the response"

    token = str(settings.get("token") or "").strip()
    if len(token) < 20:
        return None, f"{host}: token too short ({len(token)} chars)"

    # The cookies are the authority on which account this is — QX_EMAIL is
    # just a config string a human typed, and a typo in it must never be
    # able to take the feed offline. So this mismatch WARNS (loudly, once
    # per changed pair) instead of refusing. Set QX_STRICT_EMAIL=1 to make
    # it fatal, e.g. if several accounts' cookies are floating around.
    _state["account_email"] = settings.get("email")
    want = _env("QX_EMAIL")
    got = str(settings.get("email") or "")
    if want and got and want.lower() != got.lower():
        msg = (f"cookies belong to {got}, but QX_EMAIL says {want}")
        if _env("QX_STRICT_EMAIL", "0") == "1":
            return None, f"{host}: {msg} — refusing (QX_STRICT_EMAIL=1)"
        if _state.get("_email_warned") != (want, got):
            _state["_email_warned"] = (want, got)
            print(f"[qx_session] ⚠️  {msg}. Using the session's own account "
                  f"({got}) — fix QX_EMAIL if that is not the one you want.")
    return token, f"{host}: ok"


async def _password_login(client, host: str) -> tuple[bool, str]:
    """Last-resort form login. Only reached when explicitly enabled."""
    from bs4 import BeautifulSoup

    base = f"https://{host}/{lang()}"
    try:
        resp = await client.get(f"{base}/sign-in/modal/", headers=_page_headers())
        soup = BeautifulSoup(resp.content, "html.parser")
        field = soup.find("input", {"name": "_token"})
        csrf = field.get("value") if field else None
        if not csrf:
            return False, f"{host}: no CSRF token on the sign-in page"

        resp = await client.post(
            f"{base}/sign-in/",
            headers={**_page_headers(),
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"{base}/sign-in/modal",
                     "Origin": f"https://{host}"},
            data={"_token": csrf, "email": _env("QX_EMAIL"),
                  "password": _env("QX_PASSWORD"), "remember": 1},
        )
    except Exception as exc:
        return False, f"{host}: login error {type(exc).__name__}: {exc}"

    body = resp.text
    if 'name="keep_code"' in body:
        return False, (f"{host}: Quotex is asking for the e-mail PIN code. "
                       f"That cannot be automated — log in once in a browser, "
                       f"then import the cookies.")
    if "trade" in str(resp.url):
        return True, f"{host}: login ok"
    return False, f"{host}: login rejected (landed on {resp.url})"


async def refresh_token(reason: str = "", force: bool = False
                        ) -> tuple[Optional[str], str]:
    """Mint a fresh SSID from the stored browser session.

    Returns (token, detail). On success the token is already persisted to
    the token store and session.json, and `QX_TOKEN` is updated — callers
    only need to trigger a reconnect.
    """
    if not enabled():
        return None, "auto-refresh disabled (QX_AUTO_REFRESH=0)"

    now = time.time()
    if not force:
        since = now - _state["last_attempt"]
        if since < _REFRESH_MIN_INTERVAL:
            return None, (f"rate-limited — last attempt {since:.0f}s ago "
                          f"(min {_REFRESH_MIN_INTERVAL:.0f}s)")
        if (_state["consecutive_failures"] >= _DEAD_AFTER_FAILURES
                and since < _DEAD_BACKOFF):
            return None, (f"cookies look dead after "
                          f"{_state['consecutive_failures']} failures — "
                          f"backing off {_DEAD_BACKOFF:.0f}s. Import a fresh "
                          f"cookie blob to clear this.")

    async with _LOCK:
        _state["last_attempt"] = time.time()
        token, detail = await _do_refresh(reason)

    if token:
        _state["consecutive_failures"] = 0
        _state["last_success"] = time.time()
        _state["last_error"] = None
        _state["refresh_count"] += 1
        _state["last_token_preview"] = token_store.mask(token)
        _persist_token(token, reason)
    else:
        _state["consecutive_failures"] += 1
        _state["last_error"] = detail
        print(f"[qx_session] ❌ refresh failed ({reason}): {detail}")
        if _state["consecutive_failures"] == _DEAD_AFTER_FAILURES:
            _alert_dead(detail)
    return token, detail


async def _do_refresh(reason: str) -> tuple[Optional[str], str]:
    try:
        import httpx
    except Exception as exc:
        return None, f"httpx unavailable: {exc}"

    jar = load_cookies()
    have_recaller = RECALLER_NAME in jar
    if not have_recaller and not password_login_allowed():
        return None, ("no session cookies stored — open the 🔑 Token panel "
                      "and import the cookies from your browser")

    print(f"[qx_session] refreshing SSID (reason={reason or 'manual'}, "
          f"cookies={len(jar)}, recaller={'yes' if have_recaller else 'no'})")

    ctx = _ssl_context()
    cookies = _seed_jar(httpx, jar)
    errors: list[str] = []
    client_kwargs = {}
    prox = proxy()
    if prox:
        client_kwargs["proxy"] = prox
        print(f"[qx_session] routing the refresh through QX_PROXY "
              f"({prox.split('@')[-1]})")

    async with httpx.AsyncClient(verify=ctx, timeout=_HTTP_TIMEOUT,
                                 follow_redirects=True,
                                 cookies=cookies, **client_kwargs) as client:
        # ── strategy 1: replay the browser session (preferred) ──────────
        if have_recaller:
            for host in web_hosts():
                token, detail = await _try_trade_page(client, host)
                if token:
                    save_cookies({**jar, **_harvest(client)}, source="refresh")
                    _state["last_source"] = "cookies"
                    print(f"[qx_session] ✅ fresh SSID via cookies "
                          f"({token_store.mask(token)}) from {host}")
                    return token, f"cookies@{host}"
                errors.append(detail)

        # ── strategy 2: form login (opt-in, hard rate-limited) ──────────
        if password_login_allowed():
            since = time.time() - _state["last_login_attempt"]
            if since < _LOGIN_MIN_INTERVAL:
                errors.append(f"password login rate-limited "
                              f"({since:.0f}s < {_LOGIN_MIN_INTERVAL:.0f}s)")
            else:
                _state["last_login_attempt"] = time.time()
                for host in web_hosts():
                    ok, detail = await _password_login(client, host)
                    errors.append(detail)
                    if not ok:
                        continue
                    token, detail2 = await _try_trade_page(client, host)
                    errors.append(detail2)
                    if token:
                        save_cookies({**jar, **_harvest(client)},
                                     source="password-login")
                        _state["last_source"] = "password"
                        print(f"[qx_session] ✅ fresh SSID via password login "
                              f"({token_store.mask(token)}) from {host}")
                        return token, f"password@{host}"

    return None, " | ".join(errors) or "no strategy produced a token"


def _persist_token(token: str, reason: str) -> None:
    """Make the new token the one everything else uses, durably."""
    os.environ["QX_TOKEN"] = token
    try:
        token_store.save_token(token, source=f"auto-refresh:{reason or 'feed'}")
    except Exception as exc:
        print(f"[qx_session] token_store save failed: {exc}")
    try:
        from quotex_ws import QuotexWSClient
        QuotexWSClient.save_token_only(token)
    except Exception as exc:
        print(f"[qx_session] session.json save failed (non-fatal): {exc}")


def _alert_dead(detail: str) -> None:
    try:
        import alerts
        alerts.send(
            "🔴 PLYBIT — Quotex সেশন কুকি মেয়াদোত্তীর্ণ\n"
            "অটো-রিফ্রেশ আর নতুন টোকেন আনতে পারছে না, তাই ম্যানুয়ালি লাগবে:\n"
            "ব্রাউজারে Quotex এ লগইন করুন → DevTools → Console → "
            "document.cookie কপি করুন → অ্যাপের 🔑 Token প্যানেলে "
            "'Session cookies' বক্সে পেস্ট করুন।\n"
            f"শেষ ত্রুটি: {detail[:300]}",
            key="qx_cookies_dead", force=True)
    except Exception as exc:
        print(f"[qx_session] alert failed: {exc}")


# ── introspection for /api/session/status ─────────────────────────────────

def status() -> dict:
    """Non-secret view of the auto-refresh subsystem."""
    jar = load_cookies()
    store = _read_store()
    return {
        "enabled": enabled(),
        "configured": configured(),
        "has_recaller": RECALLER_NAME in jar,
        "has_cf_clearance": "cf_clearance" in jar,
        "cookie_names": sorted(jar.keys()),
        "cookies_updated_at": store.get("updated_at"),
        "cookies_source": store.get("source"),
        "persistent": token_store.is_persistent(),
        "web_hosts": web_hosts(),
        "user_agent": user_agent(),
        "password_login_allowed": password_login_allowed(),
        "account_email": _state["account_email"],
        "last_success": _state["last_success"] or None,
        "last_attempt": _state["last_attempt"] or None,
        "last_error": _state["last_error"],
        "last_source": _state["last_source"],
        "refresh_count": _state["refresh_count"],
        "consecutive_failures": _state["consecutive_failures"],
        "last_token_preview": _state["last_token_preview"],
    }
