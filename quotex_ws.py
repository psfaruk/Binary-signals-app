"""Quotex raw WebSocket client — Socket.IO v3 over plain WebSocket."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError as _e:  # pragma: no cover
    raise ImportError(
        "quotex_ws requires the 'websockets' package. "
        "pip install websockets") from _e


# ── Constants ───────────────────────────────────────────────────────────────

WS_URL = os.environ.get(
    "QX_WS_URL",
    "wss://ws2.market-qx.trade/socket.io/?EIO=3&transport=websocket",
)
DEFAULT_HOST = "market-qx.trade"


def _build_ws_url(host: str) -> str:
    """Build the WebSocket URL for the given host."""
    if not host:
        host = DEFAULT_HOST
    # Allow QX_WS_URL env override for local mock testing.
    env_override = os.environ.get("QX_WS_URL")
    if env_override and host == DEFAULT_HOST:
        return env_override
    return f"wss://ws2.{host}/socket.io/?EIO=3&transport=websocket"


IS_DEMO = int(os.environ.get("QX_IS_DEMO", "1"))
TOURNAMENT_ID = int(os.environ.get("QX_TOURNAMENT_ID", "0"))
# Tick buffer size per asset
TICK_BUFFER_MAX = 1000

PING_INTERVAL = 25.0
PING_TIMEOUT  = 60.0
CONNECT_TIMEOUT = 30.0
SUBSCRIBE_TIMEOUT = 10.0

_SESSION_FILE_LOCK = threading.Lock()


def _make_ws_is_open_check():
    try:
        class _Probe:
            close_code = None
        _ = _Probe().close_code
        def _v13_check(ws) -> bool:
            try:
                return ws.close_code is None
            except AttributeError:
                return False
        return _v13_check
    except Exception as _e:
        print(f"[silent-except] quotex_ws.py:136 {type(_e).__name__}: {_e}")
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
    """Parse an incoming Socket.IO v3 / Engine.IO v3 frame."""
    if isinstance(raw, bytes):
        return "binary", raw

    if not raw:
        return "unknown", None

    head = raw[0]
    body = raw[1:]

    if head == "0":
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
            except json.JSONDecodeError as _e:
                print(f"[silent-except] quotex_ws.py:229 {type(_e).__name__}: {_e}")
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
    """Parse Quotex's binary history payload into candle dicts."""
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
    """Raw WebSocket client speaking Socket.IO v3 to Quotex."""

    def __init__(self,
                 email: str = "",
                 password: str = "",
                 host: str = DEFAULT_HOST,
                 lang: str = "en",
                 root_path: str | None = None,
                 reconnect_policy=None,
                 **_unused):
        self.host = host or DEFAULT_HOST
        self._ws_url = _build_ws_url(self.host)
        if _unused:
            raise TypeError(
                f"QuotexWSClient got unexpected keyword arguments: "
                f"{sorted(_unused)}"
            )
        self._reconnect_policy = reconnect_policy

        # Session / auth state
        self._ssid: str | None = None
        self._cookies: str | None = None
        self._user_agent: str = ""
        self.session_data: dict = {}

        # Connection state
        self._ws = None
        self._connected = False
        self._authorized = False
        self._reader_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._closed_by_user = False
        self._auth_result: bool | None = None
        self._auth_fail_reason: str = ""

        # In-memory tick buffer: per-asset deque
        self._realtime: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=TICK_BUFFER_MAX))

        self._tick_callbacks: dict[str, list] = defaultdict(list)

        self._history_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Pending history requests keyed by asyncio.Future
        self._pending_history: dict[str, asyncio.Future] = {}
        self._binary_history_buf: dict[str, bytes] = {}
        self._binary_history_queue: deque[str] = deque()

        self._binary_tick_queue: deque = deque()

        # Subscribed assets (so stop only sends unfollow for subscribed)
        self._subscribed: set[str] = set()

        # Instruments cache (refreshed on connect)
        self._instruments: list = []

        # Payout cache: {asset: int}
        self._payouts: dict[str, int] = {}

        self._session_file_lock = _SESSION_FILE_LOCK

        # Server time offset (server ts - local ts)
        self._server_time_offset: float = 0.0

        # Per-asset open/closed state from chart_notification
        self._asset_open_state: dict[str, bool] = {}

        # Pong-tracking: timestamp of the last pong received
        self._last_pong: float = time.time()

        self._realtime_lock = asyncio.Lock()

    # ── Session / auth ────────────────────────────────────────────────────

    def set_session(self,
                    user_agent: str = "",
                    ssid: str | None = None,
                    cookies: str | None = None,
                    **_unused) -> None:
        """Store ssid + cookies so connect() can authorize on the WS."""
        self._consecutive_rejects = 0
        self._token_dead_at = 0
        self._user_agent = user_agent
        self._ssid = ssid
        self._cookies = cookies
        if ssid:
            self.session_data = {"token": ssid}

    # ── session.json support ─────────────────────────────────────────────

    @staticmethod
    def _session_json_path() -> str:
        """Locate session.json (env → Railway volume → MEIPASS → CWD)."""
        env = os.environ.get("QX_SESSION_PATH")
        if env:
            return env
        railway_vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if railway_vol:
            try:
                os.makedirs(railway_vol, exist_ok=True)
                return os.path.join(railway_vol, "session.json")
            except Exception as _e:
                print(f"[silent-except] quotex_ws.py:539 {type(_e).__name__}: {_e}")
                pass
        # PyInstaller: _MEIPASS is set to the bundle's extracted dir.
        meipass = getattr(__import__("sys"), "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, "session.json")
        if os.environ.get("RAILWAY_PUBLIC_DOMAIN") and not railway_vol:
            # Log once per process via a module-level flag.
            global _RAILWAY_NOPERSIST_WARNED
            if not globals().get("_RAILWAY_NOPERSIST_WARNED"):
                _RAILWAY_NOPERSIST_WARNED = True
                print("[quotex_ws] WARNING: running on Railway but "
                      "RAILWAY_VOLUME_MOUNT_PATH not set — session.json "
                      "will be lost on redeploy. Add a Railway volume "
                      "mounted to e.g. /data and set "
                      "RAILWAY_VOLUME_MOUNT_PATH=/data to persist tokens.")
        return os.path.join(os.getcwd(), "session.json")

    @staticmethod
    def load_session_json(email: str | None = None) -> dict | None:
        """Read session.json and return the account data for `email`."""
        path = QuotexWSClient._session_json_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                return None
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
        """Clear the token field in session.json so a stale token isn't reused."""
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
            except FileNotFoundError as _e:
                print(f"[silent-except] quotex_ws.py:622 {type(_e).__name__}: {_e}")
                pass
            except Exception as exc:
                print(f"[quotex_ws] could not clear session.json token: {exc}")

    @staticmethod
    def save_session_json(email: str, token: str, cookies: str,
                          user_agent: str) -> None:
        """Save/update a working token in session.json for restart reuse."""
        path = QuotexWSClient._session_json_path()
        with _SESSION_FILE_LOCK:
            try:
                data = {}
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError) as _e:
                    print(f"[silent-except] quotex_ws.py:646 {type(_e).__name__}: {_e}")
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
        """Persist only a token to session.json (used by /api/set-token)."""
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
        # Backoff check: if token is dead, wait before reconnecting.
        token_dead_at = getattr(self, "_token_dead_at", 0)
        if token_dead_at:
            elapsed = time.time() - token_dead_at
            backoff = 60.0
            if elapsed < backoff:
                wait_remaining = backoff - elapsed
                return False, f"token dead — backing off {wait_remaining:.0f}s (refresh via /api/set-token)"
        if self._connected and self._authorized and self._ws_is_open():
            return True, "already connected"

        if not self._ssid:
            return False, "no ssid — call set_session() first"

        self._closed_by_user = False
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self._ws_url,
                    additional_headers={
                        "User-Agent": self._user_agent or
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36",
                        "Origin": f"https://{self.host}",
                        "Cookie": self._cookies or "",
                    },
                    max_size=None,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                ),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:
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

        ok = await self._wait_for_auth()
        if not ok:
            await self._cleanup()
            _reason = self._auth_fail_reason or "rejected"
            if _reason == "timeout":
                return False, "authorization timeout (15s)"
            elif _reason == "ws_closed":
                return False, "authorization failed (ws closed)"
            return False, "authorization rejected"

        self._connected = True
        self._authorized = True

        _inst_task = asyncio.create_task(self._fetch_instruments())
        _inst_task.add_done_callback(self._on_bg_task_done)

        return True, "connected"

    @staticmethod
    def _on_bg_task_done(task: asyncio.Task) -> None:
        """Done-callback that surfaces exceptions from fire-and-forget tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            print(f"[quotex_ws] background task {task} raised: "
                  f"{type(exc).__name__}: {exc}")

    async def _wait_for_open(self) -> None:
        """Wait for the engine.io "0" open frame (sets _engine_open)."""
        deadline = time.time() + 10
        while time.time() < deadline:
            if getattr(self, "_engine_open", False):
                return
            await asyncio.sleep(0.05)
        raise asyncio.TimeoutError()

    async def _wait_for_auth(self) -> bool:
        """Wait for the authorization/accept event (sets _auth_result)."""
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
        # Explicit timeout
        self._auth_fail_reason = "timeout"
        return False

    def _ws_is_open(self) -> bool:
        """Check if the WebSocket connection is open (handles v10-v16+ APIs)."""
        if self._ws is None:
            return False
        return _WS_IS_OPEN_CHECK(self._ws)

    async def _reader_loop(self) -> None:
        """Background task: read frames, dispatch by type, push ticks.
        Dies when the socket closes."""
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
        except ConnectionClosed as _e:
            print(f"[silent-except] quotex_ws.py:904 {type(_e).__name__}: {_e}")
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
            except Exception as _e:
                print(f"[silent-except] quotex_ws.py:925 {type(_e).__name__}: {_e}")
                pass
        elif kind == "pong":
            self._last_pong = time.time()
        elif kind == "connect":
            # Socket.IO connect ack — nothing to do
            pass
        elif kind == "event":
            await self._handle_event(payload)
        elif kind == "binary":
            await self._handle_binary(payload, raw)
        elif kind == "error":
            print(f"[quotex_ws] socket.io error: {payload}")
        elif kind == "close":
            print("[quotex_ws] server closed the engine.io connection")
            self._connected = False
            self._authorized = False

    async def _handle_event(self, arr: list) -> None:
        """Dispatch a Socket.IO event: ["event_name", *args]."""
        if not arr:
            return
        name = arr[0]
        args = arr[1:]

        if name in ("authorization/accept", "authorization/success", "s_authorization"):
            self._auth_result = True
            self._authorized = True
            # Reset reject counter on successful auth.
            self._consecutive_rejects = 0
        elif name in ("authorization/reject", "authorization/error"):
            self._auth_result = False
            self._authorized = False
            self._consecutive_rejects = getattr(self, "_consecutive_rejects", 0) + 1
            print(f"[quotex_ws] ⚠️  authorization rejected ({self._consecutive_rejects}x): {args}")
            print(f"[quotex_ws]    Token has expired or is invalid.")
            if self._consecutive_rejects >= 3:
                print(f"[quotex_ws]    ⛔ Token marked DEAD after {self._consecutive_rejects} "
                      f"consecutive rejects — backing off 60s before retry.")
                print(f"[quotex_ws]    → Action required: refresh the Quotex SSID and")
                print(f"[quotex_ws]      set it via /api/set-token or Railway Variables.")
                self._token_dead_at = time.time()
                cb = getattr(self, "token_dead_callback", None)
                if callable(cb):
                    try:
                        cb()
                    except Exception as _e:
                        print(f"[silent-except] quotex_ws.py:1002 {type(_e).__name__}: {_e}")
                        pass
            else:
                print(f"[quotex_ws]    App will auto-relogin on next retry cycle.")
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
            try:
                if args and isinstance(args[0], dict):
                    server_ts = float(args[0].get("time", 0))
                    if server_ts > 0:
                        self._server_time_offset = server_ts - time.time()
            except (TypeError, ValueError) as _e:
                print(f"[silent-except] quotex_ws.py:1053 {type(_e).__name__}: {_e}")
                pass
        elif name == "chart_notification/update":
            try:
                if args and isinstance(args[0], dict):
                    body = args[0]
                    asset = body.get("asset") or body.get("instrument")
                    if asset:
                        data = body.get("data") or {}
                        if isinstance(data, dict) and "isOpened" in data:
                            self._asset_open_state[asset] = bool(data["isOpened"])
                        elif "is_open" in body:
                            self._asset_open_state[asset] = bool(body["is_open"])
            except (TypeError, ValueError) as _e:
                print(f"[silent-except] quotex_ws.py:1073 {type(_e).__name__}: {_e}")
                pass
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
        buf = self._realtime[asset]
        for _t in list(buf)[-5:]:
            if _t["time"] == ts and _t["price"] == price:
                return
        tick_dict = {"time": ts, "price": price}
        buf.append(tick_dict)
        for cb in list(self._tick_callbacks.get(asset, [])):
            try:
                result = cb(tick_dict)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:
                print(f"[quotex_ws] tick callback error for {asset}: {exc}")

    # ── Event-driven callback registration ────────────────────────────────

    def register_tick_callback(self, asset: str, callback) -> None:
        """Register a sync/async callback fired on each new tick for `asset`."""
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
        """Handle a binary-event header or a binary attachment."""
        if isinstance(payload, (bytes, bytearray)):
            if self._binary_tick_queue:
                tick_meta = self._binary_tick_queue.popleft()
                try:
                    raw_bytes = bytes(payload)
                    # Skip the leading 0x04 marker byte if present.
                    if raw_bytes and raw_bytes[0] == 0x04:
                        json_bytes = raw_bytes[1:]
                    else:
                        json_bytes = raw_bytes
                    import json as _json
                    decoded = _json.loads(json_bytes.decode("utf-8", errors="replace"))
                    if isinstance(decoded, list):
                        if decoded and isinstance(decoded[0], (list, tuple)):
                            # Multi-tick frame
                            for tick in decoded:
                                self._ingest_tick(tick)
                        elif len(decoded) >= 3 and isinstance(decoded[0], str):
                            # Single-tick frame
                            self._ingest_tick(decoded)
                    return
                except Exception as exc:
                    if not getattr(self, "_suppress_tick_decode_warnings", False):
                        print(f"[quotex_ws] tick decode failed: {exc}")
                    return

            # Otherwise, it's a history/load binary attachment.
            target = None
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
                for asset, fut in list(self._pending_history.items()):
                    if not fut.done():
                        self._binary_history_buf[asset] = (
                            self._binary_history_buf.get(asset, b"") + bytes(payload))
            return

        # payload is the parsed JSON head: [event_name, body, ...]
        if isinstance(payload, list) and payload:
            event = payload[0]
            if event in ("quotes/stream", "depth/change", "quotes/update",
                         "candleStream", "candle/update", "stream/update"):
                # Live tick stream — the binary attachment will follow.
                # Parse asset from the header body if available.
                body = payload[1] if len(payload) >= 2 else {}
                asset = None
                if isinstance(body, dict):
                    asset = body.get("asset") or body.get("instrument")
                elif isinstance(body, str):
                    asset = body
                self._binary_tick_queue.append({
                    "event": event,
                    "asset": asset,
                })
                return
            if event == "history/load" and len(payload) >= 2:
                body = payload[1] or {}
                asset = body.get("asset") or body.get("instrument")
                if asset and asset in self._pending_history:
                    self._binary_history_queue.append(asset)
                    self._binary_history_target = asset
                    fut = self._pending_history[asset]
                    if not fut.done():
                        fut.set_result([])

    async def _ping_loop(self) -> None:
        """Send Socket.IO pings and periodically refresh the session token."""
        last_reauth = time.time()
        REAUTH_INTERVAL = 300
        self._last_pong = time.time()

        try:
            while self._ws and self._ws_is_open():
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and self._ws_is_open():
                    if time.time() - self._last_pong > PING_INTERVAL * 2.5:
                        print(f"[quotex_ws] no pong in {PING_INTERVAL * 2.5:.0f}s — "
                              f"connection presumed dead, breaking ping loop")
                        try:
                            if self._ws is not None:
                                await self._ws.close()
                        except Exception as _e:
                            print(f"[silent-except] quotex_ws.py:1399 {type(_e).__name__}: {_e}")
                            pass
                        break
                    try:
                        # Engine.IO ping
                        await self._ws.send("2")
                    except Exception:
                        break

                    # Periodic re-authorization to refresh session
                    now = time.time()
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
                        except Exception as _e:
                            print(f"[silent-except] quotex_ws.py:1425 {type(_e).__name__}: {_e}")
                            pass
        except asyncio.CancelledError as _e:
            print(f"[silent-except] quotex_ws.py:1427 {type(_e).__name__}: {_e}")
            pass

    async def _cleanup(self) -> None:
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
            except Exception as _e:
                print(f"[silent-except] quotex_ws.py:1447 {type(_e).__name__}: {_e}")
                pass
        if tasks_to_await:
            try:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)
            except Exception as _e:
                print(f"[silent-except] quotex_ws.py:1452 {type(_e).__name__}: {_e}")
                pass
        self._ws = None
        self._connected = False
        self._authorized = False

    async def close(self) -> None:
        self._closed_by_user = True
        await self._cleanup()
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
        """Update or append a single instrument in self._instruments."""
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
        """Cache the 1M payout for this instrument into self._payouts."""
        if not inst or len(inst) <= 9:
            return
        try:
            name = inst[1]
            payout = int(inst[-9])
            if name and payout >= 0:
                self._payouts[name] = payout
        except (TypeError, ValueError, IndexError) as _e:
            print(f"[silent-except] quotex_ws.py:1510 {type(_e).__name__}: {_e}")
            pass

    async def get_instruments(self) -> list:
        """Return cached instruments, refreshing once if empty (retries 3x)."""
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
        """Return the cached 1-minute payout % for an asset, or None."""
        if not asset:
            return None
        # Build candidate asset names: literal + _otc variant
        candidates: list[str] = [asset]
        if asset.endswith("_otc"):
            candidates.append(asset[:-4])
        else:
            candidates.append(f"{asset}_otc")

        # First: try the payout cache (populated by _cache_payout).
        for cand in candidates:
            if cand in self._payouts:
                return self._payouts[cand]

        for inst in self._instruments:
            if not inst or len(inst) <= 9:
                continue
            if inst[1] not in candidates:
                continue
            try:
                payout = int(inst[len(inst) - 9])
                if payout >= 0:
                    return payout
            except (TypeError, ValueError, IndexError):
                continue
        return None

    # ── Stream lifecycle (3-step subscribe per asset) ─────────────────────

    async def start_candles_stream(self, asset: str, period: int) -> bool:
        """Subscribe to live ticks for one asset (3-frame subscribe)."""
        if not self._ws or not self._ws_is_open():
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
        """Unsubscribe from an asset's tick stream and clear per-asset state."""
        async with self._realtime_lock:
            self._tick_callbacks.pop(asset, None)
            if not self._ws or not self._ws_is_open():
                self._subscribed.discard(asset)
                self._realtime.pop(asset, None)
                return
            if asset not in self._subscribed:
                self._realtime.pop(asset, None)
                return
            subscribed = asset in self._subscribed
            self._subscribed.discard(asset)
            self._realtime.pop(asset, None)
        if subscribed:
            try:
                await self._ws.send(_socket_io_event("depth/unfollow", asset))
            except Exception as exc:
                print(f"[quotex_ws] depth/unfollow error for {asset}: {exc}")

    # ── Realtime price polling (in-memory, no WS I/O) ─────────────────────

    async def get_realtime_price(self, asset: str) -> list[dict]:
        """Return a snapshot of the in-memory tick buffer for `asset`."""
        async with self._realtime_lock:
            buf = self._realtime.get(asset)
            if not buf:
                return []
            return list(buf)

    # ── History ───────────────────────────────────────────────────────────

    async def get_candles(self, asset: str,
                          end_from_time: int | None,
                          offset: int,
                          period: int) -> list[dict]:
        """Fetch historical candles via WebSocket (history/load event)."""
        if not self._ws or not self._ws_is_open():
            return []

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

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_history[asset] = fut
        self._binary_history_buf.pop(asset, None)
        try:
            while self._binary_history_queue:
                if self._binary_history_queue[-1] == asset:
                    self._binary_history_queue.pop()
                else:
                    break
        except Exception as _e:
            print(f"[silent-except] quotex_ws.py:1758 {type(_e).__name__}: {_e}")
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
        """Fetch historical candles (compat shim for pyquotex's API)."""
        if max_workers and max_workers > 1:
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
        """Normalize whatever shape Quotex returned into a sorted OHLC list."""
        if not raw:
            return []
        # raw is a list of dicts or a list of lists
        out: list[dict] = []
        for c in raw:
            try:
                if isinstance(c, dict):
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
