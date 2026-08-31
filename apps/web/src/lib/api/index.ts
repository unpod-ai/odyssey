/**
 * Every page is a React Server Component, so a fresh `OdysseySDK` per
 * request is enough — no client-side data-fetching library needed for a
 * read-only dashboard. This file is the only place that knows the base
 * URL; everything else imports `@odyssey/sdk` directly.
 */
import { OdysseySDK } from "@odyssey/sdk";

export { OdysseyAPIError, OdysseyAPINotFoundError } from "@odyssey/sdk";

export function apiClient(): OdysseySDK {
  return new OdysseySDK(process.env.ODYSSEY_API_BASE_URL ?? "http://127.0.0.1:8000");
}
