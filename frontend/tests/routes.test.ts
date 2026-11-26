/** routes + protocol shape tests */

import { describe, expect, it } from "vitest";
import { parseHash, ROUTES } from "../src/routes";

describe("parseHash", () => {
  it("parses known routes", () => {
    expect(parseHash("#/terminal")).toBe("terminal");
    expect(parseHash("#/history")).toBe("history");
    expect(parseHash("#/analytics?x=1")).toBe("analytics");
    expect(parseHash("#/models")).toBe("models");
  });
  it("falls back to overview on junk/empty", () => {
    expect(parseHash("#/junk")).toBe("overview");
    expect(parseHash("")).toBe("overview");
    expect(parseHash("#/")).toBe("overview");
  });
  it("route ids cover the six pages", () => {
    expect(ROUTES).toEqual(["overview", "terminal", "history", "analytics", "models", "settings"]);
  });
});
