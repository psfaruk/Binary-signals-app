/**
 * signal-states tests — the honesty choke point. These encode the
 * directive's Phase 7 rules as executable statements:
 *   1. research predictions never render actionable
 *   2. unverified confidence never renders a number
 *   3. every abstention has a reason code + bn text
 *   4. states match backend contract 1:1
 */

import { describe, expect, it } from "vitest";
import { toSignalView, confidenceLabel, normalizeDirection, outcomeBadge, REASON_TEXT_BN } from "../src/services/signals/states";
import type { Prediction, ReasonCode } from "../src/services/websocket/protocol";

const P = (p: Partial<Prediction>): Prediction => ({
  signal: "NO_SIGNAL", confidence: null, strength: "NO_SIGNAL", ...p });

describe("toSignalView", () => {
  it("null/undefined → unavailable with DATA_UNAVAILABLE (never a fake neutral)", () => {
    const v = toSignalView(null);
    expect(v.state).toBe("NO_SIGNAL");
    expect(v.isActionable).toBe(false);
    expect(v.confidence).toBeNull();
    expect(v.reasonCode).toBe("DATA_UNAVAILABLE");
    expect(v.reasonText.length).toBeGreaterThan(0);
  });

  it("research_only frame with engine CALL → RESEARCH_ONLY, direction NO_SIGNAL, no confidence number", () => {
    const p: Prediction = P({
      signal: "CALL", confidence: 72, research_only: true,
      reason_code: "DATA_SOURCE_UNAUTHORIZED",
      research_direction: "CALL", research_confidence: 72,
    });
    const v = toSignalView(p);
    expect(v.state).toBe("RESEARCH_ONLY");
    expect(v.direction).toBe("NO_SIGNAL");          // rule 1
    expect(v.isActionable).toBe(false);
    expect(v.confidence).toBeNull();                  // rule 2
    expect(v.researchDirection).toBe("CALL");
    expect(v.researchConfidence).toBe(72);
  });

  it("NO_SIGNAL without research carries reason code + bn text", () => {
    const p: Prediction = P({ signal: "NO_SIGNAL", reason_code: "MODEL_NOT_VALIDATED" });
    const v = toSignalView(p);
    expect(v.state).toBe("NO_SIGNAL");
    expect(v.reasonCode).toBe("MODEL_NOT_VALIDATED");
    expect(REASON_TEXT_BN["MODEL_NOT_VALIDATED"]).toContain("যাচাইকৃত নয়");
  });

  it("INTERNAL_ERROR → ERROR state", () => {
    const v = toSignalView(P({ signal: "NO_SIGNAL", reason_code: "INTERNAL_ERROR" }));
    expect(v.state).toBe("ERROR");
  });

  it("DATA_STALE → STALE state", () => {
    const v = toSignalView(P({ signal: "NO_SIGNAL", reason_code: "DATA_STALE" }));
    expect(v.state).toBe("STALE");
  });

  it("eligible CALL (future: signed registry) renders actionable with confidence", () => {
    const v = toSignalView(P({ signal: "CALL", confidence: 0.71, research_only: false }));
    expect(v.state).toBe("CALL");
    expect(v.isActionable).toBe(true);
    expect(v.confidence).toBe(0.71);
  });

  it("every reason code has non-empty Bengali text (rule 3)", () => {
    const codes: ReasonCode[] = [
      "DATA_UNAVAILABLE", "DATA_INVALID", "DATA_STALE", "INSUFFICIENT_HISTORY",
      "FEATURE_ERROR", "MODEL_UNAVAILABLE", "MODEL_OUTPUT_INVALID",
      "MODEL_NOT_VALIDATED", "BELOW_ELIGIBILITY_THRESHOLD", "TARGET_CANDLE_INVALID",
      "TIMING_UNVERIFIED", "INTERNAL_ERROR", "DATA_SOURCE_UNAUTHORIZED",
    ];
    for (const c of codes) expect(REASON_TEXT_BN[c].length).toBeGreaterThan(3);
  });
});

describe("confidenceLabel", () => {
  it("null renders the unavailable marker, not a number", () => {
    expect(confidenceLabel(null)).toBe("— অনুপলব্ধ");
    expect(confidenceLabel(0.623)).toBe("62%");
  });
});

describe("normalizeDirection", () => {
  it("only CALL/PUT pass through; everything else is NO_SIGNAL", () => {
    expect(normalizeDirection("CALL")).toBe("CALL");
    expect(normalizeDirection("PUT")).toBe("PUT");
    expect(normalizeDirection("UP")).toBe("NO_SIGNAL");
    expect(normalizeDirection(undefined)).toBe("NO_SIGNAL");
    expect(normalizeDirection(7)).toBe("NO_SIGNAL");
  });
});

describe("outcomeBadge", () => {
  it("win/loss/ungraded strictly from correct field", () => {
    expect(outcomeBadge(P({ correct: 1 }))).toBe("win");
    expect(outcomeBadge(P({ correct: 0 }))).toBe("loss");
    expect(outcomeBadge(P({}))).toBe("ungraded");
    expect(outcomeBadge(P({ correct: null }))).toBe("ungraded");
  });
});
