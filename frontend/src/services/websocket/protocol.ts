/**
 * protocol.ts — the WS wire protocol + signal contract, typed.
 *
 * MIRROR OF THE EXISTING BACKEND CONTRACT (server.py ws_endpoint,
 * feed.py broadcasts, core/signal_contract.py fields). The backend is
 * NOT changed in this redesign — this file must stay byte-compatible
 * with what the server actually sends. Verified against:
 *   server.py:4589 @app.websocket("/ws")   (client→server messages)
 *   feed.py snapshot/tick/eoc/stale/market_closed broadcasts
 *   core/signal_contract.py build_contract_fields (canonical fields)
 *   feed.py _apply_truth_gate (research_only / NO_SIGNAL semantics)
 */

// ── client → server ────────────────────────────────────────────────────

export interface WsSubscribe {
  type: "subscribe";
  asset: string;
  period: number;            // allowed: 5 15 30 60 120 300 (server set)
  category?: "otc" | "real";
}
export interface WsPairsRequest { type: "pairs"; }
export interface WsStatusRequest { type: "status"; }   // doubles as keepalive
export interface WsSignalsRequest {
  type: "signals";
  asset: string;              // "ALL" = every allowlisted pair
  period: number;
  limit?: number;             // 1..200 (default 100)
  before_ctime?: number | null;
}
export type ClientMessage =
  | WsSubscribe | WsPairsRequest | WsStatusRequest | WsSignalsRequest;

// ── server → client ────────────────────────────────────────────────────

export interface Candle { time: number; open: number; high: number; low: number; close: number; }

/** Full truth-layer prediction payload as broadcast inside tick/eoc frames
 *  and returned by /api/signals/latest rows. Every field optional at the
 *  wire level — the backend fills them; the UI renders "unavailable"
 *  whenever null, never a fabricated value. */
export interface Prediction {
  signal: SignalDirection;
  confidence: number | null;
  strength: string | null;
  strategy?: string | null;
  score?: number | null;
  reasons?: string[] | null;
  // truth-layer contract fields (core/signal_contract.py)
  signal_id?: string;
  trace_id?: string;
  symbol?: string;
  truth_status?: "eligible" | "rejected" | "stale" | "error";
  reason_code?: ReasonCode | null;
  eligibility_policy_version?: string;
  contract_schema_version?: string;
  created_at_utc?: string;
  created_at_epoch?: number;
  source_candle_close_time_utc?: string | null;
  source_candle_close_epoch?: number | null;
  target_candle_open_time_utc?: string | null;
  target_candle_open_epoch?: number | null;
  target_candle_close_time_utc?: string | null;
  model_id?: string;
  model_version?: string;
  model_artifact_hash?: string;
  feature_schema_version?: string;
  data_source_id?: string;
  data_source_authorization?: string;
  data_freshness_status?: "fresh" | "stale" | "unknown";
  validation_status?: "validated" | "unvalidated";
  // research-only diagnostic preference (never actionable)
  research_only?: boolean;
  research_direction?: SignalDirection | null;
  research_confidence?: number | null;
  research_strategy?: string | null;
  research_reason?: string | null;
  // graded outcome fields (history rows)
  actual?: string | null;
  correct?: number | null;       // 1/0 when graded
  ctime?: number;               // candle open time (s) — history key
  category?: string;
  regime?: string;
  ml?: number | null;
  // anything the engines attach beyond the canonical set
  [extra: string]: unknown;
}

export type SignalDirection = "CALL" | "PUT" | "NO_SIGNAL";
export type ReasonCode =
  | "DATA_UNAVAILABLE" | "DATA_INVALID" | "DATA_STALE"
  | "INSUFFICIENT_HISTORY" | "FEATURE_ERROR" | "MODEL_UNAVAILABLE"
  | "MODEL_OUTPUT_INVALID" | "MODEL_NOT_VALIDATED"
  | "BELOW_ELIGIBILITY_THRESHOLD" | "TARGET_CANDLE_INVALID"
  | "TIMING_UNVERIFIED" | "INTERNAL_ERROR" | "DATA_SOURCE_UNAUTHORIZED";

export interface SnapshotFrame {
  type: "snapshot";
  asset: string;
  period: number;
  candles: Candle[];
  prediction: Prediction | null;
  ok?: boolean;
  status?: string;
  sub_started?: boolean;
}
export interface TickFrame {
  type: "tick";
  asset: string;
  period: number;
  candle: Candle;              // running candle
  prediction: Prediction | null;
}
export interface EocFrame {
  type: "eoc";
  asset: string;
  period: number;
  candles: Candle[];
  prediction: null;            // gated — arrives via tick
  accuracy?: unknown;
  signal?: SignalDirection | null;  // the graded candle's own signal
  ctime?: number | null;
  coordination_final?: unknown;
}
export interface StaleFrame { type: "stale"; asset: string; period: number; }
export interface MarketClosedFrame {
  type: "market_closed"; asset: string; period: number; reason?: string;
}
export interface PairsFrame {
  type: "pairs";
  pairs?: PairEntry[];          // backward-compat combined list
  real_pairs?: PairEntry[];
  otc_pairs?: PairEntry[];
  payout_floor_real?: number;
  payout_floor_otc?: number;
  [extra: string]: unknown;
}
/** One pair as actually sent on the wire (verified live: /api/pairs and
 *  the WS pairs frame both carry objects, NOT plain strings). */
export interface PairEntry {
  asset: string;
  display?: string;
  status?: string;
  payout?: number | null;
  locked?: boolean;
}
/** Accept string or object entries; return {asset, display} (defensive —
 *  some older paths may send strings). */
export function toPairEntry(v: string | PairEntry): PairEntry {
  if (typeof v === "string") return { asset: v };
  return v;
}
export function pairLabel(p: PairEntry): string {
  return p.display ?? p.asset;
}
export interface StatusFrame {
  type: "status";
  connected: boolean;
  streams?: unknown;
}
export interface SignalsFrame {
  type: "signals";
  signals: Prediction[];
  asset: string;
  period: number;
  before_ctime: number | null;
}
export interface ErrorFrame { type: "error"; error: string; }
export interface OtcPredFrame {
  type: "otc_pred";
  [extra: string]: unknown;     // frozen ML T+1 diagnostic (research-only)
}
export type ServerFrame =
  | SnapshotFrame | TickFrame | EocFrame | StaleFrame | MarketClosedFrame
  | PairsFrame | StatusFrame | SignalsFrame | ErrorFrame | OtcPredFrame
  | ({ type: string; [extra: string]: unknown });

// ── REST response shapes actually returned by the backend ─────────────

export interface HealthzResponse {
  ok: boolean;
  status?: string;
  feed?: string;
  db?: string;
  [extra: string]: unknown;
}
export interface StatusResponse {
  connected?: boolean;
  has_client?: boolean;
  pairs_count?: number;
  market?: string;
  last_tick?: number;
  [extra: string]: unknown;
}
export interface TruthLayerResponse {
  policy_version?: string;
  actionable_signals_possible?: boolean;
  blockers?: Record<string, unknown>;
  [extra: string]: unknown;
}
export interface DataQualityResponse {
  overall?: string;
  checks?: Record<string, unknown>;
  [extra: string]: unknown;
}
export interface WinrateResponse {
  overall?: { total?: number; correct?: number; winrate?: number | null;
               wilson_low?: number | null; wilson_high?: number | null;
               [k: string]: unknown };
  [extra: string]: unknown;
}
export interface ModelsRow {
  model_id?: string;
  version?: string;
  status?: string;
  active?: boolean;
  trained_at?: string | null;
  oos?: unknown;
  [extra: string]: unknown;
}
