/**
 * A thin fetch wrapper for `services/api` — see `types.ts`'s docstring for
 * why this exists instead of `@odyssey/sdk`. Every page in this app is a
 * React Server Component, so plain `fetch` (Next.js's own cached fetch)
 * is enough; no client-side data-fetching library is needed for a
 * read-only dashboard.
 */
import type {
  DatasetOut,
  EvalRunOut,
  ExportArtifactOut,
  HealthOut,
  JourneyDetailOut,
  JourneySummaryOut,
  ModelOut,
} from "./types";

export class OdysseyAPIError extends Error {
  constructor(
    public status: number,
    public path: string,
    body: string,
  ) {
    super(`${status} from ${path}: ${body}`);
  }
}

function baseUrl(): string {
  return process.env.ODYSSEY_API_BASE_URL ?? "http://127.0.0.1:8000";
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${baseUrl()}${path}`, { cache: "no-store" });
  if (!res.ok) {
    throw new OdysseyAPIError(res.status, path, await res.text());
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => get<HealthOut>("/health"),
  listJourneys: () => get<JourneySummaryOut[]>("/journeys"),
  getJourney: (journeyId: string) =>
    get<JourneyDetailOut>(`/journeys/${encodeURIComponent(journeyId)}`),
  listDatasets: () => get<DatasetOut[]>("/datasets"),
  listModels: () => get<ModelOut[]>("/models"),
  listRuns: () => get<EvalRunOut[]>("/runs"),
  listExports: () => get<ExportArtifactOut[]>("/exports"),
};
