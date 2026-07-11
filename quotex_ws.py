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
import time
from collections import defaultdict, deque
from typing import Any, Iterable

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

WS_URL = os.environ.get(
    "QX_WS_URL",
    "wss://ws2.market-qx.trade/socket.io/?EIO=3&transport=websocket",
)
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
                 host: str = "market-qx.trade",
                 lang: str = "en",
                 root_path: str | None = None,
                 reconnect_policy=None,
                 **_unused):
        # Mirror pyquotex's constructor signature so feed._make_client
        # doesn't need to know which implementation it's getting.
        self.email = email
        self.password = password
        self.host = host
        self.lang = lang
        self.root_path = root_path

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

        # Pending history requests: keyed by an asyncio.Future
        # {asset: future} — set by get_candles(), resolved by the reader loop
        # when the matching "history/load" event arrives.
        self._pending_history: dict[str, asyncio.Future] = {}
        # Partial binary history buffers: {asset: bytes}
        self._binary_history_buf: dict[str, bytes] = {}

        # Subscribed assets (so stop_candles_stream only sends unfollow for
        # assets we actually subscribed)
        self._subscribed: set[str] = set()

        # Instruments cache (refreshed on connect)
        self._instruments: list = []

        # Payout cache: {asset: int}
        self._payouts: dict[str, int] = {}

        # Last error / reason for connect()'s return value
        self._last_reason: str = ""

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

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> tuple[bool, str]:
        """Open the WebSocket and authorize with the stored ssid."""
        if self._connected and self._authorized:
            return True, "already connected"

        if not self._ssid:
            return False, "no ssid — call set_session() first"

        self._closed_by_user = False
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    WS_URL,
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
            self._last_reason = f"ws connect failed: {exc}"
            return False, self._last_reason

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

        # Wait for either authorization/accept OR authorization/reject
        ok = await asyncio.wait_for(self._wait_for_auth(), timeout=15)
        if not ok:
            await self._cleanup()
            return False, "authorization rejected"

        self._connected = True
        self._authorized = True

        # Fetch instruments in the background — non-blocking, feed.py will
        # re-call get_instruments() explicitly when it needs them.
        asyncio.create_task(self._fetch_instruments())

        return True, "connected"

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
        reader loop, which sets _authorized_event)."""
        deadline = time.time() + 15
        while time.time() < deadline:
            if getattr(self, "_auth_result", None) is not None:
                return self._auth_result
            if self._ws is None or self._ws.closed:
                return False
            await asyncio.sleep(0.05)
        return False

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
            print("[quotex_ws] server closed the engine.io connection")

    async def _handle_event(self, arr: list) -> None:
        """Dispatch a Socket.IO event: ["event_name", *args]."""
        if not arr:
            return
        name = arr[0]
        args = arr[1:]

        if name in ("authorization/accept", "authorization/success"):
            self._auth_result = True
        elif name in ("authorization/reject", "authorization/error"):
            self._auth_result = False
            print(f"[quotex_ws] authorization rejected: {args}")
        elif name == "instruments/update":
            # Per-instrument update — usually a list with one entry
            if args and isinstance(args[0], list):
                for inst in args[0]:
                    self._merge_instrument(inst)
        elif name == "instruments/list":
            # Full instruments list
            if args and isinstance(args[0], list):
                self._instruments = args[0]
        elif name == "history/load":
            # JSON-formatted history response
            if args and isinstance(args[0], dict):
                asset = args[0].get("asset") or args[0].get("instrument")
                data = args[0].get("data") or args[0].get("candles") or []
                fut = self._pending_history.get(asset)
                if fut and not fut.done():
                    fut.set_result(data)
        elif name in ("timesync",):
            # Server time sync — could be used for NTP-like correction.
            pass
        elif name == "chart_notification/update":
            # Chart notification (asset went from open to closed, etc.) —
            # we don't currently act on it; feed.py refreshes pairs on a timer.
            pass
        # Tick data is delivered as a list of [asset, ts, price, direction]
        # tuples — sent under various event names depending on Quotex version.
        # We detect by shape: a list whose first element is a list of 4 items.
        if (args and isinstance(args[0], list)
                and args[0] and isinstance(args[0][0], (list, tuple))
                and len(args[0][0]) >= 3):
            for tick in args[0]:
                self._ingest_tick(tick)

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
        # Deduplicate by timestamp — the server sometimes resends the last
        # tick on reconnect, which would confuse feed.py's "new_ticks since
        # last_tick_ts" filter.
        buf = self._realtime[asset]
        if buf and buf[-1]["time"] == ts:
            return
        tick_dict = {"time": ts, "price": price}
        buf.append(tick_dict)
        # ── Event-driven fan-out ──────────────────────────────────────────
        # Feed.py registers a callback per (asset, period) stream that puts
        # the tick into an asyncio.Queue, which the stream loop awaits
        # directly — no 50ms polling delay. Async callbacks are scheduled on
        # the event loop (we're already in the reader task, so this is safe).
        for cb in self._tick_callbacks.get(asset, []):
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
        self._tick_callbacks[asset].append(callback)

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
        # If payload is bytes, it's a binary attachment — append to whichever
        # asset's history buffer is pending.
        if isinstance(payload, (bytes, bytearray)):
            # Find any pending asset — Quotex only sends one binary history
            # at a time, so this is safe in practice.
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
                    # The binary attachment will follow in the next frame;
                    # _handle_binary(bytes) above will collect it. We resolve
                    # the future lazily — see get_candles() which polls the
                    # buffer.
                    pass

    async def _ping_loop(self) -> None:
        """Send Socket.IO pings to keep the connection alive. The server's
        pingInterval is usually 25s; we ping every PING_INTERVAL seconds."""
        try:
            while self._ws and not self._ws.closed:
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and not self._ws.closed:
                    try:
                        await self._ws.send("2")
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    async def _cleanup(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._connected = False
        self._authorized = False

    async def close(self) -> None:
        self._closed_by_user = True
        await self._cleanup()

    # ── Instruments ───────────────────────────────────────────────────────

    async def _fetch_instruments(self) -> None:
        """Request the full instruments list. Response comes async via
        the 'instruments/list' event."""
        if not self._ws or self._ws.closed:
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
                return
        self._instruments.append(inst)

    async def get_instruments(self) -> list:
        """Return the cached instruments list, refreshing once if empty."""
        if not self._instruments:
            await self._fetch_instruments()
            # Wait briefly for the response
            for _ in range(20):
                if self._instruments:
                    break
                await asyncio.sleep(0.1)
        return list(self._instruments)

    def get_payout_by_asset(self, asset: str) -> int | None:
        """Return the cached 1-minute payout % for an asset, or None."""
        # Instruments may not have loaded yet — try to extract from cache
        for inst in self._instruments:
            if inst and len(inst) > 9 and inst[1] == asset:
                try:
                    return int(inst[-9])
                except (TypeError, ValueError, IndexError):
                    continue
        return self._payouts.get(asset)

    # ── Stream lifecycle (3-step subscribe per asset) ─────────────────────

    async def start_candles_stream(self, asset: str, period: int) -> None:
        """Subscribe to live ticks for one asset. Sends 3 frames per the
        quotex-smooth-candle-mystery.txt spec:

            42["instruments/update", {"asset":asset,"period":period}]
            42["chart_notification/get", {"asset":asset,"version":"1.0.0"}]
            42["depth/follow", asset]   ← depth/follow takes a STRING payload
        """
        if not self._ws or self._ws.closed:
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

    async def stop_candles_stream(self, asset: str) -> None:
        """Unsubscribe from an asset's tick stream and clear all per-asset
        state (callbacks, tick buffer)."""
        # Always clear callbacks — even if the WS is gone, feed.py may still
        # be holding a stream it needs to release.
        self._tick_callbacks.pop(asset, None)
        if not self._ws or self._ws.closed:
            self._subscribed.discard(asset)
            self._realtime.pop(asset, None)
            return
        if asset not in self._subscribed:
            self._realtime.pop(asset, None)
            return
        try:
            await self._ws.send(_socket_io_event("depth/unfollow", asset))
        except Exception as exc:
            print(f"[quotex_ws] depth/unfollow error for {asset}: {exc}")
        self._subscribed.discard(asset)
        # Clear the tick buffer so a re-subscribe doesn't replay stale ticks
        self._realtime.pop(asset, None)

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
        """
        if not self._ws or self._ws.closed:
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

            # If a binary buffer accumulated, prefer that (more granular)
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

    async def get_historical_candles(self, asset: str,
                                     amount_of_seconds: int,
                                     period: int,
                                     max_workers: int = 1) -> list[dict]:
        """Fetch historical candles (compat shim for pyquotex's API).

        amount_of_seconds: total seconds of history (e.g. 200 * 60 = 12000).
        period: candle period in seconds.
        max_workers: ignored — we fetch in one shot over WebSocket.
        """
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
        # raw may be a list of dicts, a dict with 'data', or a list of lists
        if isinstance(raw, dict):
            for key in ("candles", "data", "history"):
                if key in raw:
                    raw = raw[key]
                    break
            else:
                raw = list(raw.values())[0] if raw else []
        out: list[dict] = []
        for c in raw:
            try:
                if isinstance(c, dict):
                    out.append({
                        "time":  int(c.get("time", c.get("from", 0))),
                        "open":  float(c.get("open", 0)),
                        "high":  float(c.get("high", 0)),
                        "low":   float(c.get("low", 0)),
                        "close": float(c.get("close", 0)),
                    })
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    # Some Quotex versions send [time, open, high, low, close]
                    out.append({
                        "time":  int(c[0]),
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


# ── Factory ─────────────────────────────────────────────────────────────────

def make_client(email: str = "",
                password: str = "",
                host: str = "market-qx.trade",
                lang: str = "en",
                root_path: str | None = None,
                **kw) -> QuotexWSClient:
    """Convenience factory mirroring pyquotex's Quotex(...) constructor."""
    return QuotexWSClient(
        email=email,
        password=password,
        host=host,
        lang=lang,
        root_path=root_path,
        **kw,
    )
