"""
core/api_keys.py — Lightweight API key system for the Binary Signals app.

User requirements (2026-08-13):
  • "এই অ্যাপ url ব্যবহার করে সমস্ত ডাটা সিগন্যাল সিগন্যাল যে কেউ দেখতে পারবে।
     কোনো বাধা থাকবে না। প্রয়োজন হলে একটি api key সিস্টেম তৈরি করেন।"

Design:
  • Public read access — anyone with the URL can view signals, charts,
    history, share-signals. NO auth required for read endpoints.
  • API keys — optional, for programmatic clients that want higher rate
    limits and an audit trail. Keys are stored hashed (SHA-256) in SQLite.
  • Admin PIN (existing) — guards token-management endpoints.

Storage:
  • `api_keys` table in signals.db (auto-created on first call).
  • Columns: id, label, key_hash, key_prefix, created, last_used, active,
    rate_limit_per_min, total_requests.

Key format:
  • `qxa_` prefix + 32 hex chars = 36 chars total. Example:
    `qxa_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6`

Key transmission:
  • HTTP: `Authorization: Bearer qxa_...` header (preferred)
  • HTTP: `?api_key=qxa_...` query param (fallback for browsers)
  • WebSocket: `?api_key=qxa_...` query param on the /ws URL
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

# Try to use the project's DB layer for connection handling.
try:
    from db import _write_cursor, _read_cursor, DB_PATH  # type: ignore
    _USE_PROJECT_DB = True
except Exception:  # pragma: no cover — standalone fallback
    _USE_PROJECT_DB = False
    DB_PATH = os.environ.get(
        "DB_PATH",
        os.path.abspath(os.path.join(os.path.dirname(__file__) or "..", "signals.db")),
    )

    import contextlib

    @contextlib.contextmanager
    def _write_cursor():
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn.cursor()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def _read_cursor():
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn.cursor()
        finally:
            conn.close()


KEY_PREFIX = "qxa_"
KEY_BYTES = 16  # 32 hex chars
DEFAULT_RATE_LIMIT = 60  # requests per minute


@dataclass
class APIKeyInfo:
    """Public representation of an API key (no hash)."""
    id: int
    label: str
    key_prefix: str  # first 12 chars of the actual key, for display
    created: float
    last_used: Optional[float]
    active: bool
    rate_limit_per_min: int
    total_requests: int


def _hash_key(key: str) -> str:
    """SHA-256 hash of the API key. The raw key is never stored."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _generate_key() -> str:
    """Generate a new random API key. Returns the raw key (shown ONCE to user)."""
    return KEY_PREFIX + secrets.token_hex(KEY_BYTES)


def _ensure_table() -> None:
    """Create the api_keys table if it does not exist. Idempotent."""
    with _write_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                created REAL NOT NULL,
                last_used REAL,
                active INTEGER NOT NULL DEFAULT 1,
                rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
                total_requests INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(active)")


def create_key(label: str, rate_limit_per_min: int = DEFAULT_RATE_LIMIT) -> tuple[str, APIKeyInfo]:
    """Create a new API key.

    Args:
        label: human-readable name (e.g. "mobile-app", "telegram-bot")
        rate_limit_per_min: per-minute request cap

    Returns:
        (raw_key, info) — raw_key is shown only here, store it client-side.
    """
    _ensure_table()
    raw_key = _generate_key()
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:12]  # qxa_a1b2c3d4
    created = time.time()
    with _write_cursor() as cur:
        cur.execute("""
            INSERT INTO api_keys (label, key_hash, key_prefix, created, active, rate_limit_per_min, total_requests)
            VALUES (?, ?, ?, ?, 1, ?, 0)
        """, (label, key_hash, key_prefix, created, rate_limit_per_min))
        key_id = cur.lastrowid
    info = APIKeyInfo(
        id=key_id, label=label, key_prefix=key_prefix,
        created=created, last_used=None, active=True,
        rate_limit_per_min=rate_limit_per_min, total_requests=0,
    )
    return raw_key, info


def list_keys() -> list[APIKeyInfo]:
    """List all API keys (without hashes)."""
    _ensure_table()
    with _read_cursor() as cur:
        cur.execute("""
            SELECT id, label, key_prefix, created, last_used, active,
                   rate_limit_per_min, total_requests
            FROM api_keys
            ORDER BY created DESC
        """)
        rows = cur.fetchall()
    return [
        APIKeyInfo(
            id=r[0], label=r[1], key_prefix=r[2], created=r[3],
            last_used=r[4], active=bool(r[5]),
            rate_limit_per_min=r[6], total_requests=r[7],
        ) for r in rows
    ]


def revoke_key(key_id: int) -> bool:
    """Deactivate an API key by ID. Returns True if a row was updated."""
    _ensure_table()
    with _write_cursor() as cur:
        cur.execute("UPDATE api_keys SET active=0 WHERE id=?", (key_id,))
        return cur.rowcount > 0


def delete_key(key_id: int) -> bool:
    """Permanently delete an API key. Returns True if a row was deleted."""
    _ensure_table()
    with _write_cursor() as cur:
        cur.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
        return cur.rowcount > 0


def verify_key(key: str) -> Optional[APIKeyInfo]:
    """Verify an API key. Returns APIKeyInfo if valid+active, else None.

    Side effect: updates last_used and total_requests.
    """
    if not key or not key.startswith(KEY_PREFIX):
        return None
    _ensure_table()
    key_hash = _hash_key(key)
    with _read_cursor() as cur:
        cur.execute("""
            SELECT id, label, key_prefix, created, last_used, active,
                   rate_limit_per_min, total_requests
            FROM api_keys
            WHERE key_hash=? AND active=1
        """, (key_hash,))
        row = cur.fetchone()
    if not row:
        return None
    info = APIKeyInfo(
        id=row[0], label=row[1], key_prefix=row[2], created=row[3],
        last_used=row[4], active=bool(row[5]),
        rate_limit_per_min=row[6], total_requests=row[7],
    )
    # Update last_used + total_requests (best-effort, non-blocking on failure)
    try:
        with _write_cursor() as cur:
            cur.execute("""
                UPDATE api_keys
                SET last_used=?, total_requests=total_requests+1
                WHERE id=?
            """, (time.time(), info.id))
    except Exception:
        pass
    return info


def is_public_read_enabled() -> bool:
    """Whether public (no-key) read access is allowed.

    Default: True (per user requirement "যে কেউ দেখতে পারবে").
    Set QX_PUBLIC_READ=0 to require an API key for /api/* reads.
    """
    return os.environ.get("QX_PUBLIC_READ", "1") == "1"


# ── Endpoint classification ────────────────────────────────────────────────
# Read endpoints — public by default (anyone with URL can access).
# Write endpoints — require either admin PIN or a valid API key.
# Token-admin endpoints — require admin PIN (existing system).

_PUBLIC_READ_PREFIXES = (
    "/api/share-signals",
    "/api/pairs",
    "/api/allowlist",
    "/api/history",
    "/api/signals",
    "/api/stats",
    "/api/brain",
    "/api/patterns",
    "/api/time-patterns",
    "/api/module-analysis",
    "/api/theory-analysis",
    "/api/quality-analysis",
    "/api/pair-deep-stats",
    "/api/breakeven",
    "/api/pair-health",
    "/api/backtest",
    "/api/streaming-status",
    "/api/strategies",
    "/api/current-strategy",
    "/api/auto-tune",
    "/api/algorithm-changes",
    "/api/status",
    "/api/monitoring",
    "/healthz",
    "/api/token-status",  # token import status (read-only)
    "/api/auth/state",  # first-run check
    # ── Token-push endpoints — USER REQ 2026-08-17 ──
    # "টোকেন দিলেই ডেটা আসবে" — paste a token, get data. No PIN, no admin
    # key on the frontend. These accept POST {token: "..."} from anyone
    # with the URL.
    "/api/set-token",
    "/api/session/cookies",
    "/api/session/refresh",
    "/api/session/status",
    "/api/reconnect",
)

_ADMIN_ONLY_PREFIXES = (
    # USER REQ 2026-08-17: token-push endpoints (/api/set-token,
    # /api/session/cookies, /api/session/refresh, /api/reconnect) are
    # NO LONGER admin-gated. The frontend pushes tokens with NO PIN and
    # NO admin key. They fall through to the "unknown" branch below,
    # which defaults to public_read when QX_PUBLIC_READ=1 (the default).
    "/api/signals/clear",
    "/api/admin/",
    "/api/auth/claim",
    "/api/auth/change-pin",
    "/api/auth/verify",
    "/api/patterns/refresh",
    "/api/auto-tune/apply",
    "/api/pair-health/reset",
)

# Token-push endpoints that USED to be admin-only and are now PUBLIC.
# Listed explicitly so the test script (and any future audit) can verify
# the policy is correct.
TOKEN_PUSH_PREFIXES = (
    "/api/set-token",
    "/api/session/cookies",
    "/api/session/refresh",
    "/api/reconnect",
)

_API_KEY_WRITE_PREFIXES = (
    "/api/share-signals/save",
)


# Public sub-routes under /api/keys/* that bypass the api_key_write gate.
# /api/keys/verify is public so users can test their key without admin auth.
_PUBLIC_API_KEY_SUBROUTES = (
    "/api/keys/verify",
)


def classify_request(path: str) -> str:
    """Classify an HTTP path as 'public_read', 'admin', 'api_key_write', or 'unknown'.

    'unknown' routes default to requiring auth (fail-closed) when
    QX_PUBLIC_READ=0, and to public read when QX_PUBLIC_READ=1 (default).
    """
    if not path:
        return "unknown"
    # Public sub-routes (e.g. /api/keys/verify) — check BEFORE admin/write.
    for prefix in _PUBLIC_API_KEY_SUBROUTES:
        if path == prefix or path.startswith(prefix):
            return "public_read"
    for prefix in _ADMIN_ONLY_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return "admin"
    # /api/keys/* (except public sub-routes above) — admin-only management.
    if path == "/api/keys" or path.startswith("/api/keys/"):
        return "admin"
    for prefix in _API_KEY_WRITE_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return "api_key_write"
    for prefix in _PUBLIC_READ_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return "public_read"
    return "unknown"


def extract_key_from_request(request) -> Optional[str]:
    """Extract API key from FastAPI Request — checks Authorization header then ?api_key."""
    # 1. Authorization: Bearer qxa_...
    auth_header = request.headers.get("authorization", "") if hasattr(request, "headers") else ""
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header[7:].strip()
        if candidate.startswith(KEY_PREFIX):
            return candidate
    # 2. ?api_key=qxa_...
    if hasattr(request, "query_params"):
        candidate = request.query_params.get("api_key", "")
        if candidate and candidate.startswith(KEY_PREFIX):
            return candidate
    return None
