import { describe, expect, it } from "vitest";
import { formatBytes, formatMs } from "@/lib/format";

describe("formatMs", () => {
  it("keeps sub-second latencies in the unit a voice deployment tunes in", () => {
    expect(formatMs(0)).toBe("0 ms");
    expect(formatMs(312.4)).toBe("312 ms");
    expect(formatMs(999)).toBe("999 ms");
  });

  it("reads a slow reply in seconds", () => {
    expect(formatMs(1000)).toBe("1.00 s");
    expect(formatMs(2480)).toBe("2.48 s");
  });

  it("shows not-recorded as the dash every other unset value uses", () => {
    expect(formatMs(null)).toBe("—");
    expect(formatMs(undefined)).toBe("—");
    expect(formatMs(Number.NaN)).toBe("—");
  });
});

describe("formatBytes", () => {
  it("still scales to the largest unit", () => {
    expect(formatBytes(1500)).toBe("1.5 KB");
  });
});
