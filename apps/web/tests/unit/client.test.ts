/**
 * The URL-building / error-mapping logic this used to test now lives in
 * `@odyssey/sdk` (see sdk/javascript/tests/client.test.ts) — this app only
 * needs to verify its own thin wrapper picks up ODYSSEY_API_BASE_URL and
 * hands back a real client, not re-test the SDK's own transport.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { OdysseySDK } from "@odyssey/sdk";
import { apiClient } from "@/lib/api";

const originalEnv = process.env.ODYSSEY_API_BASE_URL;

beforeEach(() => {
  process.env.ODYSSEY_API_BASE_URL = "http://test-api";
});

afterEach(() => {
  process.env.ODYSSEY_API_BASE_URL = originalEnv;
});

describe("apiClient", () => {
  it("builds an OdysseySDK", () => {
    expect(apiClient()).toBeInstanceOf(OdysseySDK);
  });

  it("defaults to 127.0.0.1:8000 when ODYSSEY_API_BASE_URL is unset", () => {
    delete process.env.ODYSSEY_API_BASE_URL;
    expect(apiClient()).toBeInstanceOf(OdysseySDK);
  });
});
