"""
Quotex HTTP login via curl_cffi (browser TLS impersonation).

pyquotex's own login uses httpx, whose TLS fingerprint Cloudflare rejects
with 403 on the Quotex mirrors. curl_cffi impersonates a real Chrome TLS
handshake and passes the challenge, so we replicate the login flow here:

  1. GET  /{lang}/sign-in/           -> CSRF `_token` + cookies
  2. POST /{lang}/sign-in/           -> authenticated session (redirects to /trade)
  3. parse `window.settings = {...}` on the trade page -> ssid token

The resulting (ssid, cookies) pair is fed to pyquotex via set_session(),
which skips its blocked HTTP login entirely and goes straight to the
websocket.
"""
import json
import re

from curl_cffi import requests as cf_requests

IMPERSONATE = "chrome"

_TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
_SETTINGS_RE = re.compile(r"window\.settings\s*=\s*(\{.*?\})\s*;", re.DOTALL)


def _cookie_string(session) -> str:
    return "; ".join(f"{k}={v}" for k, v in session.cookies.items())


def fetch_session(email: str, password: str,
                  host: str = "market-qx.trade",
                  user_agent: str | None = None,
                  lang: str = "en") -> dict:
    """Log in with email/password and return {"ssid", "cookies"} on success,
    or {"error": reason} on failure. Synchronous — run in a thread from
    async code."""
    if not email or not password:
        return {"error": "QX_EMAIL / QX_PASSWORD not set"}

    base = f"https://{host}/{lang}"
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent

    s = cf_requests.Session(impersonate=IMPERSONATE, headers=headers)

    r = s.get(f"{base}/sign-in/", timeout=30)
    if r.status_code != 200:
        return {"error": f"sign-in page HTTP {r.status_code}"}
    m = _TOKEN_RE.search(r.text)
    if not m:
        return {"error": "CSRF _token not found on sign-in page"}

    r = s.post(f"{base}/sign-in/", timeout=30, allow_redirects=True, data={
        "_token": m.group(1),
        "email": email,
        "password": password,
        "remember": 1,
    })

    if 'name="keep_code"' in r.text:
        return {"error": "Quotex emailed you a PIN code (new device check). "
                         "Automated login can't read your inbox — log in once "
                         "from a browser on this machine, or put the session "
                         "token in .env as QX_TOKEN."}

    if "trade" not in str(r.url):
        # Try the trade page directly — some responses land back on the form
        r = s.get(f"{base}/trade", timeout=30)
        if "trade" not in str(r.url) or r.status_code != 200:
            return {"error": f"login rejected (landed on {r.url}, "
                             f"HTTP {r.status_code}) — check QX_EMAIL/QX_PASSWORD"}

    m = _SETTINGS_RE.search(r.text)
    if not m:
        return {"error": "window.settings not found on trade page"}
    try:
        ssid = json.loads(m.group(1)).get("token")
    except json.JSONDecodeError as e:
        return {"error": f"could not parse window.settings: {e}"}
    if not ssid:
        return {"error": "no token in window.settings"}

    return {"ssid": ssid, "cookies": _cookie_string(s)}
