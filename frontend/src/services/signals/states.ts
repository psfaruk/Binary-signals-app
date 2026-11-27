/**
 * states.ts — the UI signal-state machine, mirroring the backend Truth
 * Layer contract (directive Phase 7). Pure functions — unit-tested.
 *
 * Cardinal rules encoded here:
 *   1. A research prediction NEVER renders as an actionable signal.
 *   2. Unverified confidence renders as UNAVAILABLE — never a number.
 *   3. Every abstention carries a human-readable reason-code mapping.
 *   4. Frontend states match backend contract states 1:1 (no invented
 *      intermediate states like "maybe").
 */

import type { Prediction, ReasonCode, SignalDirection } from "../websocket/protocol";

export type UiSignalState =
  | "CALL" | "PUT" | "NO_SIGNAL" | "STALE" | "ERROR" | "RESEARCH_ONLY";

export interface SignalView {
  state: UiSignalState;
  direction: SignalDirection;      // what the badge shows
  isActionable: boolean;
  confidence: number | null;        // null → render "unavailable"
  reasonCode: ReasonCode | null;
  reasonText: string;               // bn-first human text
  researchDirection: SignalDirection | null;
  researchConfidence: number | null;
}

/** Bengali-first reason-code copy (English terms kept for codes). */
export const REASON_TEXT_BN: Record<ReasonCode, string> = {
  DATA_UNAVAILABLE: "ডেটা পাওয়া যায়নি — কোনো প্রেডিকশন নেই",
  DATA_INVALID: "ক্যান্ডেল ডেটা অসম্পূর্ণ/ত্রুটিপূর্ণ",
  DATA_STALE: "ফিড পুরোনো (৯০+ সেকেন্ড) — ডেটা বিশ্বাসযোগ্য নয়",
  INSUFFICIENT_HISTORY: "পর্যাপ্ত হিস্টোরি নেই (৬৪+ ক্যান্ডেল লাগবে)",
  FEATURE_ERROR: "ফিচার তৈরি করতে ব্যর্থ",
  MODEL_UNAVAILABLE: "মডেল আর্টিফ্যাক্ট লোড হয়নি",
  MODEL_OUTPUT_INVALID: "মডেল আউটপুট অবৈধ (অ-সসীম/সীমার বাইরে)",
  MODEL_NOT_VALIDATED: "মডেল যাচাইকৃত নয় — কোনো বৈধ OOS প্রমাণ নেই",
  BELOW_ELIGIBILITY_THRESHOLD: "পলিসি থ্রেশহোল্ড পূরণ হয়নি — ইঞ্জিন বিরত",
  TARGET_CANDLE_INVALID: "টার্গেট ক্যান্ডেলের সময়/আইডেন্টিটি অবৈধ",
  TIMING_UNVERIFIED: "ব্রোকার সার্ভার-টাইম সিঙ্ক হয়নি",
  INTERNAL_ERROR: "অপ্রত্যাশিত অভ্যন্তরীণ ত্রুটি (fail-closed)",
  DATA_SOURCE_UNAUTHORIZED: "ডেটা সোর্স অননুমোদিত — অ্যাকশনেবল সিগন্যাল অসম্ভব",
};

/**
 * Map a backend prediction frame → UI view state. THE honesty choke
 * point of the frontend: everything funnels through here.
 */
export function toSignalView(p: Prediction | null | undefined): SignalView {
  // no frame at all → unavailable, not a fake neutral
  if (!p || typeof p !== "object") {
    return unavailable("DATA_UNAVAILABLE");
  }

  const researchOnly = p.research_only === true;
  const direction = normalizeDirection(p.signal);

  // ERROR state from the contract
  if (p.truth_status === "error" || p.reason_code === "INTERNAL_ERROR") {
    return unavailable("INTERNAL_ERROR", { state: "ERROR" });
  }
  if (p.data_freshness_status === "stale" || p.reason_code === "DATA_STALE") {
    return unavailable("DATA_STALE", { state: "STALE" });
  }

  // Research-only frames: show the engine preference ONLY as research.
  if (researchOnly) {
    return {
      state: "RESEARCH_ONLY",
      direction: "NO_SIGNAL",
      isActionable: false,
      confidence: null,                    // rule 2: never a number here
      reasonCode: p.reason_code ?? "DATA_SOURCE_UNAUTHORIZED",
      reasonText: REASON_TEXT_BN[p.reason_code ?? "DATA_SOURCE_UNAUTHORIZED"],
      researchDirection: normalizeDirection(p.research_direction ?? null),
      researchConfidence:
        numberOrNull(p.research_confidence),
    };
  }

  // Eligible actionable frames (only possible once a signed validation
  // registry + authorized source exist — today this branch is unreachable,
  // but the UI must be able to render it honestly the day it happens).
  if (direction === "CALL" || direction === "PUT") {
    return {
      state: direction,
      direction,
      isActionable: true,
      confidence: numberOrNull(p.confidence),
      reasonCode: null,
      reasonText: "",
      researchDirection: null,
      researchConfidence: null,
    };
  }

  // plain NO_SIGNAL
  return unavailable(p.reason_code ?? "BELOW_ELIGIBILITY_THRESHOLD");
}

function unavailable(reason: ReasonCode, opts?: { state?: UiSignalState }): SignalView {
  return {
    state: opts?.state ?? "NO_SIGNAL",
    direction: "NO_SIGNAL",
    isActionable: false,
    confidence: null,
    reasonCode: reason,
    reasonText: REASON_TEXT_BN[reason],
    researchDirection: null,
    researchConfidence: null,
  };
}

export function normalizeDirection(d: unknown): SignalDirection {
  return d === "CALL" ? "CALL" : d === "PUT" ? "PUT" : "NO_SIGNAL";
}

export function numberOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Confidence label: unavailable unless a real measured number exists. */
export function confidenceLabel(v: number | null): string {
  return v === null ? "— অনুপলব্ধ" : `${Math.round(v * 100)}%`;
}

/** Grade badge for history rows: actual outcome only, never invented. */
export function outcomeBadge(p: Prediction): "win" | "loss" | "ungraded" {
  if (p.correct === 1) return "win";
  if (p.correct === 0) return "loss";
  return "ungraded";
}
