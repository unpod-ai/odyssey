import { notFound } from "next/navigation";
import { apiClient, OdysseyAPINotFoundError } from "@/lib/api";
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
      <h1>{journey.journey_id}</h1>
      <p>Complete: {journey.complete ? "yes" : "no"}</p>
      {journey.incomplete_reason && <p>Reason: {journey.incomplete_reason}</p>}
      <h2>Metrics</h2>
      <ul>
        <li>steps: {journey.metrics.steps ?? "—"}</li>
        <li>aggregated_reward: {journey.metrics.aggregated_reward ?? "—"}</li>
        <li>num_tool_calls: {journey.metrics.num_tool_calls ?? "—"}</li>
        <li>num_tool_failures: {journey.metrics.num_tool_failures ?? "—"}</li>
        <li>tool_error_rate: {journey.metrics.tool_error_rate ?? "—"}</li>
      </ul>
      <h2>Steps ({journey.steps.length})</h2>
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
  );
}
