/**
 * client.ts — typed REST client (directive Phase 6: every component
 * consumes real backend data; no fake demo values).
 *
 * Design: one fetch wrapper + one function per real endpoint. Shapes in
 * protocol.ts mirror what server.py actually returns (verified against
 * the code, not invented). 401/403/429/5xx map to a typed ApiError so
 * every screen can show an honest error state instead of a spinner.
 */

import type {
  HealthzResponse, StatusResponse, TruthLayerResponse,
  DataQualityResponse, WinrateResponse, ModelsRow, Prediction, Candle,
  PairEntry,
} from "../websocket/protocol";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function get<T>(path: string, timeoutMs = 12_000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(path, { signal: ctrl.signal, headers: { accept: "application/json" } });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json() as { error?: string; detail?: string }).error
                    ?? (await res.json() as { detail?: string }).detail ?? ""; } catch { /* keep "" */ }
      throw new ApiError(res.status, detail || `HTTP ${res.status}`);
    }
    return await res.json() as T;
  } finally {
    clearTimeout(timer);
  }
}

// ── endpoints (kept 1:1 with the backend's real routes) ───────────────

export const api = {
  healthz:      () => get<HealthzResponse>("/healthz"),
  status:       () => get<StatusResponse>("/api/status"),
  truthLayer:   () => get<TruthLayerResponse>("/api/truth-layer"),
  dataQuality:  () => get<DataQualityResponse>("/api/data-quality"),
  timeSync:     () => get<Record<string, unknown>>("/api/time-sync"),
  settlementIntegrity: () => get<Record<string, unknown>>("/api/settlement-integrity"),
  streamingStatus: () => get<Record<string, unknown>>("/api/streaming-status"),

  pairs: (category?: "otc" | "real") =>
    get<{ pairs?: PairEntry[]; real_pairs?: PairEntry[];
           otc_pairs?: PairEntry[]; [k: string]: unknown }>(
      category ? `/api/pairs/${category}` : "/api/pairs"),

  /** history REST path (server.py /api/history/{asset}/{period}) */
  history: (asset: string, period: number) =>
    get<{ candles?: Candle[]; [k: string]: unknown }>(
      `/api/history/${encodeURIComponent(asset)}/${period}`),

  signalsLatest: () => get<Prediction[] | { signals?: Prediction[] } | Record<string, unknown>>(
    "/api/signals/latest"),
  signalsCount: () => get<Record<string, unknown>>("/api/signals/count"),

  winrate: (period = 60, days?: number) =>
    get<WinrateResponse>(`/api/winrate?period=${period}${days ? `&days=${days}` : ""}`),

  models: () => get<ModelsRow[] | { models?: ModelsRow[] } | Record<string, unknown>>(
    "/api/prediction/models"),
  predictionOverview: () => get<Record<string, unknown>>("/api/prediction/overview"),
  stats: () => get<Record<string, unknown>>("/api/stats"),

  authState: () => get<Record<string, unknown>>("/api/auth/state"),
};
