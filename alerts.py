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


# ═══════════════════════════════════════════════════════════════════════════
# AGENT PERFORMANCE ALERTS (DEEP-FIX-2026-08-07)
# ═══════════════════════════════════════════════════════════════════════════

def agent_edge_gained(asset: str, auc: float, samples: int) -> None:
    """Agent earned authority on a pair — this is worth celebrating."""
    send(f"🟢 AGENT AUTHORITY — {asset}\n"
         f"AUC: {auc:.3f} over {samples} graded decisions — agent may now "
         f"override signals on this pair.\n"
         f"Check /api/agent/status for details.",
         key=f"agent_edge_{asset}")


def agent_auc_drop(asset: str, auc: float, prev_auc: float, samples: int) -> None:
    """Agent's measured edge dropped significantly."""
    send(f"🔴 AGENT AUC DROP — {asset}\n"
         f"AUC fell from {prev_auc:.3f} → {auc:.3f} (n={samples}).\n"
         f"If below 0.52, authority will be revoked.\n"
         f"Check /api/agent/models for per-asset details.",
         key=f"agent_auc_drop_{asset}")


def pair_wr_collapse(asset: str, win_rate: float, breakeven: float, samples: int) -> None:
    """A pair's win rate collapsed below breakeven — auto-disabled by gate."""
    send(f"🔴 PAIR DISABLED — {asset}\n"
         f"7-day WR: {win_rate:.1f}% (breakeven: {breakeven:.1f}%, n={samples}).\n"
         f"Breakeven gate auto-disabled this pair. Check /api/breakeven/report.",
         key=f"wr_collapse_{asset}")


def signal_quality_shift(tier: str, old_wr: float, new_wr: float, samples: int) -> None:
    """Signal quality tier accuracy shifted — may indicate regime change."""
    direction = "⬆️ improved" if new_wr > old_wr else "⬇️ declined"
    send(f"🟡 QUALITY SHIFT — {tier} signals\n"
         f"{direction}: {old_wr:.1f}% → {new_wr:.1f}% (n={samples}).\n"
         f"Check /api/quality-analysis for breakdown.",
         key=f"quality_shift_{tier}")


def breakeven_gate_triggered(asset: str, win_rate: float, breakeven: float) -> None:
    """Breakeven gate blocked a signal for a losing pair."""
    send(f"⛔ BREAKEVEN GATE — {asset}\n"
         f"Signal blocked: {win_rate:.1f}% WR < {breakeven:.1f}% breakeven.\n"
         f"Pair auto-disabled until WR recovers. Check /api/breakeven/report.",
         key=f"begate_{asset}")
