import { notFound } from "next/navigation";
import { apiClient, OdysseyAPINotFoundError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { Badge } from "@/components/Badge";
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
      </div>

      <h2>Steps ({journey.steps.length})</h2>
      <div className="card table-card">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Trainable status</th>
                <th>Messages</th>
              </tr>
            </thead>
            <tbody>
              {journey.steps.map((step) => (
                <tr key={step.index}>
                  <td>{step.index}</td>
                  <td>{step.trainable_status}</td>
                  <td>{step.message_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
