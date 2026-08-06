"""Lightweight operator alerting (Telegram) for critical feed events."""
import json
import os
import time
import urllib.request
import urllib.error

# One alert per event type per this many seconds minimum (debounce).
_MIN_ALERT_INTERVAL_SEC = 60
_last_sent: dict[str, float] = {}


def _configured() -> bool:
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )


def send(text: str, *, key: str = "default", force: bool = False) -> bool:
    """Send a Telegram message; returns True if actually sent (no-op if unconfigured)."""
    if not _configured():
        return False
    now = time.time()
    if not force and (now - _last_sent.get(key, 0)) < _MIN_ALERT_INTERVAL_SEC:
        return False
    _last_sent[key] = now

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
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
    send(f"🔴 PLYBIT — Quotex token DEAD\n{consecutive_rejects}x পরপর authorization rejected হয়েছে।\nনতুন টোকেন পুশ করুন: /api/set-token?token=NEW_TOKEN\n(browser DevTools থেকে সংগ্রহ করুন)", key="token_dead", force=True)


def feed_recovered() -> None:
    send("🟢 PLYBIT — Quotex সংযোগ ফিরে এসেছে, লাইভ ডেটা চলছে।", key="feed_recovered", force=True)


def all_streams_stale(stale_count: int, total: int) -> None:
    send(f"🟠 PLYBIT — সব স্ট্রিম স্থবির (stale)\n{stale_count}/{total} স্ট্রিমে কোনো নতুন tick আসছে না। টোকেন মৃত না হলেও কানেকশন সমস্যা থাকতে পারে — /api/token-status চেক করুন।", key="all_stale")
