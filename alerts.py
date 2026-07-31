"""
Lightweight operator alerting (Telegram) for critical feed events.

FIX (LIVE-TICK-RELIABILITY-2026-07-31): the app had ZERO proactive
notification when the Quotex live feed died — the QX_TOKEN expires
roughly every 24h (see .env.example) and the only way to notice was to
manually poll /api/token-status or watch the chart go stale. This module
sends a Telegram message the moment the feed transitions
alive -> dead and dead -> alive, so the operator can push a fresh token
via /api/set-token within minutes instead of finding out hours later.

Deliberately dependency-free (stdlib urllib only) so it doesn't add a
new pip package to requirements.txt. Fully optional: if
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set, every call is a
no-op — safe to import unconditionally from feed.py.

Setup (optional):
  1. Talk to @BotFather on Telegram -> /newbot -> copy the bot token.
  2. Message your new bot once (anything), then open:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     and read "chat":{"id": ...} from the JSON to get your chat id.
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway Variables.
"""
import json
import os
import time
import urllib.request
import urllib.error

# Don't hammer Telegram (or spam the operator's phone) if something is
# flapping — one alert per event type per this many seconds minimum.
_MIN_ALERT_INTERVAL_SEC = 60
_last_sent: dict[str, float] = {}


def _configured() -> bool:
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )


def send(text: str, *, key: str = "default", force: bool = False) -> bool:
    """Send a Telegram message. Returns True if actually sent.

    `key` debounces: repeated alerts with the same key inside
    _MIN_ALERT_INTERVAL_SEC are dropped (except `force=True`, used for
    the one-shot state-transition messages which matter individually).
    No-op (returns False) if Telegram isn't configured — callers don't
    need to check `_configured()` themselves.
    """
    if not _configured():
        return False
    now = time.time()
    if not force and (now - _last_sent.get(key, 0)) < _MIN_ALERT_INTERVAL_SEC:
        return False
    _last_sent[key] = now

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        return True
    except urllib.error.URLError as exc:
        print(f"[alerts] Telegram send failed: {exc}")
        return False
    except Exception as exc:
        print(f"[alerts] Telegram send error: {type(exc).__name__}: {exc}")
        return False


def token_dead(consecutive_rejects: int) -> None:
    send(
        "🔴 PLYBIT — Quotex token DEAD\n"
        f"{consecutive_rejects}x পরপর authorization rejected হয়েছে।\n"
        "নতুন টোকেন পুশ করুন: /api/set-token?token=NEW_TOKEN\n"
        "(browser DevTools থেকে সংগ্রহ করুন)",
        key="token_dead", force=True,
    )


def feed_recovered() -> None:
    send(
        "🟢 PLYBIT — Quotex সংযোগ ফিরে এসেছে, লাইভ ডেটা চলছে।",
        key="feed_recovered", force=True,
    )


def all_streams_stale(stale_count: int, total: int) -> None:
    send(
        "🟠 PLYBIT — সব স্ট্রিম স্থবির (stale)\n"
        f"{stale_count}/{total} স্ট্রিমে কোনো নতুন tick আসছে না। "
        "টোকেন মৃত না হলেও কানেকশন সমস্যা থাকতে পারে — /api/token-status চেক করুন।",
        key="all_stale",
    )
