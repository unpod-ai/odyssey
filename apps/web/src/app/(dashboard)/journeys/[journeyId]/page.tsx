import { notFound } from "next/navigation";
import { apiClient, OdysseyAPINotFoundError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { DataTable } from "@/components/DataTable";
import { formatMs } from "@/lib/format";
import Link from "next/link";
import type { JourneyDetailOut } from "@odyssey/sdk";

async function loadJourney(journeyId: string): Promise<JourneyDetailOut> {
  try {
    return await apiClient().journeys.get(journeyId);
  } catch (err) {
    if (err instanceof OdysseyAPINotFoundError) {
      notFound();
    }
    throw err;
  }
}

export default async function JourneyDetailPage({
  params,
}: PageProps<"/journeys/[journeyId]">) {
  const { journeyId } = await params;
  const journey = await loadJourney(journeyId);

  return (
    <div>
      <PageHeader
        title={journey.journey_id}
        description={journey.incomplete_reason ? `Incomplete: ${journey.incomplete_reason}` : undefined}
      />
      <div className="stat-grid">
        <StatCard
          label="Status"
          value={
            <Badge variant={journey.complete ? "success" : "neutral"}>
              {journey.complete ? "complete" : "incomplete"}
            </Badge>
          }
        />
        <StatCard label="Steps" value={journey.metrics.steps ?? "—"} />
        <StatCard label="Aggregated reward" value={journey.metrics.aggregated_reward ?? "—"} />
        <StatCard label="Tool calls" value={journey.metrics.num_tool_calls ?? "—"} />
        <StatCard label="Tool failures" value={journey.metrics.num_tool_failures ?? "—"} />
        <StatCard label="Tool error rate" value={journey.metrics.tool_error_rate ?? "—"} />
        <StatCard label="Recorded by" value={journey.provenance?.framework ?? "—"} />
        <StatCard label="Provider" value={journey.provenance?.providers?.join(", ") || "—"} />
        <StatCard label="Avg latency" value={formatMs(journey.provenance?.avg_latency_ms)} />
        <StatCard label="Avg TTFT" value={formatMs(journey.provenance?.avg_ttft_ms)} />
      </div>

      {journey.provenance?.parent_journey_id ? (
        // The `<call_id>.llm` link the SDK writes: this journey holds the
        // provider calls a voice call made, and the call itself is the
        // conversation. Following it by hand meant knowing the convention.
        <p>
          Provider calls from{" "}
          <Link
            href={`/journeys/${encodeURIComponent(journey.provenance.parent_journey_id)}`}
            className="mono"
          >
            {journey.provenance.parent_journey_id}
          </Link>
        </p>
      ) : null}

      <DataTable
        title={`Steps (${journey.steps.length})`}
        rows={journey.steps}
        keyFor={(step) => String(step.index)}
        emptyLabel="This journey has no recorded steps."
        columns={[
          { header: "#", render: (step) => step.index, sortValue: (step) => step.index },
          {
            header: "Trainable status",
            render: (step) => step.trainable_status,
            sortValue: (step) => step.trainable_status,
          },
          { header: "Messages", render: (step) => step.message_count, sortValue: (step) => step.message_count },
          {
            header: "Provider",
            render: (step) => step.provider ?? "—",
            sortValue: (step) => step.provider ?? "",
          },
          {
            header: "Latency",
            render: (step) => formatMs(step.latency_ms),
            sortValue: (step) => step.latency_ms ?? -1,
          },
          {
            header: "TTFT",
            render: (step) => formatMs(step.ttft_ms),
            sortValue: (step) => step.ttft_ms ?? -1,
          },
        ]}
      />
    </div>
  );
}
