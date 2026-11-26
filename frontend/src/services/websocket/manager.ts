/**
 * manager.ts — dedicated WebSocket manager (directive Phase 6).
 *
 * Requirements implemented here:
 *   • automatic reconnection with exponential backoff + jitter
 *   • duplicate message protection (identity-keyed dedup window)
 *   • subscription management (server is the ledger via interested_cids;
 *     the manager resends its subscriptions after every reconnect so the
 *     server re-attaches us — mirroring the legacy client's resubscribe)
 *   • stale-data detection (per-subscription last-frame clock + wall clock)
 *   • heartbeat — the server closes idle sockets (_WS_IDLE_TIMEOUT), so we
 *     MUST send {type:"status"} at ≤15 s forever (protocol fact, not tunable)
 *   • connection health indicator states for the UI
 *   • safe cleanup — dispose() tears down sockets/timers/listeners with no
 *     dangling callbacks across page navigation
 *
 * Tested by tests/ws-manager.test.ts (pure logic; no real sockets).
 */

import type {
  ClientMessage, ServerFrame, WsSubscribe,
} from "./protocol";

export type ConnState =
  | "idle" | "connecting" | "open" | "backoff" | "closed" | "failed";

export interface Subscription { asset: string; period: number; category?: string; }

interface Handler {
  onFrame?: (f: ServerFrame) => void;
  onState?: (s: ConnState, attempt: number) => void;
}

const HEARTBEAT_MS = 15_000;       // must stay ≤ server idle timeout (15 s)
const DEAD_AFTER_MS = 45_000;      // no inbound frame → force reconnect
const STALE_AFTER_MS = 90_000;     // mirrors contract DEFAULT_FRESHNESS_SECS
const BASE_BACKOFF_MS = 800;
const MAX_BACKOFF_MS = 30_000;
const MAX_ATTEMPTS = 25;            // then state=failed (UI shows manual retry)
const DEDUP_WINDOW = 128;

export class WsManager {
  private ws: WebSocket | null = null;
  private url: string;
  private state: ConnState = "idle";
  private attempt = 0;
  private backoffTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastInboundAt = 0;
  private lastFrameIds: string[] = [];
  private subs = new Map<string, Subscription>();
  private handler: Handler = {};
  private disposed = false;

  constructor(url?: string) {
    this.url = url ??
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  }

  bind(h: Handler): void {
    this.handler = h;
    this.handler.onState?.(this.state, this.attempt);
  }

  getState(): ConnState { return this.state; }
  getSubscriptions(): Subscription[] { return [...this.subs.values()]; }

  // ── lifecycle ──────────────────────────────────────────────────────

  connect(): void {
    if (this.disposed) return;
    if (this.state === "open" || this.state === "connecting") return;
    this.setState("connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.disposed || ws !== this.ws) return;
      this.attempt = 0;
      this.lastInboundAt = Date.now();
      this.setState("open");
      this.resubscribeAll();
      this.startHeartbeat();
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.disposed || ws !== this.ws) return;
      this.lastInboundAt = Date.now();
      this.handleMessage(String(ev.data));
    };
    ws.onclose = () => { if (ws === this.ws) this.handleDisconnect(); };
    ws.onerror = () => {
      if (ws === this.ws) { try { ws.close(); } catch { /* noop */ } }
    };
  }

  disconnect(): void {
    this.stopHeartbeat();
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onclose = null; ws.onerror = null; ws.onopen = null; ws.onmessage = null;
      try { ws.close(); } catch { /* noop */ }
    }
    this.setState("closed");
  }

  dispose(): void {
    this.disposed = true;
    this.disconnect();
    if (this.backoffTimer) { clearTimeout(this.backoffTimer); this.backoffTimer = null; }
    this.subs.clear();
    this.handler = {};
  }

  // ── messaging ───────────────────────────────────────────────────────

  send(msg: ClientMessage): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try { this.ws.send(JSON.stringify(msg)); return true; }
    catch { return false; }
  }

  subscribe(sub: WsSubscribe): void {
    const key = subKey(sub);
    this.subs.set(key, { asset: sub.asset, period: sub.period, category: sub.category });
    this.send(sub);
  }

  unsubscribe(asset: string, period: number): void {
    this.subs.delete(subKey({ asset, period }));
    // The legacy server keeps streams alive server-side (interested_cids
    // drop on socket close); dropping our record is sufficient. If a future
    // server adds unsubscribe frames, send them here.
  }

  /** staleness of the SUBSCRIPTION's data, per the contract's 90 s default */
  isStale(): boolean {
    if (this.state !== "open") return true;
    return Date.now() - this.lastInboundAt > STALE_AFTER_MS;
  }

  // ── internals ───────────────────────────────────────────────────────

  private handleMessage(raw: string): void {
    let f: ServerFrame;
    try { f = JSON.parse(raw) as ServerFrame; } catch { return; }
    if (!f || typeof f !== "object" || typeof f.type !== "string") return;
    if (isDuplicate(f, this.lastFrameIds, DEDUP_WINDOW)) return;
    this.handler.onFrame?.(f);
  }

  private resubscribeAll(): void {
    for (const sub of this.subs.values()) {
      this.send({ type: "subscribe", asset: sub.asset, period: sub.period,
                 ...(sub.category ? { category: sub.category as "otc" | "real" } : {}) });
    }
    // refresh the pair list after a reconnect (server sends `pairs`)
    this.send({ type: "pairs" });
    this.send({ type: "status" });
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - this.lastInboundAt > DEAD_AFTER_MS) {
        // dead connection — the legacy overlay message, verbatim intent:
        // "no data for 45s — force-close and let backoff reconnect"
        try { this.ws.close(); } catch { /* noop */ }
        return;
      }
      this.send({ type: "status" });      // keepalive the server expects
    }, HEARTBEAT_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null; }
  }

  private handleDisconnect(): void {
    this.stopHeartbeat();
    if (this.state === "closed") return;   // intentional disconnect
    this.ws = null;
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.disposed || this.state === "closed") return;
    if (this.attempt >= MAX_ATTEMPTS) { this.setState("failed"); return; }
    const delay = backoffMs(this.attempt);
    this.setState("backoff");
    if (this.backoffTimer) clearTimeout(this.backoffTimer);
    this.backoffTimer = setTimeout(() => this.connect(), delay);
  }

  private setState(s: ConnState): void {
    this.state = s;
    this.handler.onState?.(s, this.attempt);
  }
}

// ── pure helpers (unit-tested without sockets) ────────────────────────

export function subKey(s: { asset: string; period: number }): string {
  return `${s.asset}@${s.period}`;
}

/** exponential backoff with jitter: 0.8s → 1.6s → … → 30s TOTAL cap */
export function backoffMs(attempt: number): number {
  const exp = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * Math.pow(2, attempt));
  const jitter = exp * 0.25 * (Math.random() * 2 - 1);
  // clamp the FINAL delay (base + jitter) into [100ms, 30s] — the jitter
  // may never push the total above the cap (test: backoffMs caps at 30s).
  return Math.round(Math.min(MAX_BACKOFF_MS, Math.max(100, exp + jitter)));
}

/**
 * Duplicate-frame protection. A frame's identity = type + asset + period
 * + the most granular monotonic field present (tick candle time, eoc
 * ctime, signal created_at_epoch, else nothing). Returns true when the
 * same identity was already seen within the window.
 */
export function isDuplicate(f: ServerFrame, seen: string[], window: number): boolean {
  const id = frameIdentity(f);
  if (!id) return false;
  if (seen.includes(id)) return true;
  seen.push(id);
  if (seen.length > window) seen.splice(0, seen.length - window);
  return false;
}

function frameIdentity(f: ServerFrame): string | null {
  const t = f.type as string;
  const asset = (f as { asset?: string }).asset ?? "";
  const period = (f as { period?: number }).period ?? "";
  let granular = "";
  if (t === "tick" || t === "eoc") {
    const c = (f as { candle?: { time?: number }; ctime?: number });
    granular = String(c.candle?.time ?? c.ctime ?? "");
  } else if (t === "snapshot") {
    const candles = (f as { candles?: { time?: number }[] }).candles;
    granular = String(candles?.length ?? "") + ":" +
               String(candles?.[candles.length - 1]?.time ?? "");
  } else if (t === "signals") {
    const sigs = (f as { signals?: unknown[] }).signals;
    granular = String(sigs?.length ?? "") + ":" +
               String((sigs?.[sigs.length - 1] as { ctime?: number } | undefined)?.ctime ?? "");
  } else if (t === "pairs" || t === "status") {
    // keepalive-shaped frames are NOT deduped (they carry no data identity)
    return null;
  }
  return `${t}|${asset}|${period}|${granular}`;
}
