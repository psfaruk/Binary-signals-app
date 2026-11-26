/**
 * protocol wire-parity tests — locked to the ACTUAL backend payloads
 * (verified live 2026-11-27 against the running server):
 *   • /api/pairs & WS `pairs` frame entries are OBJECTS
 *     {asset, display, status, payout, locked} — the string assumption
 *     crashed React (#31) on 2026-11-27; this test keeps that fixed.
 */

import { describe, expect, it } from "vitest";
import { toPairEntry, pairLabel, type PairEntry } from "../src/services/websocket/protocol";

describe("wire parity: pair entries", () => {
  it("accepts the real object shape from /api/pairs", () => {
    const wire: PairEntry = {
      asset: "USDBDT_otc", display: "USD/BDT", status: "otc",
      payout: null, locked: false,
    };
    expect(toPairEntry(wire)).toBe(wire);
    expect(pairLabel(wire)).toBe("USD/BDT");
  });

  it("defends against legacy string entries", () => {
    expect(toPairEntry("EURUSD_otc")).toEqual({ asset: "EURUSD_otc" });
    expect(pairLabel({ asset: "EURUSD_otc" })).toBe("EURUSD_otc");
  });
});
