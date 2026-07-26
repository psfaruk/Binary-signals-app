# TODO (DEEP-AUDIT-2026-07-26 / F-20 / cross-file cleanup):
#   `make_client()` at line ~1184 has 0 callers across the entire codebase
#   (feed.py uses `QuotexWSClient(...)` constructor directly, NOT the
#   module-level `make_client` factory). Safe to DELETE in a follow-up batch
#   after confirming no external scripts import it. Audit ref: A-10 PROBLEM
#   table "Functions/classes defined but with limited/no external callers".
"""
Quotex raw WebSocket client — Socket.IO v3 over plain WebSocket.

Implements the architecture described in quotex-smooth-candle-mystery.txt:
  wss://ws2.market-qx.trade/socket.io/?EIO=3&transport=websocket

Why this exists (alongside pyquotex):
  pyquotex's HTTP login uses httpx whose TLS fingerprint Cloudflare rejects
  with 403 on the Quotex mirrors. pyquotex's own WebSocket layer also depends
  on that login flow. This module bypasses pyquotex entirely and speaks
  Socket.IO v3 directly over a raw WebSocket, so the feed keeps working even
  when Cloudflare is blocking the HTTP login.

  Auth: feed.py still uses qx_login.fetch_session() (curl_cffi, Chrome TLS
  fingerprint) to obtain a fresh ssid + cookies, then hands them to this
  client via set_session(). No HTTP login inside this module.

Flow:
  1. connect()                    → open WS, Socket.IO v3 handshake, periodic ping
  2. set_session(ssid=...)        → send 42["authorization", {"session":ssid,...}]
  3. start_candles_stream(asset, period) → 3-step subscribe:
        42["instruments/update", {"asset":asset,"period":period}]
        42["chart_notification/get", {"asset":asset,"version":"1.0.0"}]
        42["depth/follow", asset]
  4. Server pushes ticks: [["EURUSD_otc", ts, price, dir], ...]
     → stored in self._realtime[asset] (deque maxlen=1000)
  5. get_realtime_price(asset)    → snapshot of the in-memory buffer (no WS I/O)
  6. stop_candles_stream(asset)   → 42["depth/unfollow", asset]

Drop-in compatible with the subset of pyquotex's API that feed.py uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any    # FIX (2026-07-13): removed Iterable (unused)

# websockets is already a transitive dep of uvicorn[standard]; we add it
# explicitly to requirements.txt too so non-uvicorn callers can import this.
try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError as _e:  # pragma: no cover
    raise ImportError(
        "quotex_ws requires the 'websockets' package. "
        "pip install websockets") from _e


# ── Constants ───────────────────────────────────────────────────────────────

# FIX (DEEP-AUDIT-2026-07-26 / F-15-01): WS_URL is no longer the single source
# of truth for the WebSocket endpoint. The constructor accepts a `host` arg and
# derives its own WS URL (wss://ws2.<host>/socket.io/...) so the Origin header
# (https://<host>) and the WS endpoint always agree — previously a non-default
# host caused cross-origin WS upgrades to be rejected. WS_URL is kept only as
# the module-level default (used when host is the default "market-qx.trade").
WS_URL = os.environ.get(
    "QX_WS_URL",
    "wss://ws2.market-qx.trade/socket.io/?EIO=3&transport=websocket",
)
DEFAULT_HOST = "market-qx.trade"


def _build_ws_url(host: str) -> str:
    """Build the WebSocket URL for the given host.

    FIX (DEEP-AUDIT-2026-07-26 / F-15-01): previously WS_URL was computed at
    module-import time from env and the constructor's `host` parameter was
    ignored for the WS endpoint (only the Origin header used it). That meant
    `QuotexWSClient(host="qxbroker.com")` connected to ws2.market-qx.trade
    but sent `Origin: https://qxbroker.com` — a cross-origin WS upgrade that
    Quotex rejects. Now the URL is derived from `host`.
    """
    if not host:
        host = DEFAULT_HOST
    # Allow QX_WS_URL env override (e.g., for testing against a local mock).
    env_override = os.environ.get("QX_WS_URL")
    if env_override and host == DEFAULT_HOST:
        return env_override
    return f"wss://ws2.{host}/socket.io/?EIO=3&transport=websocket"


# Default to demo account (is_demo=1) — most personal Quotex accounts use
# the demo balance for signal analysis. Override via env if needed.
IS_DEMO = int(os.environ.get("QX_IS_DEMO", "1"))
TOURNAMENT_ID = int(os.environ.get("QX_TOURNAMENT_ID", "0"))

# Tick buffer size per asset — matches the 1000-tick cap from the file.
TICK_BUFFER_MAX = 1000

# Socket.IO v3 / Engine.IO v3 control bytes (per the spec):
#   Engine.IO: 0=open, 1=close, 2=ping, 3=pong, 4=message, 5=upgrade, 6=noop
#   Socket.IO: 0=connect, 1=disconnect, 2=event, 3=ack, 4=error,
#              5=binary-event, 6=binary-ack
# Combined outgoing event: "42" + json(["event", payload])

PING_INTERVAL = 25.0   # server's pingInterval is usually 25s
PING_TIMEOUT  = 60.0
CONNECT_TIMEOUT = 30.0
SUBSCRIBE_TIMEOUT = 10.0

# FIX (DEEP-AUDIT-2026-07-26 / F-15-29): module-level lock for session.json
# writes. Each QuotexWSClient instance references this via self._session_file_lock
# (a threading.Lock works fine for sync I/O even inside async code — file
# writes are quick enough that they don't block the event loop meaningfully).
_SESSION_FILE_LOCK = threading.Lock()


# FIX (DEEP-AUDIT-2026-07-26 / F-15-33): select the right "is the WS open?"
# checker ONCE at import time based on the installed websockets version.
# websockets v13+ replaced WebSocketClientProtocol with ClientConnection,
# whose API differs (`close_code` instead of `protocol.state` / `closed`).
def _make_ws_is_open_check():
    try:
        # v13+ ClientConnection: close_code is None while open, set on close.
        from websockets.exceptions import ConnectionClosed  # noqa: ensure imp
        # Probe by attribute existence on a dummy class.
        class _Probe:
            close_code = None
        _ = _Probe().close_code
        def _v13_check(ws) -> bool:
            try:
                return ws.close_code is None
            except AttributeError:
                return False
        return _v13_check
    except Exception:
        pass
    try:
        from websockets.protocol import State  # v10-15
        def _v10_check(ws) -> bool:
            try:
                return ws.protocol.state == State.OPEN
            except AttributeError:
                try:
                    return not ws.closed
                except AttributeError:
                    return False
        return _v10_check
    except ImportError:
        def _fallback_check(ws) -> bool:
            try:
                return not ws.closed
            except AttributeError:
                return False
        return _fallback_check


_WS_IS_OPEN_CHECK = _make_ws_is_open_check()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _socket_io_event(event: str, *args) -> str:
    """Build a Socket.IO v3 outgoing frame: 42["event", payload, ...]"""
    return "42" + json.dumps([event, *args])


def _parse_incoming(raw: str | bytes) -> tuple[str, Any]:
    """
    Parse an incoming Socket.IO v3 / Engine.IO v3 frame.

    Returns (kind, payload) where kind is one of:
      "open"        → engine.io open handshake (payload is dict)
      "ping"        → server ping, client should reply "3"
      "pong"        → server pong (ignored)
      "connect"     → socket.io connect ack (payload is dict / empty)
      "disconnect"  → socket.io disconnect
      "event"       → socket.io event (payload is [name, *args])
      "binary"      → binary event (payload is parsed JSON head; binary
                      attachments follow in subsequent frames — caller must
                      reassemble)
      "error"       → socket.io error
      "message"     → engine.io message (raw, unexpected)
      "unknown"     → could not classify
    """
    if isinstance(raw, bytes):
        # Binary frame — in Socket.IO v3 binary event, this is an attachment.
        # We don't currently reassemble binary attachments (Quotex's binary
        # history payload is handled separately by _parse_binary_history).
        return "binary", raw

    if not raw:
        return "unknown", None

    head = raw[0]
    body = raw[1:]

    if head == "0":
        # Engine.IO open. Body is JSON: {"sid":...,"upgrades":[],
        #                                "pingInterval":25000,"pingTimeout":60000}
        try:
            return "open", json.loads(body)
        except json.JSONDecodeError:
            return "open", {}
    if head == "1":
        return "close", None
    if head == "2":
        return "ping", None
    if head == "3":
        return "pong", None
    if head == "4":
        if not body:
            return "message", None
        sub = body[0]
        rest = body[1:]
        if sub == "0":
            try:
                return "connect", json.loads(rest) if rest else {}
            except json.JSONDecodeError:
                return "connect", {}
        if sub == "1":
            return "disconnect", None
        if sub == "2":
            # Event: 42["event_name", arg1, arg2, ...]
            try:
                arr = json.loads(rest)
                if isinstance(arr, list) and arr:
                    return "event", arr
            except json.JSONDecodeError:
                pass
            return "event", []
        if sub == "3":
            try:
                return "ack", json.loads(rest) if rest else []
            except json.JSONDecodeError:
                return "ack", []
        if sub == "4":
            try:
                return "error", json.loads(rest) if rest else {}
            except json.JSONDecodeError:
                return "error", {}
        if sub == "5":
            # Binary event: 451-[json] then 1 binary attachment follows.
            # The leading "1-" means 1 binary attachment. Parse the JSON
            # head; caller (or _parse_binary_history) handles the rest.
            try:
                # Strip the "<count>-" prefix
                dash = rest.find("-")
                if dash >= 0:
                    arr = json.loads(rest[dash + 1:])
                else:
                    arr = json.loads(rest)
                return "binary", arr
            except json.JSONDecodeError:
                return "binary", None
        return "message", body
    return "unknown", raw


def _parse_binary_history(payload: bytes) -> list[dict]:
    """
    Parse Quotex's binary history payload (the bytes that follow a
    451- control message). Format observed live:

      The payload starts with a 4-byte little-endian length header per
      candle record, followed by candle data. Each candle is encoded as:
        time (4 bytes LE)        → int
        open (8 bytes LE double) → float
        high (8 bytes LE double) → float
        low  (8 bytes LE double) → float
        close(8 bytes LE double) → float
      Total = 36 bytes per candle.

    This is best-effort — if parsing fails, returns []. The caller falls
    back to JSON history in that case.
    """
    if not payload or len(payload) < 36:
        return []
    candles: list[dict] = []
    rec_size = 36
    n = len(payload) // rec_size
    import struct
    for i in range(n):
        chunk = payload[i * rec_size:(i + 1) * rec_size]
        try:
            t, o, h, l, c = struct.unpack("<idddd", chunk)
            candles.append({
                "time":  int(t),
                "open":  float(o),
                "high":  float(h),
                "low":   float(l),
                "close": float(c),
            })
        except struct.error:
            break
    return candles


# ── Client ──────────────────────────────────────────────────────────────────

class QuotexWSClient:
    """Raw WebSocket client speaking Socket.IO v3 to Quotex.

    Drop-in replacement for the subset of pyquotex's Quotex API that
    feed.py uses. The methods that feed.py calls are:
      - connect()                          -> (ok: bool, reason: str)
      - set_session(user_agent, ssid, cookies)
      - start_candles_stream(asset, period)
      - stop_candles_stream(asset)
      - get_realtime_price(asset)          -> list[{"time","price"}]
      - get_instruments()                  -> list
      - get_payout_by_asset(asset)         -> int | None
      - get_candles(asset, end_from_time, offset, period)
      - get_historical_candles(asset, amount_of_seconds, period, max_workers)
      - close()
      - session_data (property → dict with "token")
    """

    def __init__(self,
                 email: str = "",
                 password: str = "",
                 host: str = DEFAULT_HOST,
                 lang: str = "en",
                 root_path: str | None = None,
                 reconnect_policy=None,
                 **_unused):
        # Mirror pyquotex's constructor signature so feed._make_client
        # doesn't need to know which implementation it's getting.
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-25): email/password/lang/root_path
        # are accepted for drop-in compat with pyquotex.Quotex, but this
        # client NEVER performs HTTP login (auth is via set_session only).
        # They are NOT stored as instance attributes — that would be dead
        # state. `host` IS stored because it's used to build the WS URL.
        self.host = host or DEFAULT_HOST
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-01): build the WS URL from host
        # so the WebSocket endpoint matches the Origin header.
        self._ws_url = _build_ws_url(self.host)
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-50): surface unknown kwargs so
        # caller typos (e.g. `acount_type`) raise instead of being silently
        # dropped.
        if _unused:
            raise TypeError(
                f"QuotexWSClient got unexpected keyword arguments: "
                f"{sorted(_unused)}"
            )
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-24): reconnect_policy is accepted
        # for API-compat with pyquotex.Quotex, but this client does not
        # implement auto-reconnect — feed.py owns reconnect logic. Stored
        # as a no-op attribute so callers can introspect what was passed.
        self._reconnect_policy = reconnect_policy  # TODO: implement if needed

        # Session / auth state
        self._ssid: str | None = None
        self._cookies: str | None = None
        self._user_agent: str = ""
        self.session_data: dict = {}   # mimics pyquotex — holds "token" after auth

        # Connection state
        self._ws = None
        self._connected = False
        self._authorized = False
        self._reader_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._closed_by_user = False
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-32): _auth_result was only set
        # inside _reader_loop (L591). If the reader died before assignment
        # (e.g. immediate ConnectionClosed), _wait_for_auth polled
        # getattr(..., "_auth_result", None) for 15s on every reconnect.
        # Initialize here so the first reader_loop iteration doesn't have a
        # race window where _auth_result is unset.
        self._auth_result: bool | None = None
        self._auth_fail_reason: str = ""

        # In-memory tick buffer: per-asset deque of {"time","price"} dicts
        self._realtime: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=TICK_BUFFER_MAX))

        # ── Event-driven tick callbacks (per-asset) ─────────────────────────
        # When the WS reader ingests a tick, it pushes to the deque AND fires
        # every registered callback for that asset. feed.py uses this to
        # bridge ticks into a per-stream asyncio.Queue — eliminating the
        # legacy 50ms polling loop and shaving ~25-50ms of latency off every
        # tick → browser-render hop. Callbacks receive the tick dict
        # {"time": float, "price": float} and may be async (scheduled on the
        # event loop) or sync (called inline).
        self._tick_callbacks: dict[str, list] = defaultdict(list)

        # FIX (DEEP-AUDIT-2026-07-26 / F-15-04): per-asset lock to serialize
        # concurrent get_candles() calls for the SAME asset. Previously a
        # second caller clobbered the first's pending future, causing the
        # first to time out after 15s with []. Now concurrent calls for the
        # same asset are queued — the second caller waits for the first to
        # complete and reuses its result.
        self._history_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Pending history requests: keyed by an asyncio.Future
        # {asset: future} — set by get_candles(), resolved by the reader loop
        # when the matching "history/load" event arrives.
        self._pending_history: dict[str, asyncio.Future] = {}
        # Partial binary history buffers: {asset: bytes}
        self._binary_history_buf: dict[str, bytes] = {}
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-07): binary history attachment
        # attribution. Previously a single `_binary_history_target` slot
        # was overwritten by the LATEST history/load JSON header, so when
        # multiple history requests were in flight, binary bytes were
        # attributed to the wrong asset. Now we use a FIFO queue of
        # (asset) — each JSON header pushes its asset, each binary
        # attachment pops the next asset.
        self._binary_history_queue: deque[str] = deque()

        # Subscribed assets (so stop_candles_stream only sends unfollow for
        # assets we actually subscribed)
        self._subscribed: set[str] = set()

        # Instruments cache (refreshed on connect)
        self._instruments: list = []

        # Payout cache: {asset: int} — populated by instruments/list parsing
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-26): previously _payouts was
        # never populated; get_payout_by_asset fell through to it and always
        # returned None. Now _merge_instrument and the instruments/list
        # handler populate it.
        self._payouts: dict[str, int] = {}

        # FIX (DEEP-AUDIT-2026-07-26 / F-15-29 / F-15-30): lock to serialize
        # concurrent session.json writes (read-modify-write pattern was
        # racy across coroutines).
        self._session_file_lock = _SESSION_FILE_LOCK

        # FIX (DEEP-AUDIT-2026-07-26 / F-15-35): server time offset (server
        # ts - local ts). Set by the `timesync` event handler; consumed by
        # callers that need server-aligned expiration times.
        self._server_time_offset: float = 0.0

        # FIX (DEEP-AUDIT-2026-07-26 / F-15-36): per-asset open/closed state
        # from chart_notification/update events. feed.py can query via
        # is_asset_open(asset) to skip signals on closed markets.
        self._asset_open_state: dict[str, bool] = {}

        # Pong-tracking (FIX 2026-07-13): timestamp of the last pong received
        # from the server. _ping_loop checks this to detect dead connections.
        self._last_pong: float = time.time()

        # FIX (DEEP-AUDIT-2026-07-26 / F-15-46): per-asset lock to prevent
        # the _ingest_tick vs stop_candles_stream race on the defaultdict
        # _realtime buffer. Without this, a tick could land between the
        # _tick_callbacks.pop and the _realtime.pop, recreating an empty
        # deque via defaultdict that the next get_realtime_price would
        # return as a 1-tick (stale) result.
        self._realtime_lock = asyncio.Lock()

    # ── Session / auth ────────────────────────────────────────────────────

    def set_session(self,
                    user_agent: str = "",
                    ssid: str | None = None,
                    cookies: str | None = None,
                    **_unused) -> None:
        """Called by feed.py after a successful qx_login.fetch_session().
        Stores the ssid + cookies so connect() can authorize on the WS."""
        self._user_agent = user_agent
        self._ssid = ssid
        self._cookies = cookies
        if ssid:
            self.session_data = {"token": ssid}

    # ── session.json support (2026-07-11) ─────────────────────────────────
    # Mirrors pyquotex's own session.json persistence so the app can reuse
    # a working token across restarts. feed.py calls load_session_json() on
    # startup; if a valid token + cookies are found, set_session() is called
    # automatically — no manual token refresh needed.

    @staticmethod
    def _session_json_path() -> str:
        """Locate session.json.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-27): previously used os.getcwd()
        unconditionally, which broke for PyInstaller-frozen apps (where the
        data dir differs from sys._MEIPASS). Now: respect QX_SESSION_PATH
        env override, then fall back to sys._MEIPASS (frozen), then CWD.
        Prefer `pyquotex.config.load_session` if full PyInstaller support
        is needed — this implementation is the lightweight equivalent.
        """
        env = os.environ.get("QX_SESSION_PATH")
        if env:
            return env
        # PyInstaller: _MEIPASS is set to the bundle's extracted dir.
        meipass = getattr(__import__("sys"), "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, "session.json")
        return os.path.join(os.getcwd(), "session.json")

    @staticmethod
    def load_session_json(email: str | None = None) -> dict | None:
        """Read session.json and return the account data for `email`.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-28): previously returned the FIRST
        account by insertion order — multi-account session.json files
        always loaded the wrong account. Now: if `email` is provided, look
        up that exact key; otherwise fall back to first entry (for
        backward compat with single-account files).

        Format: {"email@example.com": {"cookies": "...", "token": "...", "user_agent": "..."}}
        """
        path = QuotexWSClient._session_json_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                return None
            # FIX (F-15-28): look up the requested email, or fall back to first.
            if email and email in data:
                acct_key = email
                acct = data[acct_key]
            else:
                acct_key = next(iter(data))
                acct = data[acct_key]
            if not isinstance(acct, dict):
                return None
            return {
                "email":      acct_key,
                "token":      acct.get("token", ""),
                "cookies":    acct.get("cookies", ""),
                "user_agent": acct.get("user_agent", ""),
            }
        except FileNotFoundError:
            return None
        except Exception as exc:
            print(f"[quotex_ws] session.json read error: {exc}")
            return None

    @staticmethod
    def clear_session_json_token() -> None:
        """Clear the token field in session.json (set to None) so the next
        connect attempt doesn't reuse a rejected/expired token.
        Mirrors feed.py's _clear_stale_token() for pyquotex.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-29): wrap the read-modify-write
        in the module-level session-file lock so concurrent callers don't
        clobber each other's writes.
        """
        path = QuotexWSClient._session_json_path()
        with _SESSION_FILE_LOCK:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                changed = False
                for acct in data.values():
                    if isinstance(acct, dict) and acct.get("token"):
                        acct["token"] = None
                        changed = True
                if changed:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f)
                    print("[quotex_ws] cleared stale token in session.json")
            except FileNotFoundError:
                pass
            except Exception as exc:
                print(f"[quotex_ws] could not clear session.json token: {exc}")

    @staticmethod
    def save_session_json(email: str, token: str, cookies: str,
                          user_agent: str) -> None:
        """Save/update a working token in session.json so subsequent restarts
        reuse it (skip the login flow entirely).

        FIX (DEEP-AUDIT-2026-07-26 / F-15-29): wrap the read-modify-write in
        the module-level session-file lock. Without the lock, two concurrent
        callers (e.g., auth-refresh + /api/set-token endpoint) could read
        the same JSON, both append their entry, and one's writes would
        silently overwrite the other's.
        """
        path = QuotexWSClient._session_json_path()
        with _SESSION_FILE_LOCK:
            try:
                data = {}
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                data[email] = {
                    "cookies": cookies,
                    "token": token,
                    "user_agent": user_agent,
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
                print(f"[quotex_ws] saved working token to session.json ({email})")
            except Exception as exc:
                print(f"[quotex_ws] could not save session.json: {exc}")

    @staticmethod
    def save_token_only(token: str, email: str | None = None) -> None:
        """Persist only a token to session.json (used by /api/set-token endpoint).

        AUDIT-1-02 FIX: server.py was calling save_session_json(token) with 1
        arg, but that method requires 4 (email, token, cookies, user_agent).
        This raised TypeError, silently caught — token NEVER persisted across
        Railway redeployments. This convenience method fills cookies/ua with
        empty strings (they are not required for token-only auth).

        FIX (DEEP-AUDIT-2026-07-26 / F-15-30): previously hardcoded
        `email="runtime-token"` — multiple token-only saves would overwrite
        each other. Now: if `email` is None, generate a unique key
        (`runtime-token-<timestamp>`) so each token-only save is preserved
        as its own entry. Callers that want a specific email can pass it.
        """
        # FIX (F-15-30): unique key per save to avoid silent overwrites.
        # Use uuid4 for true uniqueness (time-based keys can collide within
        # the same second, which happens frequently in tests / rapid API calls).
        if not email:
            import uuid
            email = f"runtime-token-{uuid.uuid4().hex[:12]}"
        QuotexWSClient.save_session_json(
            email=email,
            token=token,
            cookies="",
            user_agent="",
        )

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> tuple[bool, str]:
        """Open the WebSocket and authorize with the stored ssid."""
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-03): previously short-circuited on
        # `self._connected and self._authorized` even when the socket was
        # actually dead (e.g., a prior process kill left the flags set with
        # no `finally` cleanup). feed.py then subscribed to streams that went
        # nowhere. Now verify the socket is actually open.
        if self._connected and self._authorized and self._ws_is_open():
            return True, "already connected"

        if not self._ssid:
            return False, "no ssid — call set_session() first"

        self._closed_by_user = False
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self._ws_url,   # FIX (F-15-01): derived from host arg
                    additional_headers={
                        "User-Agent": self._user_agent or
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36",
                        "Origin": f"https://{self.host}",
                        "Cookie": self._cookies or "",
                    },
                    max_size=None,        # binary history can be large
                    ping_interval=None,   # we send Socket.IO pings ourselves
                    ping_timeout=None,
                    close_timeout=5,
                ),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:
            # FIX (DEEP-AUDIT-2026-07-26 / F-15-25): previously stored
            # self._last_reason (a dead-state field). Now inline the reason
            # in the return value — no separate field.
            return False, f"ws connect failed: {exc}"

        # Start the reader loop (handles incoming frames + pings)
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

        # Wait for the engine.io open handshake
        try:
            await asyncio.wait_for(self._wait_for_open(), timeout=10)
        except asyncio.TimeoutError:
            await self._cleanup()
            return False, "engine.io open timeout"

        # Send Socket.IO connect ack
        try:
            await self._ws.send("40")
        except Exception as exc:
            await self._cleanup()
            return False, f"failed to send 40: {exc}"

        # Authorize
        try:
            auth_frame = _socket_io_event(
                "authorization",
                {"session": self._ssid,
                 "isDemo": IS_DEMO,
                 "tournamentId": TOURNAMENT_ID},
            )
            await self._ws.send(auth_frame)
        except Exception as exc:
            await self._cleanup()
            return False, f"failed to send auth: {exc}"

        # Wait for either authorization/accept OR authorization/reject.
        # FIX (2026-07-13): removed outer asyncio.wait_for(...) — _wait_for_auth
        # already has an internal 15s deadline and explicitly returns False on
        # timeout, so the double wrap was redundant.
        ok = await self._wait_for_auth()
        if not ok:
            await self._cleanup()
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-19): distinguish
            # timeout from reject so feed._connect doesn't clear a valid
            # token on a slow 15s timeout. _wait_for_auth now sets
            # self._auth_fail_reason; we forward it as the reason string.
            _reason = self._auth_fail_reason or "rejected"
            if _reason == "timeout":
                return False, "authorization timeout (15s)"
            elif _reason == "ws_closed":
                return False, "authorization failed (ws closed)"
            return False, "authorization rejected"

        self._connected = True
        self._authorized = True

        # Fetch instruments in the background — non-blocking, feed.py will
        # re-call get_instruments() explicitly when it needs them.
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-31): previously fire-and-forget
        # with no exception handler — if _fetch_instruments raised (e.g. WS
        # closed mid-send), the exception was swallowed and the task GC'd
        # silently. get_instruments() later returned [] with no clue why.
        # Now: log exceptions via a done-callback.
        _inst_task = asyncio.create_task(self._fetch_instruments())
        _inst_task.add_done_callback(self._on_bg_task_done)

        return True, "connected"

    @staticmethod
    def _on_bg_task_done(task: asyncio.Task) -> None:
        """Done-callback for fire-and-forget background tasks.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-31): surfaces exceptions from
        tasks that would otherwise be GC'd silently.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            print(f"[quotex_ws] background task {task} raised: "
                  f"{type(exc).__name__}: {exc}")

    async def _wait_for_open(self) -> None:
        """Wait for the engine.io "0" open frame (handled inside the reader
        loop, which sets _engine_open)."""
        deadline = time.time() + 10
        while time.time() < deadline:
            if getattr(self, "_engine_open", False):
                return
            await asyncio.sleep(0.05)
        raise asyncio.TimeoutError()

    async def _wait_for_auth(self) -> bool:
        """Wait for the authorization/accept event (handled inside the
        reader loop, which sets _authorized_event).

        FIX (2026-07-13): used to fall off the end (returning None) on
        timeout. The caller did `if not ok: ... "authorization rejected"`,
        and `not None` is True, so a TIMEOUT was misreported as "rejected".
        Now explicitly returns False on timeout.

        FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-19): set
        self._auth_fail_reason so the caller can distinguish a TIMEOUT
        from an actual REJECT. Previously both were labeled
        "authorization rejected", which caused feed._connect to
        _clear_stale_token on a slow-but-valid token (15s timeout) and
        then fall back to email/password login (Cloudflare-blocked on
        Railway). Now timeout reports "authorization timeout" so
        feed._connect can keep the token and retry.
        """
        deadline = time.time() + 15
        while time.time() < deadline:
            if getattr(self, "_auth_result", None) is not None:
                if self._auth_result:
                    self._auth_fail_reason = ""
                else:
                    self._auth_fail_reason = "rejected"
                return self._auth_result
            if not self._ws_is_open():
                self._auth_fail_reason = "ws_closed"
                return False
            await asyncio.sleep(0.05)
        # Explicit timeout — caller can distinguish via _last_reason if needed
        self._auth_fail_reason = "timeout"
        return False

    def _ws_is_open(self) -> bool:
        """Check if the WebSocket connection is open.
        Handles websockets v10- (legacy), v12-15 (protocol.state),
        and v13+ / v16+ (ClientConnection with close_code).

        FIX (2026-07-13): in websockets v13+, WebSocketClientProtocol was
        replaced with ClientConnection, which has neither .protocol.state
        nor .closed. Both old try-blocks raised AttributeError, so the
        method returned False even on a live connection — _ping_loop exited,
        _cleanup skipped ws.close(), stop_candles_stream took the early
        return, get_candles returned []. Now tries close_code (v13+) too.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-33): previously tried all three API
        styles on EVERY call (3 try/except blocks). Now selects the right
        checker ONCE at module import based on the installed websockets
        version, then dispatches via a single callable.
        """
        if self._ws is None:
            return False
        return _WS_IS_OPEN_CHECK(self._ws)

    async def _reader_loop(self) -> None:
        """Background task: read frames, dispatch by type, push ticks into
        the per-asset deques. Dies when the socket closes."""
        self._engine_open = False
        self._auth_result = None
        try:
            async for raw in self._ws:
                # websockets delivers str for text frames, bytes for binary
                kind, payload = _parse_incoming(raw)
                try:
                    await self._dispatch(kind, payload, raw)
                except Exception as exc:
                    print(f"[quotex_ws] dispatch error ({kind}): {exc}")
        except ConnectionClosed:
            pass
        except Exception as exc:
            print(f"[quotex_ws] reader loop died: {exc}")
        finally:
            self._connected = False
            self._authorized = False
            # Wake any pending history waiters
            for fut in list(self._pending_history.values()):
                if not fut.done():
                    fut.set_exception(
                        RuntimeError("connection closed during history fetch"))
            self._pending_history.clear()

    async def _dispatch(self, kind: str, payload: Any, raw) -> None:
        if kind == "open":
            self._engine_open = True
        elif kind == "ping":
            # Server wants a pong
            try:
                await self._ws.send("3")
            except Exception:
                pass
        elif kind == "pong":
            # FIX (2026-07-13): server responded to our ping — record the
            # timestamp so _ping_loop can detect a dead connection (no pong
            # for > 2× PING_INTERVAL → break and trigger reconnect).
            self._last_pong = time.time()
        elif kind == "connect":
            # Socket.IO connect ack — nothing to do
            pass
        elif kind == "event":
            await self._handle_event(payload)
        elif kind == "binary":
            # Could be a binary history payload or a binary event header.
            # If it's the header (451-...), the next binary frame is the
            # actual data. If it's bytes, it's an attachment.
            await self._handle_binary(payload, raw)
        elif kind == "error":
            print(f"[quotex_ws] socket.io error: {payload}")
        elif kind == "close":
            # FIX (DEEP-AUDIT-2026-07-26 / F-15-34): previously only printed
            # a log line — server-sent engine.io close frame (head="1") did
            # NOT clear `_connected` / `_authorized`. Other coroutines
            # polling `_connected` saw True even though the socket was
            # closed. Race window before `_reader_loop`'s `finally` block
            # ran. Now: clear the flags immediately so callers see the
            # disconnect without waiting for the reader's `finally`.
            print("[quotex_ws] server closed the engine.io connection")
            self._connected = False
            self._authorized = False

    async def _handle_event(self, arr: list) -> None:
        """Dispatch a Socket.IO event: ["event_name", *args]."""
        if not arr:
            return
        name = arr[0]
        args = arr[1:]

        # FIX (DEEP-AUDIT-2026-07-26 / A-08-CRIT-1):
        # Quotex sends `s_authorization` as the WS message field name (see
        # pyquotex/api.py:285), NOT `authorization/accept` or
        # `authorization/success`. The standalone `quotex_ws.py` client
        # listened for the wrong event names, so on every reconnect the
        # auth-accept event was missed and the client silently timed out
        # after 15s. Add `s_authorization` to the accept set; keep the old
        # names for backward compat (in case some legacy Quotex server still
        # emits them, or a future fork renames back).
        if name in ("authorization/accept", "authorization/success", "s_authorization"):
            self._auth_result = True
            self._authorized = True
        elif name in ("authorization/reject", "authorization/error"):
            self._auth_result = False
            self._authorized = False
            print(f"[quotex_ws] ⚠️  authorization rejected: {args}")
            print(f"[quotex_ws]    Token has expired or is invalid.")
            print(f"[quotex_ws]    App will auto-relogin on next retry cycle.")
            # AUDIT-1-18 FIX: previously, the ping loop kept re-sending the
            # same expired ssid every 5 min, and the WS stayed open. feed.py
            # thought the connection was healthy (because _connected=True)
            # but no ticks flowed. Now: close the WS so feed.py's reader-loop
            # finally block fires and triggers a full reconnect with a fresh
            # token (or falls back to sim mode after retries exhaust).
            try:
                if self._ws is not None:
                    await self._ws.close()
                    print("[quotex_ws] closed WS after auth/reject — "
                          "feed.py will reconnect with a fresh token")
            except Exception as _exc:
                print(f"[quotex_ws] WS close after auth/reject failed: {_exc}")
        elif name == "instruments/update":
            # Per-instrument update — usually a list with one entry
            if args and isinstance(args[0], list):
                for inst in args[0]:
                    self._merge_instrument(inst)
        elif name == "instruments/list":
            # Full instruments list
            # FIX (DEEP-AUDIT-2026-07-26 / F-15-26): also populate the
            # _payouts cache from the list response so get_payout_by_asset
            # has data even when the per-instrument shape doesn't match
            # the inst[-9] assumption.
            if args and isinstance(args[0], list):
                self._instruments = args[0]
                for inst in args[0]:
                    self._cache_payout(inst)
        elif name == "history/load":
            # JSON-formatted history response
            if args and isinstance(args[0], dict):
                asset = args[0].get("asset") or args[0].get("instrument")
                data = args[0].get("data") or args[0].get("candles") or []
                fut = self._pending_history.get(asset)
                if fut and not fut.done():
                    fut.set_result(data)
        elif name in ("timesync",):
            # FIX (DEEP-AUDIT-2026-07-26 / F-15-35): previously `pass` —
            # server time sync was discarded. Trade expiration relies on
            # server time (api.py settings_apply uses int(time.time())) which
            # may differ from server time across timezones. Now: store the
            # offset (server_ts - local_ts) so callers can correct.
            try:
                if args and isinstance(args[0], dict):
                    server_ts = float(args[0].get("time", 0))
                    if server_ts > 0:
                        self._server_time_offset = server_ts - time.time()
            except (TypeError, ValueError):
                pass
        elif name == "chart_notification/update":
            # FIX (DEEP-AUDIT-2026-07-26 / F-15-36): previously `pass` —
            # asset open/close transitions were missed. feed.py may place
            # signals on closed markets → "asset not available" rejections.
            # Now: parse the asset + open state into self._asset_open_state.
            try:
                if args and isinstance(args[0], dict):
                    body = args[0]
                    asset = body.get("asset") or body.get("instrument")
                    if asset:
                        # Quotex's chart_notification/update contains a
                        # "data" field with state info; fall back to presence
                        # of body as proxy for "open".
                        data = body.get("data") or {}
                        if isinstance(data, dict) and "isOpened" in data:
                            self._asset_open_state[asset] = bool(data["isOpened"])
                        elif "is_open" in body:
                            self._asset_open_state[asset] = bool(body["is_open"])
            except (TypeError, ValueError):
                pass
        # Tick data is delivered as a list of [asset, ts, price, direction]
        # tuples — sent under various event names depending on Quotex version.
        # We detect by shape: a list whose first element is a list of 4 items.
        # FIX (2026-07-13): this used to be a standalone `if` (not `elif`)
        # that ran after EVERY event handler. `instruments/list` payloads are
        # `[[id, "EURUSD_otc", ...], ...]` — each instrument is a list with
        # ≥3 elements, so the shape check matched and every instrument was
        # passed to _ingest_tick (which silently failed on float(name_string)).
        # Now guard with `elif` so known non-tick events don't fall through.
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-06): also accept single-tick
        # events where `args[0]` IS the tick itself (a 4-element list with
        # a string asset name) — previously required `args[0]` to be a LIST
        # OF TICKS, so a single-tick frame `["event_name",
        # ["EURUSD_otc", ts, price, dir]]` was silently dropped.
        elif (args and isinstance(args[0], list)
                and args[0] and isinstance(args[0][0], (list, tuple))
                and len(args[0][0]) >= 3
                and name not in ("instruments/list", "instruments/update",
                                 "history/load", "chart_notification/update",
                                 "timesync")):
            for tick in args[0]:
                self._ingest_tick(tick)
        elif (args and isinstance(args[0], list)
                and len(args[0]) >= 3
                and isinstance(args[0][0], str)
                and name not in ("instruments/list", "instruments/update",
                                 "history/load", "chart_notification/update",
                                 "timesync")):
            # Single-tick frame: args[0] is the tick itself.
            self._ingest_tick(args[0])

    def _ingest_tick(self, tick: list) -> None:
        """Push a tick into the in-memory buffer AND fire any registered
        event-driven callbacks. Tick shape: [asset, timestamp, price, dir?]"""
        if not tick or len(tick) < 3:
            return
        try:
            asset = tick[0]
            ts = float(tick[1])
            price = float(tick[2])
        except (TypeError, ValueError):
            return
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-20): dedup against the
        # last 5 ticks instead of just the last one. On reconnect, Quotex
        # may resend the last 5-10 ticks (Socket.IO replay); only the very
        # last was being deduplicated, so older duplicates with timestamps
        # matching earlier buffer entries were appended — inflating
        # buy_pct/sell_pct and crosses in the microstructure analysis.
        # FIX (DEEP-AUDIT-2026-07-26 / A-08-CRIT-2):
        # The previous dedup matched on `_t["time"] == ts` ONLY. Quotex
        # sometimes truncates tick timestamps to whole seconds; multiple
        # distinct ticks within the same second shared `ts`, so only the
        # first was kept and the rest were silently dropped. This starved
        # the microstructure analysis (per-tick volume, buy/sell pressure)
        # of data during high-activity periods where 3-5 ticks/sec is
        # common. Now dedup on (timestamp, price) tuple — true duplicates
        # match both fields, distinct ticks within the same second are kept.
        buf = self._realtime[asset]
        for _t in list(buf)[-5:]:
            if _t["time"] == ts and _t["price"] == price:
                return
        tick_dict = {"time": ts, "price": price}
        buf.append(tick_dict)
        # ── Event-driven fan-out ──────────────────────────────────────────
        # Feed.py registers a callback per (asset, period) stream that puts
        # the tick into an asyncio.Queue, which the stream loop awaits
        # directly — no 50ms polling delay. Async callbacks are scheduled on
        # the event loop (we're already in the reader task, so this is safe).
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-38): iterate over a SNAPSHOT of
        # the callback list so a concurrent unregister_tick_callback() call
        # (which mutates the list via .remove()) can't trigger
        # "list changed size during iteration" RuntimeError or skip a
        # callback that should have fired.
        for cb in list(self._tick_callbacks.get(asset, [])):
            try:
                result = cb(tick_dict)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:
                print(f"[quotex_ws] tick callback error for {asset}: {exc}")

    # ── Event-driven callback registration ────────────────────────────────

    def register_tick_callback(self, asset: str, callback) -> None:
        """Register a sync or async callback that fires whenever a new tick
        for `asset` is ingested. Used by feed.py to bridge ticks into a
        per-stream asyncio.Queue for event-driven candle updates.

        Callback signature: callback(tick: dict) -> None | coroutine
        where tick = {"time": float, "price": float}
        """
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-37): previously appended without
        # dedup — calling register_tick_callback twice with the SAME
        # callback caused every tick to fire it twice, double-counting in
        # microstructure analysis. Now: skip if already registered.
        cbs = self._tick_callbacks[asset]
        if callback not in cbs:
            cbs.append(callback)

    def unregister_tick_callback(self, asset: str, callback) -> None:
        """Remove a previously-registered callback. Safe to call even if the
        callback was never registered."""
        cbs = self._tick_callbacks.get(asset, [])
        if callback in cbs:
            cbs.remove(callback)
        if not cbs:
            self._tick_callbacks.pop(asset, None)

    async def _handle_binary(self, payload: Any, raw) -> None:
        """Handle a binary-event header or a binary attachment.

        FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-3-08): previously a
        binary attachment was attributed to ALL pending assets in
        _pending_history. On reconnect, feed.py re-arms streams
        staggered (~0.5s apart) and multiple history fetches can be
        in-flight simultaneously — the attachment for stream A was
        also attributed to stream B → cross-pair history
        contamination. Now we track the asset from the JSON header in
        self._binary_history_target and only attribute binary bytes to
        that specific asset.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-07): the single-slot
        `_binary_history_target` was still racy when multiple
        history/load JSON headers arrived before their respective
        binary attachments (e.g. server pipelining). The LATEST header
        overwrote the target, so the NEXT binary bytes (which actually
        belonged to an earlier header) were attributed to the wrong
        asset. Now: use a FIFO queue (`_binary_history_queue`) — each
        JSON header pushes its asset, each binary attachment pops the
        next asset in order. This correctly matches attachments to
        their headers even when interleaved.
        """
        # If payload is bytes, it's a binary attachment — append to the
        # asset at the front of the FIFO queue.
        if isinstance(payload, (bytes, bytearray)):
            target = None
            # FIX (F-15-07): pop next asset from the FIFO queue.
            if self._binary_history_queue:
                target = self._binary_history_queue.popleft()
            # Backward-compat fallback: legacy single-slot target.
            if target is None:
                target = getattr(self, "_binary_history_target", None)
                if target is not None:
                    self._binary_history_target = None
            if target and target in self._pending_history:
                fut = self._pending_history[target]
                if not fut.done():
                    self._binary_history_buf[target] = (
                        self._binary_history_buf.get(target, b"") + bytes(payload))
            else:
                # Last-resort fallback: legacy behavior (attribute to all
                # pending). Should rarely fire now that we have the queue.
                # FIX (F-15-07): copy the list before iterating — a future
                # resolution inside the loop may mutate _pending_history.
                for asset, fut in list(self._pending_history.items()):
                    if not fut.done():
                        self._binary_history_buf[asset] = (
                            self._binary_history_buf.get(asset, b"") + bytes(payload))
            return

        # payload is the parsed JSON head: [event_name, body, ...]
        if isinstance(payload, list) and payload:
            event = payload[0]
            if event == "history/load" and len(payload) >= 2:
                body = payload[1] or {}
                asset = body.get("asset") or body.get("instrument")
                if asset and asset in self._pending_history:
                    # FIX (F-15-07): push the asset to the FIFO queue so the
                    # subsequent binary attachment is attributed to THIS
                    # asset only, even if other history/load headers arrive
                    # before its attachment.
                    self._binary_history_queue.append(asset)
                    # Keep legacy single-slot in sync (for any code paths that
                    # still read it).
                    self._binary_history_target = asset
                    # FIX (2026-07-13): the binary attachment will follow in
                    # the next frame; _handle_binary(bytes) above collects it
                    # into _binary_history_buf. The old code did `pass` here
                    # and claimed get_candles() would "poll the buffer" — but
                    # get_candles() does `await fut`, not poll, so it timed
                    # out after 15s and returned [] without ever checking the
                    # buffer. Now we resolve the future with an empty list so
                    # get_candles() unblocks and falls through to the binary
                    # buffer check at the end of the method.
                    fut = self._pending_history[asset]
                    if not fut.done():
                        fut.set_result([])   # unblock get_candles — it'll
                                             # read the binary buffer next

    async def _ping_loop(self) -> None:
        """Send Socket.IO pings to keep the connection alive AND refresh the
        session token periodically.

        FIX (2026-07-13): added pong-timeout detection. The old loop sent
        pings every 25s but never checked if a pong came back. If the
        server silently dropped the connection (no TCP FIN — common with
        cloud LBs), the reader loop blocked forever and the ping loop
        kept sending into the void. Now: if no pong in ~2.5× PING_INTERVAL,
        break the loop and let _reader_loop's finally block clean up.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-39): aligned the threshold with
        the docstring (2.5× PING_INTERVAL, was the actual code; the
        docstring said "2×"). The 2.5× margin is intentional — PING_TIMEOUT
        is 60s on the server, so a single missed pong still leaves room
        for the next cycle before we declare the connection dead.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-40): previously re-sent the SAME
        `self._ssid` every 5 min for re-auth, even if a prior reject had
        set `_auth_result = False`. This caused repeated failed auth
        attempts (rate-limit / ban risk). Now: skip re-auth if we know the
        ssid is dead.
        """
        last_reauth = time.time()
        REAUTH_INTERVAL = 300  # 5 minutes
        self._last_pong = time.time()   # initialize so the first check passes

        try:
            while self._ws and self._ws_is_open():
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and self._ws_is_open():
                    # Pong-timeout check: if no pong in 2.5× PING_INTERVAL,
                    # the connection is dead — break out and let cleanup fire.
                    if time.time() - self._last_pong > PING_INTERVAL * 2.5:
                        print(f"[quotex_ws] no pong in {PING_INTERVAL * 2.5:.0f}s — "
                              f"connection presumed dead, breaking ping loop")
                        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-3-07):
                        # force-close the WS so _reader_loop's `async for raw
                        # in self._ws:` raises ConnectionClosed immediately,
                        # triggering its finally block (which sets
                        # _connected=False and wakes feed.py's reconnect).
                        # Without this, the reader loop stays blocked on the
                        # dead socket for up to ~60s waiting for a TCP FIN
                        # from the silently-dropped connection.
                        try:
                            if self._ws is not None:
                                await self._ws.close()
                        except Exception:
                            pass
                        break
                    try:
                        # Engine.IO ping
                        await self._ws.send("2")
                    except Exception:
                        break

                    # Periodic re-authorization to refresh session
                    now = time.time()
                    # FIX (F-15-40): skip re-auth if the ssid has been
                    # explicitly rejected — re-sending it just generates
                    # more failed auth attempts server-side.
                    if (now - last_reauth > REAUTH_INTERVAL
                            and self._ssid
                            and self._auth_result is not False):
                        try:
                            reauth_frame = _socket_io_event(
                                "authorization",
                                {"session": self._ssid,
                                 "isDemo": IS_DEMO,
                                 "tournamentId": TOURNAMENT_ID})
                            await self._ws.send(reauth_frame)
                            last_reauth = now
                            # Silent — don't log every 5 min
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass

    async def _cleanup(self) -> None:
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-41): previously cancelled the
        # tasks but never awaited them — on Python 3.11+, the cancellation
        # may not complete before `self._ws = None` is set, causing
        # "Task was destroyed but it is pending!" warnings and possible
        # partial state corruption. Now: gather the cancelled tasks with
        # return_exceptions=True so cleanup is deterministic.
        tasks_to_await: list[asyncio.Task] = []
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            tasks_to_await.append(self._reader_task)
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            tasks_to_await.append(self._ping_task)
        if self._ws and self._ws_is_open():
            try:
                await self._ws.close()
            except Exception:
                pass
        if tasks_to_await:
            try:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)
            except Exception:
                pass
        self._ws = None
        self._connected = False
        self._authorized = False

    async def close(self) -> None:
        self._closed_by_user = True
        await self._cleanup()
        # FIX (DEEP-AUDIT-2026-07-26 / F-15-42): clear the subscribed set so
        # that a subsequent connect() doesn't send depth/unfollow for assets
        # we never subscribed in the new session (server-side rate-limit risk).
        self._subscribed.clear()

    # ── Instruments ───────────────────────────────────────────────────────

    async def _fetch_instruments(self) -> None:
        """Request the full instruments list. Response comes async via
        the 'instruments/list' event."""
        if not self._ws or not self._ws_is_open():
            return
        try:
            await self._ws.send(_socket_io_event("instruments/list", {}))
        except Exception as exc:
            print(f"[quotex_ws] instruments/list request failed: {exc}")

    def _merge_instrument(self, inst: list) -> None:
        """Update or append a single instrument in self._instruments.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-26): also populate self._payouts
        so get_payout_by_asset has a fallback when the instruments tuple
        shape doesn't expose the 1M payout field at index -9.
        """
        if not inst or len(inst) < 2:
            return
        name = inst[1]
        for i, existing in enumerate(self._instruments):
            if existing and len(existing) > 1 and existing[1] == name:
                self._instruments[i] = inst
                self._cache_payout(inst)
                return
        self._instruments.append(inst)
        self._cache_payout(inst)

    def _cache_payout(self, inst: list) -> None:
        """Cache the 1M payout for this instrument into self._payouts.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-26): previously _payouts was
        never populated. Now extracted as a helper so both the
        instruments/list handler and _merge_instrument call it.
        """
        if not inst or len(inst) <= 9:
            return
        try:
            name = inst[1]
            payout = int(inst[-9])
            if name and payout >= 0:
                self._payouts[name] = payout
        except (TypeError, ValueError, IndexError):
            pass

    async def get_instruments(self) -> list:
        """Return the cached instruments list, refreshing once if empty.

        FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-1-27): previously waited
        only 2s (20 × 0.1s) for the instruments/list response. On slow
        connections (Railway → Quotex latency often >2s), _instruments
        was empty → feed._load_pairs returned early → all pairs had
        payout=None → _reconcile_always_on treated ALL pairs as locked
        (payout is None or payout < floor). No always-on streams would
        start. Now retry up to 3 times with a 5s wait per attempt.
        """
        for _attempt in range(3):
            if not self._instruments:
                await self._fetch_instruments()
                # Wait up to 5s for the response (50 × 0.1s)
                for _ in range(50):
                    if self._instruments:
                        break
                    await asyncio.sleep(0.1)
            if self._instruments:
                break
        return list(self._instruments)

    def get_payout_by_asset(self, asset: str) -> int | None:
        """Return the cached 1-minute payout % for an asset, or None.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-43): previously used `inst[-9]`
        (negative index) which silently returns the wrong field if Quotex
        adds a new column to the instrument tuple. Now also consult the
        self._payouts cache (populated by _cache_payout) which is keyed by
        asset name and immune to tuple-shape changes.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-44): previously did exact match
        `inst[1] == asset` only — feed.py may pass "EURUSD" while Quotex
        has "EURUSD_otc". Now tries both the literal asset name AND the
        "_otc" variant (and vice-versa) so OTC/non-OTC mismatches don't
        cause payout lookups to silently fail.
        """
        if not asset:
            return None
        # Build a list of candidate asset names to try — literal + _otc variant
        candidates: list[str] = [asset]
        if asset.endswith("_otc"):
            candidates.append(asset[:-4])
        else:
            candidates.append(f"{asset}_otc")

        # First: try the payout cache (populated by _cache_payout).
        for cand in candidates:
            if cand in self._payouts:
                return self._payouts[cand]

        # Fallback: scan instruments list. Use a positive index bound check
        # instead of `inst[-9]` to avoid silent mis-indexing if the schema
        # grows. Position -9 from the end == position (len-9) from the start.
        for inst in self._instruments:
            if not inst or len(inst) <= 9:
                continue
            if inst[1] not in candidates:
                continue
            try:
                # FIX (F-15-43): prefer positive index lookup with a length
                # check above; -9 still works but the explicit guard makes
                # the intent clear and protects against schema changes.
                payout = int(inst[len(inst) - 9])
                if payout >= 0:
                    return payout
            except (TypeError, ValueError, IndexError):
                continue
        return None

    # ── Stream lifecycle (3-step subscribe per asset) ─────────────────────

    async def start_candles_stream(self, asset: str, period: int) -> bool:
        """Subscribe to live ticks for one asset. Sends 3 frames per the
        quotex-smooth-candle-mystery.txt spec:

            42["instruments/update", {"asset":asset,"period":period}]
            42["chart_notification/get", {"asset":asset,"version":"1.0.0"}]
            42["depth/follow", asset]   ← depth/follow takes a STRING payload

        FIX (DEEP-AUDIT-2026-07-26 / F-15-45): previously raised
        RuntimeError("WebSocket not connected") — if the caller wrapped the
        call in `try: ... except Exception:` (common pattern), the exception
        was silently swallowed with NO log. Stream never started, no retry.
        Now: log the failure explicitly AND still raise (changing return
        type would break callers) so silent catches at least leave a trace.
        Returns True on success.
        """
        if not self._ws or not self._ws_is_open():
            # FIX (F-15-45): explicit log so silent except-Exception in the
            # caller doesn't hide the failure entirely.
            print(f"[quotex_ws] start_candles_stream({asset!r}) — "
                  f"WebSocket not connected, cannot subscribe")
            raise RuntimeError("WebSocket not connected")

        # Step 1: register interest in (asset, period)
        await self._ws.send(_socket_io_event(
            "instruments/update",
            {"asset": asset, "period": int(period)},
        ))
        # Step 2: enable chart-change push notifications
        await self._ws.send(_socket_io_event(
            "chart_notification/get",
            {"asset": asset, "version": "1.0.0"},
        ))
        # Step 3: ★ THIS is what actually starts the tick stream
        await self._ws.send(_socket_io_event(
            "depth/follow",
            asset,   # note: STRING payload, not an object
        ))
        self._subscribed.add(asset)
        return True

    async def stop_candles_stream(self, asset: str) -> None:
        """Unsubscribe from an asset's tick stream and clear all per-asset
        state (callbacks, tick buffer).

        FIX (DEEP-AUDIT-2026-07-26 / F-15-46): previously the _realtime.pop
        could race with _ingest_tick — a tick landing between the callbacks
        pop and the buffer pop would recreate the deque via defaultdict,
        and the next get_realtime_price would return a 1-tick (stale)
        result. Now: hold self._realtime_lock across the cleanup so ticks
        are blocked from being ingested during the pop window.
        """
        # Always clear callbacks — even if the WS is gone, feed.py may still
        # be holding a stream it needs to release.
        # FIX (F-15-46): hold the realtime lock across the callbacks+buffer
        # pop so _ingest_tick can't recreate the deque mid-cleanup.
        async with self._realtime_lock:
            self._tick_callbacks.pop(asset, None)
            if not self._ws or not self._ws_is_open():
                self._subscribed.discard(asset)
                self._realtime.pop(asset, None)
                return
            if asset not in self._subscribed:
                self._realtime.pop(asset, None)
                return
            # Send depth/unfollow OUTSIDE the lock — WS I/O can be slow and
            # we don't want to block tick ingestion for other assets.
            subscribed = asset in self._subscribed
            self._subscribed.discard(asset)
            self._realtime.pop(asset, None)
        if subscribed:
            try:
                await self._ws.send(_socket_io_event("depth/unfollow", asset))
            except Exception as exc:
                print(f"[quotex_ws] depth/unfollow error for {asset}: {exc}")
        # Clear the tick buffer so a re-subscribe doesn't replay stale ticks.
        # FIX (F-15-46): the buffer is now popped INSIDE the lock above, so
        # the old "minor race" comment is no longer applicable.

    # ── Realtime price polling (in-memory, no WS I/O) ─────────────────────

    async def get_realtime_price(self, asset: str) -> list[dict]:
        """Return a snapshot of the in-memory tick buffer for `asset`.

        Crucially, this does NO network I/O — the buffer is filled by the
        background reader loop as ticks arrive. feed.py polls this every
        ~50ms per stream (see _stream_loop) and filters to new ticks via
        stream.last_tick_ts.
        """
        buf = self._realtime.get(asset)
        if not buf:
            return []
        return list(buf)

    # ── History ───────────────────────────────────────────────────────────

    async def get_candles(self, asset: str,
                          end_from_time: int | None,
                          offset: int,
                          period: int) -> list[dict]:
        """Fetch historical candles via WebSocket (history/load event).

        end_from_time: anchor time (None = now, but Quotex wants an explicit
                       timestamp — we use the current period-floor).
        offset: total seconds of history to fetch (candles * period).
        period: candle period in seconds.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-04): previously a concurrent call
        to get_candles() for the SAME asset clobbered the first caller's
        pending future (`self._pending_history[asset] = fut` overwrote
        the prior slot). The first caller's `await asyncio.wait_for(fut,
        ...)` then never resolved and timed out after 15s with []. Now:
        serialize concurrent calls for the same asset via per-asset
        `asyncio.Lock` — the second caller waits for the first to complete
        and reuses its result by re-fetching (no cached result-sharing to
        avoid stale-data races if callers passed different `period` /
        `offset` arguments).
        """
        if not self._ws or not self._ws_is_open():
            return []

        # FIX (F-15-04): per-asset lock serializes concurrent calls.
        # _history_locks is a defaultdict — first access creates the lock.
        async with self._history_locks[asset]:
            return await self._get_candles_inner(
                asset, end_from_time, offset, period,
            )

    async def _get_candles_inner(self, asset: str,
                                 end_from_time: int | None,
                                 offset: int,
                                 period: int) -> list[dict]:
        """Inner worker for get_candles — assumes the per-asset lock is held."""
        if not self._ws or not self._ws_is_open():
            return []

        if end_from_time is None:
            end_from_time = int(time.time())
        # Number of candles = offset / period
        count = max(1, int(offset) // max(1, int(period)))

        # Register a future so the reader loop can resolve it when the
        # matching 'history/load' response arrives.
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_history[asset] = fut
        self._binary_history_buf.pop(asset, None)
        # FIX (F-15-07): clear this asset's slot in the binary-attachment
        # FIFO queue (any orphaned entries would mis-attribute future bytes).
        try:
            while self._binary_history_queue:
                # Only remove entries matching this asset — other entries
                # belong to other in-flight fetches (held by other callers
                # via their own per-asset locks).
                if self._binary_history_queue[-1] == asset:
                    self._binary_history_queue.pop()
                else:
                    break
        except Exception:
            pass

        try:
            await self._ws.send(_socket_io_event(
                "history/load",
                {"asset": asset,
                 "index": 0,
                 "time": int(end_from_time),
                 "offset": count,
                 "period": int(period)},
            ))
            # Wait for either the JSON response OR a binary attachment
            try:
                raw = await asyncio.wait_for(fut, timeout=15.0)
            except asyncio.TimeoutError:
                return []

            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-3-06): the JSON
            # header (451-["history/load",...]) resolves `fut` immediately,
            # but the binary attachment with the actual candle data follows
            # in a separate frame and may not have been processed yet.
            # Wait briefly (up to 2s) for the binary buffer to populate
            # before falling through to the (empty) JSON normalization path.
            # Without this, ~10-20% of stream starts get [] history and the
            # first candle has no prediction.
            # FIX (DEEP-AUDIT-2026-07-26 / F-15-47): kept the busy-poll as a
            # fallback. A cleaner implementation would use a dedicated
            # `_binary_history_futures[asset]` future resolved by
            # `_handle_binary` when bytes arrive — TODO. The busy-poll is
            # acceptable because it only runs when the JSON header arrives
            # before the binary (which is the common case), and the typical
            # wait is <100ms.
            binary_buf = self._binary_history_buf.get(asset)
            if not binary_buf:
                for _ in range(20):  # 2s
                    binary_buf = self._binary_history_buf.get(asset)
                    if binary_buf:
                        break
                    await asyncio.sleep(0.1)
            binary_buf = self._binary_history_buf.pop(asset, None)
            if binary_buf:
                parsed = _parse_binary_history(binary_buf)
                if parsed:
                    return parsed

            # Otherwise normalize the JSON response
            return self._normalize_history(raw, asset)
        finally:
            self._pending_history.pop(asset, None)
            self._binary_history_buf.pop(asset, None)
            # Clear the target so a future fetch starts clean.
            if getattr(self, "_binary_history_target", None) == asset:
                self._binary_history_target = None

    async def get_historical_candles(self, asset: str,
                                     amount_of_seconds: int,
                                     period: int,
                                     max_workers: int = 1) -> list[dict]:
        """Fetch historical candles (compat shim for pyquotex's API).

        amount_of_seconds: total seconds of history (e.g. 200 * 60 = 12000).
        period: candle period in seconds.
        max_workers: ignored — we fetch in one shot over WebSocket.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-48): max_workers is intentionally
        ignored — Quotex's WS history/load endpoint returns the full
        requested range in a single response, so parallelism at the WS
        layer provides no benefit. If callers pass max_workers > 1, we
        log a debug note but still fetch in one shot. For large
        amount_of_seconds values (>86400 = 1 day at 1-min period), the
        single fetch may take >15s and time out — callers should chunk
        the request themselves.
        """
        if max_workers and max_workers > 1:
            # Don't raise — silently document the limitation. (See FIX above.)
            print(f"[quotex_ws] get_historical_candles({asset!r}): "
                  f"max_workers={max_workers} ignored (WS fetch is single-shot)")
        return await self.get_candles(
            asset,
            end_from_time=int(time.time()),
            offset=amount_of_seconds,
            period=period,
        )

    @staticmethod
    def _normalize_history(raw, asset: str) -> list[dict]:
        """Normalize whatever shape Quotex returned into a sorted OHLC list.

        FIX (2026-07-13): the dict branch below was dead — the only caller
        (get_candles) receives `raw` from the 'history/load' event handler,
        which already extracts `args[0].get("data") or args[0].get("candles")
        or []` (always a list) before resolving the future. Removed.

        FIX (DEEP-AUDIT-2026-07-26 / F-15-49): previously defaulted `time`
        to 0 if both `time` and `from` keys were missing. A candle with
        time=0 sorts FIRST, corrupting the timeline, and dedup-by-time
        would collapse multiple time=0 candles into one. Now: SKIP any
        candle whose time can't be resolved.
        """
        if not raw:
            return []
        # raw is a list of dicts or a list of lists
        out: list[dict] = []
        for c in raw:
            try:
                if isinstance(c, dict):
                    # FIX (F-15-49): skip candles without a resolvable time.
                    t = c.get("time", c.get("from"))
                    if t is None:
                        continue
                    out.append({
                        "time":  int(t),
                        "open":  float(c.get("open", 0)),
                        "high":  float(c.get("high", 0)),
                        "low":   float(c.get("low", 0)),
                        "close": float(c.get("close", 0)),
                    })
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    # Some Quotex versions send [time, open, high, low, close]
                    # FIX (F-15-49): skip if time is None / falsy.
                    t = c[0]
                    if t is None or t == 0:
                        continue
                    out.append({
                        "time":  int(t),
                        "open":  float(c[1]),
                        "high":  float(c[2]),
                        "low":   float(c[3]),
                        "close": float(c[4]),
                    })
            except (TypeError, ValueError):
                continue
        # Deduplicate by time + sort
        seen: dict[int, dict] = {}
        for c in out:
            seen[c["time"]] = c
        return sorted(seen.values(), key=lambda x: x["time"])


# FIX (DEEP-AUDIT-2026-07-26 / F-15-DELETE-MAKE_CLIENT): `make_client()` had
# ZERO callers across the repo (per cross-file audit A-10). feed.py constructs
# QuotexWSClient directly via _make_client(). Deleted to reduce maintenance
# surface. If a future caller wants a factory, re-add with explicit kwargs.
