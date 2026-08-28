import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { api, OdysseyAPIError } from "@/lib/api/client";

const originalFetch = global.fetch;
const originalEnv = process.env.ODYSSEY_API_BASE_URL;

beforeEach(() => {
  process.env.ODYSSEY_API_BASE_URL = "http://test-api";
});

afterEach(() => {
  global.fetch = originalFetch;
  process.env.ODYSSEY_API_BASE_URL = originalEnv;
  vi.restoreAllMocks();
});

function mockFetch(status: number, body: unknown) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }) as unknown as typeof fetch;
}

describe("api client", () => {
  it("calls the right path for listJourneys and parses the response", async () => {
    mockFetch(200, [{ journey_id: "j1", date: "2026-08-28", complete: true }]);
    const result = await api.listJourneys();
    expect(global.fetch).toHaveBeenCalledWith(
      "http://test-api/journeys",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result).toEqual([{ journey_id: "j1", date: "2026-08-28", complete: true }]);
  });

  it("URL-encodes the journey id path parameter", async () => {
    mockFetch(200, { journey_id: "a/b", complete: true, incomplete_reason: null, metrics: {}, steps: [] });
    await api.getJourney("a/b");
    expect(global.fetch).toHaveBeenCalledWith(
      "http://test-api/journeys/a%2Fb",
      expect.anything(),
    );
  });

  it("raises OdysseyAPIError with the status code on a non-2xx response", async () => {
    mockFetch(404, { detail: "not found" });
    await expect(api.getJourney("nope")).rejects.toBeInstanceOf(OdysseyAPIError);
    try {
      await api.getJourney("nope");
    } catch (err) {
      expect((err as OdysseyAPIError).status).toBe(404);
    }
  });
});
