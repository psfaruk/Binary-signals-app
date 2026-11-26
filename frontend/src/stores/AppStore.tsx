/**
 * AppStore — centralized application state (directive Phase 4:
 * "Centralized application state"). React Context + useReducer; no
 * external state library (dependency discipline).
 *
 * The store owns: connection state, market/pair/period selection, pair
 * lists, the ACTIVE stream's candles, the latest prediction frame,
 * truth-layer snapshot, and persisted settings. WS frames are normalized
 * here ONCE — pages/components read derived data, never raw frames.
 */

import { createContext, useContext, useEffect, useMemo, useReducer, type ReactNode } from "react";
import { WsManager, type ConnState } from "../services/websocket/manager";
import type { ServerFrame, Prediction, Candle, PairEntry } from "../services/websocket/protocol";
import { api } from "../services/api/client";

export type Market = "otc" | "real";

export interface Settings {
  theme: "dark" | "light";
  sound: boolean;
  refreshMs: number;            // REST fallback poll when WS down
  showCountdown: boolean;
}

interface State {
  conn: ConnState;
  wsStale: boolean;
  market: Market;
  asset: string;
  period: number;
  pairsReal: PairEntry[];
  pairsOtc: PairEntry[];
  candles: Candle[];
  lastPrice: number | null;
  candleOpenAt: number | null;
  prediction: Prediction | null;
  truth: { actionable_signals_possible?: boolean; [k: string]: unknown } | null;
  health: Record<string, unknown> | null;
  signalsToday: number | null;
  settings: Settings;
  error: string | null;
}

type Action =
  | { t: "conn"; conn: ConnState }
  | { t: "frame"; frame: ServerFrame }
  | { t: "pairs"; real: PairEntry[]; otc: PairEntry[] }
  | { t: "select"; market?: Market; asset?: string; period?: number }
  | { t: "candles"; candles: Candle[] }
  | { t: "prediction"; p: Prediction | null }
  | { t: "truth"; truth: State["truth"] }
  | { t: "health"; health: Record<string, unknown> | null }
  | { t: "signalsToday"; n: number | null }
  | { t: "settings"; s: Partial<Settings> }
  | { t: "error"; message: string | null };

const DEFAULT_ASSET = "EURUSD_otc";

function loadSettings(): Settings {
  const saved = typeof localStorage !== "undefined"
    ? localStorage.getItem("bsx.settings") : null;
  const base: Settings = {
    theme: "dark", sound: false, refreshMs: 10_000, showCountdown: true,
  };
  if (!saved) return base;
  try { return { ...base, ...(JSON.parse(saved) as Partial<Settings>) }; }
  catch { return base; }
}

function initialState(): State {
  return {
    conn: "idle",
    wsStale: false,
    market: "otc",
    asset: DEFAULT_ASSET,
    period: 60,
    pairsReal: [],
    pairsOtc: [],
    candles: [],
    lastPrice: null,
    candleOpenAt: null,
    prediction: null,
    truth: null,
    health: null,
    signalsToday: null,
    settings: loadSettings(),
    error: null,
  };
}

function reducer(state: State, a: Action): State {
  switch (a.t) {
    case "conn": {
      const stale = a.conn !== "open";
      return stale ? { ...state, conn: a.conn, wsStale: stale } : { ...state, conn: a.conn };
    }
    case "frame":
      return applyFrame(state, a.frame);
    case "pairs":
      return { ...state, pairsReal: a.real, pairsOtc: a.otc };
    case "select": {
      const market = a.market ?? state.market;
      return {
        ...state,
        market,
        asset: a.asset ?? state.asset,
        period: a.period ?? state.period,
        // selection change → next frames rebuild chart data
        candles: [],
        prediction: null,
      };
    }
    case "candles":
      return { ...state, candles: a.candles };
    case "prediction":
      return { ...state, prediction: a.p };
    case "truth":
      return { ...state, truth: a.truth };
    case "health":
      return { ...state, health: a.health };
    case "signalsToday":
      return { ...state, signalsToday: a.n };
    case "settings":
      return { ...state, settings: { ...state.settings, ...a.s } };
    case "error":
      return { ...state, error: a.message };
  }
}

function applyFrame(state: State, f: ServerFrame): State {
  const type = f.type as string;
  const asset = (f as { asset?: string }).asset;
  const period = (f as { period?: number }).period;

  // Only frames for the ACTIVE subscription mutate the chart state.
  const forActive = asset === state.asset && period === state.period;

  if (type === "snapshot" && forActive) {
    const candles = (f as { candles?: Candle[] }).candles ?? [];
    const pred = (f as { prediction?: Prediction | null }).prediction ?? null;
    return {
      ...state, candles,
      prediction: pred,
      candleOpenAt: nextCandleOpen(candles, state.period),
      lastPrice: candles.length ? candles[candles.length - 1]?.close ?? null : null,
    };
  }
  if (type === "tick" && forActive) {
    const running = (f as { candle?: Candle }).candle;
    const pred = (f as { prediction?: Prediction | null }).prediction ?? null;
    if (!running) return state;
    const merged = mergeRunning(state.candles, running);
    return {
      ...state,
      candles: merged,
      lastPrice: running.close,
      candleOpenAt: running.time,
      prediction: pred ?? state.prediction,
    };
  }
  if (type === "eoc" && forActive) {
    // candle closed: rebuild from the frame's authoritative slice
    const candles = (f as { candles?: Candle[] }).candles ?? [];
    return { ...state, candles,
             candleOpenAt: nextCandleOpen(candles, state.period) };
  }
  if (type === "pairs") {
    // WIRE PARITY (React error #31 lesson, 2026-11-27): entries are
    // OBJECTS {asset, display, status, payout, locked} — never plain
    // strings. Normalize once here; every consumer gets PairEntry[].
    const norm = (xs: unknown): PairEntry[] =>
      Array.isArray(xs) ? xs.filter((x) =>
        typeof x === "object" && x !== null && typeof (x as PairEntry).asset === "string") as PairEntry[] : [];
    const real = norm((f as { real_pairs?: unknown }).real_pairs);
    const otc = norm((f as { otc_pairs?: unknown }).otc_pairs);
    return { ...state, pairsReal: real.length ? real : state.pairsReal,
             pairsOtc: otc.length ? otc : state.pairsOtc };
  }
  if (type === "stale") {
    return { ...state, wsStale: true };
  }
  if (type === "market_closed" && forActive) {
    return { ...state, error: "market_closed" };
  }
  if (type === "error") {
    return state;   // surfaced via console only — panels stay honest
  }
  return state;
}

function mergeRunning(closed: Candle[], running: Candle): Candle[] {
  if (!closed.length) return [running];
  const last = closed[closed.length - 1]!;
  if (last.time === running.time) return [...closed.slice(0, -1), running];
  if (running.time > last.time) return [...closed, running];
  return closed;
}

function nextCandleOpen(candles: Candle[], period: number): number | null {
  const last = candles.length ? candles[candles.length - 1] : null;
  return last ? last.time + period : null;
}

// ── context ────────────────────────────────────────────────────────────

export interface AppApi {
  state: State;
  ws: WsManager;
  select(market: Market, asset: string, period?: number): void;
  saveSettings(s: Partial<Settings>): void;
  refreshOverview(): void;
}

const Ctx = createContext<AppApi | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, undefined, initialState);
  const ws = useMemo(() => new WsManager(), []);

  // bind manager → store
  useEffect(() => {
    ws.bind({
      onState: (conn) => dispatch({ t: "conn", conn }),
      onFrame: (frame) => dispatch({ t: "frame", frame }),
    });
    return () => ws.dispose();          // safe cleanup on unmount/navigation
  }, [ws]);

  // initial REST snapshot + subscribe to the default stream
  useEffect(() => {
    ws.connect();
    ws.subscribe({ type: "subscribe", asset: state.asset, period: state.period, category: "otc" });
    ws.send({ type: "pairs" });
    void refreshOverview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // persist settings
  useEffect(() => {
    try { localStorage.setItem("bsx.settings", JSON.stringify(state.settings)); } catch { /* private mode */ }
    document.documentElement.dataset.theme = state.settings.theme;
  }, [state.settings]);

  // REST fallback poller when the socket is not open (Vercel mode)
  useEffect(() => {
    if (state.conn === "open") return;
    const id = setInterval(async () => {
      try {
        const h = await api.status();
        dispatch({ t: "health", health: h as Record<string, unknown> });
      } catch { /* keep last known state */ }
    }, state.settings.refreshMs);
    return () => clearInterval(id);
  }, [state.conn, state.settings.refreshMs]);

  // stale clock: mark wsStale=true when no frames for 90 s (contract)
  useEffect(() => {
    const id = setInterval(() => {
      if (ws.getState() === "open" && ws.isStale()) dispatch({ t: "error", message: "DATA_STALE" });
    }, 15_000);
    return () => clearInterval(id);
  }, [ws]);

  const select = (market: Market, asset: string, period?: number) => {
    const p = period ?? state.period;
    ws.unsubscribe(state.asset, state.period);
    dispatch({ t: "select", market, asset, period: p });
    ws.subscribe({ type: "subscribe", asset, period: p, category: market });
  };

  const saveSettings = (s: Partial<Settings>) => dispatch({ t: "settings", s });

  const refreshOverview = async () => {
    try {
      const [truth, health, count] = await Promise.all([
        api.truthLayer(), api.healthz(), api.signalsCount().catch(() => null),
      ]);
      dispatch({ t: "truth", truth: truth as State["truth"] });
      dispatch({ t: "health", health: health as Record<string, unknown> });
      const n = (count as { today?: number } | null)?.today ?? null;
      dispatch({ t: "signalsToday", n });
    } catch {
      dispatch({ t: "error", message: "overview unavailable" });
    }
  };

  const value: AppApi = { state, ws, select, saveSettings, refreshOverview };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp outside AppProvider");
  return ctx;
}
