/**
 * Hand-written TypeScript mirrors of `packages/odyssey-schemas`'s pydantic
 * DTOs. Temporary: `docs/STRUCTURE.md` says this app should consume
 * `@odyssey/sdk` (`sdk/javascript`, item 8.5) instead of its own client —
 * that package isn't built yet, so this is a deliberate, documented scope
 * cut, not the intended end state. Replace `src/lib/api/*` with
 * `@odyssey/sdk` imports the same commit 8.5 lands; these types should
 * disappear, not grow.
 */

export interface HealthOut {
  status: string;
}

export interface JourneySummaryOut {
  journey_id: string;
  date: string;
  complete: boolean;
}

export interface StepOut {
  index: number;
  trainable_status: string;
  message_count: number;
}

export interface JourneyMetricsOut {
  steps: number | null;
  aggregated_reward: number | null;
  num_tool_calls: number | null;
  num_tool_failures: number | null;
  tool_error_rate: number | null;
}

export interface JourneyDetailOut {
  journey_id: string;
  complete: boolean;
  incomplete_reason: string | null;
  metrics: JourneyMetricsOut;
  steps: StepOut[];
}

export interface DatasetVersionOut {
  version: number;
  manifest_sha256: string;
  uri: string;
}

export interface DatasetOut {
  name: string;
  versions: DatasetVersionOut[];
}

export interface ModelVersionOut {
  version: number;
  sha256: string;
  uri: string;
  base_model: string | null;
  corpus_version: string | null;
}

export interface ModelOut {
  name: string;
  versions: ModelVersionOut[];
}

export interface EvalRunOut {
  benchmark_name: string;
  metric_name: string;
  mean_score: number;
  report_path: string;
}

export interface ExportArtifactOut {
  name: string;
  path: string;
  rows: number;
  sha256: string;
}
