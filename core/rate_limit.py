"""
core/rate_limit.py — REAL in-process HTTP rate limiting (PHASE-J, 2026-10).

HISTORY: `api_keys.rate_limit_per_min` was ADVERTISED since 2026-08-13
(created keys carried a per-minute cap) but NEVER enforced — Phase D
documented this honestly as residual risk / blocker B4 ("no 429 logic
exists"). The owner has now authorized fixing it (2026-10).

Design (deliberately dependency-free — no slowapi, no Redis):
  • Fixed-window counters keyed by (bucket, client-ip).
  • Thread-safe via a single lock; periodic pruning of dead windows.
  • Buckets (limits configurable via env, all per-minute):
      auth        — /api/auth/* claim/verify/change-pin   (default 10)
      token_push  — operator-gated token endpoints        (default 30)
      global      — every other /api/* route per IP       (default 600)
    api_key_write requests are limited per API KEY using the key's own
    `rate_limit_per_min` from the api_keys table (finally enforced).
  • Kill switch: QX_RATE_LIMIT=0 disables everything (tests / private
    LAN deployments). Default ON.
  • On block → 429 + Retry-After + X-RateLimit-* headers; on allow →
    X-RateLimit-Limit / X-RateLimit-Remaining headers (truthful — the
    numbers reflect what this limiter actually enforces).

Honest limits: these protect a small self-hosted deployment against
runaway clients and brute force. They are NOT a DDoS shield (documented
in docs/phase_j_m/PHASE_J_SECURITY_HARDENING.md).
"""
from __future__ import annotations

import os
import threading
import time

_LOCK = threading.Lock()
_COUNTERS: dict[str, list] = {}          # key -> [window_start, count]
_LAST_PRUNE = 0.0
_PRUNE_INTERVAL = 300.0                  # seconds between dead-window sweeps

# Env-configurable per-minute limits (parsed once per call so tests can
# tweak them via os.environ without re-importing).
_DEFAULTS = {
    "auth": 10,
    "token_push": 30,
    "global": 600,
}


def is_enabled() -> bool:
    """Kill switch — QX_RATE_LIMIT=0 disables all enforcement."""
    return os.environ.get("QX_RATE_LIMIT", "1") != "0"


def _limit_for(bucket: str) -> int:
    env_name = f"QX_RL_{bucket.upper()}"
    raw = os.environ.get(env_name, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULTS.get(bucket, 600)


def bucket_for_path(path: str) -> str:
    """Classify a request path into a rate-limit bucket.

    Mirrors the api_keys route classification (kept separate on purpose:
    auth *policy* and rate *policy* evolve independently).
    """
    if path.startswith("/api/auth/"):
        return "auth"
    if (path.startswith("/api/set-token")
            or path.startswith("/api/session/cookies")
            or path.startswith("/api/session/refresh")
            or path.startswith("/api/reconnect")):
        return "token_push"
    if path.startswith("/api/"):
        return "global"
    return "static"          # never limited (pages, JS, CSS, /healthz)


def hit(key: str, limit: int, window: float = 60.0) -> tuple[bool, float]:
    """Consume one unit from a fixed window.

    Returns (allowed, retry_after_seconds). retry_after is 0.0 when
    allowed, else the seconds until the current window rolls over.
    """
    global _LAST_PRUNE
    now = time.time()
    with _LOCK:
        # AUDIT FIX #5 (2026-10): the old prune did _COUNTERS.clear(),
        # which ALSO wiped ACTIVE windows (inflating effective limits and
        # briefly falsifying X-RateLimit-Remaining). Now removes only
        # STALE entries (older than 2x the max window).
        if now - _LAST_PRUNE > _PRUNE_INTERVAL:
            stale = [k for k, e in _COUNTERS.items()
                     if (now - e[0]) >= 2 * window + _PRUNE_INTERVAL]
            for k in stale:
                del _COUNTERS[k]
            _LAST_PRUNE = now
        entry = _COUNTERS.get(key)
        if not entry or (now - entry[0]) >= window:
            _COUNTERS[key] = [now, 1]
            return True, 0.0
        entry[1] += 1
        if entry[1] <= limit:
            return True, 0.0
        retry = max(0.05, window - (now - entry[0]))
        return False, retry


def headers_for(key: str, limit: int, window: float = 60.0) -> dict:
    """Truthful X-RateLimit-* headers for the CURRENT state of a key.

    Called after a successful `hit` so responses carry what the limiter
    actually enforces (limit / remaining in this window).
    """
    now = time.time()
    with _LOCK:
        entry = _COUNTERS.get(key)
        used = entry[1] if entry and (now - entry[0]) < window else 0
    remaining = max(0, limit - used)
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
    }


def reset() -> None:
    """Clear all counters (used by the test battery between scenarios)."""
    with _LOCK:
        _COUNTERS.clear()


def client_key(request) -> str:
    """Best-effort client identity for keying (falls back to 'local').

    AUDIT NOTE #8 (2026-10): by default ONLY the TCP peer address is used
    — X-Forwarded-For is deliberately NOT trusted (trivially spoofable
    when the app is directly exposed; spoofing would hand an attacker a
    fresh bucket per request). Behind a reverse proxy (Railway etc.) all
    visitors share the peer's buckets — set QX_TRUST_PROXY=1 there so the
    FIRST XFF hop (the address the PROXY saw) is used instead. Only do
    this when you trust your proxy to overwrite inbound XFF.
    """
    if os.environ.get("QX_TRUST_PROXY", "0") == "1":
        xff = ""
        try:
            xff = (request.headers.get("X-Forwarded-For") or "").strip()
        except Exception:
            xff = ""
        if xff:
            return xff.split(",")[0].strip() or "local"
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None
    return host or "local"
