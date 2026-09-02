import { raiseForStatus } from "./errors.js";
import type { HealthOut } from "./types.generated.js";
import { DatasetsResource } from "./resources/datasets.js";
import { ExportsResource } from "./resources/exports.js";
import { JourneysResource } from "./resources/journeys.js";
import { ModelsResource } from "./resources/models.js";
import { RunsResource } from "./resources/runs.js";

export class Transport {
  constructor(
    private readonly baseUrl: string,
    private readonly apiKey?: string,
  ) {}

  async get<T>(path: string): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.apiKey) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    }
    const res = await fetch(`${this.baseUrl}${path}`, { headers });
    const body = await res.text();
    if (!res.ok) {
      raiseForStatus(res.status, body, path);
    }
    return JSON.parse(body) as T;
  }
}

export class OdysseySDK {
  readonly journeys: JourneysResource;
  readonly datasets: DatasetsResource;
  readonly models: ModelsResource;
  readonly runs: RunsResource;
  readonly exports: ExportsResource;
  private readonly transport: Transport;

  constructor(baseUrl: string, apiKey?: string) {
    // Node-side only (this runs server-side, e.g. apps/web) -- browsers
    // don't have process.env, so guard the lookup.
    const resolvedApiKey =
      apiKey ??
      (typeof process !== "undefined" ? process.env?.ODYSSEY_API_AUTH_KEY : undefined);
    this.transport = new Transport(baseUrl, resolvedApiKey);
    this.journeys = new JourneysResource(this.transport);
    this.datasets = new DatasetsResource(this.transport);
    this.models = new ModelsResource(this.transport);
    this.runs = new RunsResource(this.transport);
    this.exports = new ExportsResource(this.transport);
  }

  async health(): Promise<HealthOut> {
    return this.transport.get<HealthOut>("/health");
  }
}
