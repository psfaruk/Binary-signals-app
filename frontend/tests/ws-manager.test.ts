/**
 * ws-manager tests — pure logic: backoff, dedup, subscription bookkeeping.
 * (No real sockets; the browser integration is exercised in E2E manually.)
 */

import { describe, expect, it } from "vitest";
import { backoffMs, isDuplicate, subKey } from "../src/services/websocket/manager";
import type { ServerFrame, TickFrame } from "../src/services/websocket/protocol";

describe("backoffMs", () => {
  it("grows exponentially and caps at 30s", () => {
    expect(backoffMs(0)).toBeLessThanOrEqual(1000);
    expect(backoffMs(1)).toBeLessThanOrEqual(2600);
    const at10 = [...Array(20)].map(() => backoffMs(10));
    expect(Math.max(...at10)).toBeLessThanOrEqual(30_000);
  });
  it("always ≥100ms even with heavy negative jitter", () => {
    for (let i = 0; i < 50; i++) expect(backoffMs(0)).toBeGreaterThanOrEqual(100);
  });
});

describe("subKey", () => {
  it("joins asset@period", () => {
    expect(subKey({ asset: "EURUSD_otc", period: 60 })).toBe("EURUSD_otc@60");
  });
});

describe("isDuplicate", () => {
  const seen: string[] = [];

  it("lets keepalive-shaped frames through (pairs/status are not deduped)", () => {
    expect(isDuplicate({ type: "pairs" } as ServerFrame, seen, 8)).toBe(false);
    expect(isDuplicate({ type: "pairs" } as ServerFrame, seen, 8)).toBe(false);
    expect(isDuplicate({ type: "status" } as ServerFrame, seen, 8)).toBe(false);
  });

  it("dedups identical tick frames (same candle time)", () => {
    const tick: TickFrame = { type: "tick", asset: "A", period: 60, candle: { time: 100, open: 1, high: 2, low: 0.5, close: 1.5 }, prediction: null };
    expect(isDuplicate(tick as ServerFrame, seen, 8)).toBe(false);
    expect(isDuplicate(tick as ServerFrame, seen, 8)).toBe(true);
    const tick2: TickFrame = { ...tick, candle: { ...tick.candle, close: 1.7 } };
    expect(isDuplicate(tick2 as ServerFrame, seen, 8)).toBe(true); // same identity
  });

  it("allows a NEW candle time through", () => {
    const t1 = { type: "tick", asset: "A", period: 60, candle: { time: 101 } } as unknown as ServerFrame;
    const t2 = { type: "tick", asset: "A", period: 60, candle: { time: 102 } } as unknown as ServerFrame;
    expect(isDuplicate(t1, seen, 8)).toBe(false);
    expect(isDuplicate(t2, seen, 8)).toBe(false);
  });

  it("window is bounded", () => {
    const s: string[] = [];
    for (let i = 0; i < 20; i++) {
      isDuplicate({ type: "tick", asset: "A", period: 60, candle: { time: i } } as unknown as ServerFrame, s, 8);
    }
    expect(s.length).toBeLessThanOrEqual(8);
  });
});
