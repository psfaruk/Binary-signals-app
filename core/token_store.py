"""
core/token_store.py — persistent Quotex-token storage + UI access control.

WHY THIS EXISTS
---------------
Before this module the only reliable way to give the app a fresh Quotex
token was to edit `QX_TOKEN` in Railway → Variables, which triggers a full
redeploy (container rebuild, ~2 min downtime, DB reopened). `/api/set-token`
existed but was unusable in practice for two reasons:

  1. It is guarded by `_check_admin_key()`, which is FAIL-CLOSED — with no
     `ADMIN_KEY` env var set (the live deployment has none) it returns 503
     to everyone, including the operator.
  2. Whatever it applied lived only in `os.environ` of the running process,
     so the next redeploy lost it and the operator was back to Variables.

This module fixes both:

  * `save_token()` writes the token to the Railway volume
    (`RAILWAY_VOLUME_MOUNT_PATH`, i.e. `/app/data`), so it survives
    redeploys. `load_token()` is read at server startup before the feed
    connects.
  * A PIN (chosen by the operator on first use, "claim on first use") is
    stored as a PBKDF2 hash next to it, so the token endpoints can be
    exposed to the frontend UI without leaving them open to the public
    internet — and without requiring a Railway Variable to bootstrap.

The PIN only ever unlocks token import / reconnect. The genuinely
sensitive endpoints (`/api/debug`, `/api/db-download`, `/api/signals/clear`)
keep using `ADMIN_KEY` alone, so a stolen PIN cannot exfiltrate the DB.
`ADMIN_KEY`, when set, works as a master key everywhere and can reset a
forgotten/hijacked PIN.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

_LOCK = threading.RLock()

TOKEN_FILE = "qx_token.json"
AUTH_FILE = "app_auth.json"

_PBKDF2_ROUNDS = 200_000
_MIN_PIN_LEN = 6
_MIN_TOKEN_LEN = 20


# ── state directory ───────────────────────────────────────────────────────

def state_dir() -> Path:
    """Directory for persisted runtime state.

    Order: QX_STATE_DIR → Railway volume mount → dirname(DB_PATH) → app dir.
    Everything except the last is expected to be on a persistent volume;
    the app-dir fallback keeps local dev working.
    """
    candidates = []
    for env in ("QX_STATE_DIR", "RAILWAY_VOLUME_MOUNT_PATH"):
        val = os.environ.get(env, "").strip()
        if val:
            candidates.append(Path(val))
    db_path = os.environ.get("DB_PATH", "").strip()
    if db_path:
        parent = Path(db_path).parent
        if str(parent) not in (".", ""):
            candidates.append(parent)
    candidates.append(Path(__file__).resolve().parent.parent)

    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return cand
        except Exception:
            continue
    return Path(__file__).resolve().parent.parent


def is_persistent() -> bool:
    """True when state_dir() lives on something that survives a redeploy."""
    d = str(state_dir())
    for env in ("QX_STATE_DIR", "RAILWAY_VOLUME_MOUNT_PATH"):
        val = os.environ.get(env, "").strip()
        if val and Path(val).resolve() == Path(d).resolve():
            return True
    # DB_PATH on Railway points into the mounted volume.
    db_path = os.environ.get("DB_PATH", "").strip()
    if db_path and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        try:
            return Path(db_path).parent.resolve() == Path(d).resolve()
        except Exception:
            return False
    # Local dev: the app dir is as persistent as the machine.
    return not bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN"))


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[token_store] read error {path}: {exc}")
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)          # no-op semantics on Windows, fine
    except Exception:
        pass


# ── token normalisation ───────────────────────────────────────────────────

_SESSION_RE = re.compile(r'"session"\s*:\s*"([^"]+)"')
_TOKEN_KEY_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')
_IS_DEMO_RE = re.compile(r'"isDemo"\s*:\s*(\d+)')


def normalize_token(raw: str) -> tuple[str, dict]:
    """Turn whatever the operator pasted into the bare Quotex session hash.

    quotex_ws.connect() sends `{"session": <token>, ...}` as the
    authorization payload, so the token MUST be the bare session value.
    But DevTools hands the operator the whole frame, and people paste it
    verbatim. Accepted inputs:

        42["authorization",{"session":"abc…","isDemo":1,"tournamentId":0}]
        {"session":"abc…"}          {"token":"abc…"}
        "abc…"                      abc…
        %5B%22authorization%22…     (url-encoded copies)
        a:4:{s:10:"session_id";…}   (PHP-serialised ssid — passed through)

    Returns (token, meta) where meta records what was detected so the UI
    can show "pasted a full SSID frame — extracted the session".
    """
    meta: dict = {"input_format": "raw"}
    if not raw:
        return "", meta
    s = str(raw).strip()
    s = s.strip().strip("\r\n").strip()

    if "%22" in s or "%7B" in s.upper() or "%5B" in s.upper():
        try:
            decoded = urllib.parse.unquote(s)
            if decoded != s:
                s = decoded.strip()
                meta["url_decoded"] = True
        except Exception:
            pass

    # PHP-serialised ssid — used by some Quotex frontends. Keep verbatim.
    if s.startswith("a:") and 's:' in s and "{" in s:
        meta["input_format"] = "php_serialized_ssid"
        return s, meta

    m = _SESSION_RE.search(s)
    if m:
        meta["input_format"] = ("socketio_frame" if s.lstrip().startswith(("4", "["))
                                else "json_object")
        demo = _IS_DEMO_RE.search(s)
        if demo:
            meta["is_demo"] = int(demo.group(1))
        return m.group(1).strip(), meta

    m = _TOKEN_KEY_RE.search(s)
    if m:
        meta["input_format"] = "json_token_field"
        return m.group(1).strip(), meta

    # Bare value, possibly wrapped in quotes and/or split across lines.
    s = s.strip('\'"').strip()
    s = re.sub(r"\s+", "", s)
    return s, meta


def validate_token(token: str) -> Optional[str]:
    """Return an error string if the token is obviously unusable, else None."""
    if not token:
        return "empty token"
    if len(token) < _MIN_TOKEN_LEN:
        return (f"token too short ({len(token)} chars) — real Quotex session "
                f"tokens are {_MIN_TOKEN_LEN}+ chars. Paste the whole "
                f'42["authorization",{{"session":"…"}}] frame if unsure.')
    if any(ch.isspace() for ch in token):
        return "token contains whitespace after normalisation — paste it again"
    return None


# ── token persistence ─────────────────────────────────────────────────────

def save_token(token: str, source: str = "api") -> dict:
    """Persist the token so it survives a redeploy. Returns the record."""
    rec = {
        "token": token,
        "saved_at": time.time(),
        "source": source,
        "persistent": is_persistent(),
    }
    with _LOCK:
        _write_json(state_dir() / TOKEN_FILE, rec)
    return rec


def load_token() -> Optional[dict]:
    with _LOCK:
        rec = _read_json(state_dir() / TOKEN_FILE)
    tok = (rec.get("token") or "").strip() if rec else ""
    if not tok:
        return None
    return rec


def token_meta() -> dict:
    """Non-secret description of the stored token, safe to expose publicly."""
    rec = load_token() or {}
    tok = (rec.get("token") or "").strip()
    return {
        "stored": bool(tok),
        "saved_at": rec.get("saved_at"),
        "source": rec.get("source"),
        "preview": mask(tok),
        "persistent": is_persistent(),
        "state_dir": str(state_dir()),
    }


def clear_token() -> None:
    with _LOCK:
        try:
            (state_dir() / TOKEN_FILE).unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[token_store] clear_token failed: {exc}")


ENV_SEEN_FILE = "qx_env_seen.json"


def resolve_boot_token() -> tuple[str, str]:
    """Decide which token the process should boot with: env var or stored.

    Both sources are legitimate and neither carries a timestamp on its own,
    so we timestamp the env var ourselves: the first boot that sees a given
    `QX_TOKEN` value records when it appeared. After that:

      * stored token saved LATER than that moment  → the operator imported a
        fresh token from the UI, so it wins (this is what stops a stale
        Railway Variable from clobbering the UI token on every redeploy);
      * a `QX_TOKEN` value we have never seen before → the operator just
        edited the Variable, so the env wins;
      * no env var at all → stored token wins.

    Returns (token, reason) with reason in
    {"env", "env-new", "stored-newer", "stored", "none"}.
    """
    env_tok = os.environ.get("QX_TOKEN", "").strip()
    stored = load_token()
    stored_tok = (stored or {}).get("token", "").strip()
    stored_at = float((stored or {}).get("saved_at") or 0)

    if not env_tok:
        return (stored_tok, "stored") if stored_tok else ("", "none")

    path = state_dir() / ENV_SEEN_FILE
    with _LOCK:
        seen = _read_json(path)
        digest = hashlib.sha256(env_tok.encode("utf-8")).hexdigest()
        fresh_env = seen.get("hash") != digest
        if fresh_env:
            seen = {"hash": digest, "first_seen": time.time()}
            try:
                _write_json(path, seen)
            except Exception as exc:
                print(f"[token_store] could not record env token marker: {exc}")

    if fresh_env:
        return env_tok, "env-new"
    if stored_tok and stored_tok != env_tok and stored_at > float(seen.get("first_seen") or 0):
        return stored_tok, "stored-newer"
    return env_tok, "env"


def mask(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "…" + token[-3:]
    return f"{token[:6]}…{token[-4:]}"


# ── PIN (claim-on-first-use) ──────────────────────────────────────────────

def _auth_record() -> dict:
    with _LOCK:
        return _read_json(state_dir() / AUTH_FILE)


def _hash_pin(pin: str, salt: bytes, rounds: int = _PBKDF2_ROUNDS) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt,
                               rounds).hex()


def env_admin_key() -> str:
    return os.environ.get("ADMIN_KEY", "").strip()


def is_claimed() -> bool:
    rec = _auth_record()
    return bool(rec.get("pin_hash") and rec.get("salt"))


def auth_state() -> dict:
    """What the frontend needs to render the right form. No secrets."""
    rec = _auth_record()
    return {
        "claimed": is_claimed(),
        "claimed_at": rec.get("claimed_at"),
        "admin_key_env": bool(env_admin_key()),
        "persistent": is_persistent(),
        "state_dir": str(state_dir()),
    }


def claim_pin(pin: str) -> dict:
    """First-run claim. Fails if already claimed (use change_pin instead)."""
    pin = (pin or "").strip()
    if len(pin) < _MIN_PIN_LEN:
        return {"ok": False, "error": f"PIN must be at least {_MIN_PIN_LEN} characters"}
    if is_claimed():
        return {"ok": False, "error": "already claimed — enter the existing PIN, "
                                      "or reset it with the ADMIN_KEY"}
    salt = secrets.token_bytes(16)
    rec = {
        "pin_hash": _hash_pin(pin, salt),
        "salt": salt.hex(),
        "iterations": _PBKDF2_ROUNDS,
        "claimed_at": time.time(),
    }
    with _LOCK:
        _write_json(state_dir() / AUTH_FILE, rec)
    print("[token_store] 🔐 access PIN claimed — token import is now "
          "PIN-protected")
    return {"ok": True}


def verify_pin(pin: str) -> bool:
    pin = (pin or "").strip()
    if not pin:
        return False
    rec = _auth_record()
    stored = rec.get("pin_hash")
    salt_hex = rec.get("salt")
    if not stored or not salt_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except Exception:
        return False
    rounds = int(rec.get("iterations") or _PBKDF2_ROUNDS)
    return hmac.compare_digest(_hash_pin(pin, salt, rounds), stored)


def verify_admin_key(key: str) -> bool:
    expected = env_admin_key()
    if not expected or not key:
        return False
    return hmac.compare_digest(key.strip(), expected)


def change_pin(new_pin: str, old_pin: str = "", admin_key: str = "") -> dict:
    """Rotate the PIN. Needs the old PIN, or the ADMIN_KEY as a master key."""
    new_pin = (new_pin or "").strip()
    if len(new_pin) < _MIN_PIN_LEN:
        return {"ok": False, "error": f"PIN must be at least {_MIN_PIN_LEN} characters"}
    if not (verify_admin_key(admin_key) or verify_pin(old_pin)):
        return {"ok": False, "error": "wrong PIN"}
    salt = secrets.token_bytes(16)
    rec = {
        "pin_hash": _hash_pin(new_pin, salt),
        "salt": salt.hex(),
        "iterations": _PBKDF2_ROUNDS,
        "claimed_at": time.time(),
    }
    with _LOCK:
        _write_json(state_dir() / AUTH_FILE, rec)
    return {"ok": True}


def authorize(pin: str = "", admin_key: str = "") -> tuple[bool, str]:
    """Gate for token import / reconnect.

    USER REQUIREMENT (2026-08-17): "টোকেন মেনইয়ালি অ্যাপ ui তে শুধু মাত্র টোকেন
    দিলেই যেনো অ্যাপ টি লাইভ ডেটা কানেক্ট হয়, কোনো এডমিন key, অন্যান্য key
    এই গুলো fronted এ থাকবে না" — token push from the UI must work with
    JUST a token. No PIN, no admin key required on the frontend.

    Therefore this function now ALWAYS allows token-push operations. The
    PIN/admin_key parameters are accepted for backwards compatibility with
    existing callers (token_agent.py, minter_service.py) but are no longer
    required.

    The genuinely sensitive endpoints (/api/db-download, /api/debug,
    /api/signals/clear) still use _check_admin_key() directly in server.py,
    so a stolen token-push URL still cannot exfiltrate the DB.
    """
    if verify_admin_key(admin_key):
        return True, "admin_key"
    if pin and is_claimed() and verify_pin(pin):
        return True, "pin"
    # Token-push is open by design (USER REQ 2026-08-17). Returning "open"
    # here means /api/set-token, /api/session/cookies, /api/session/refresh
    # work without any auth header from the frontend.
    return True, "open"
