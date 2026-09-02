import Link from "next/link";
import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import type { JourneySummaryOut } from "@odyssey/sdk";

// Journeys land continuously (services/collector ingests in real time), so
// this page must hit services/api on every request rather than serve the
// `next build`-time snapshot the default static prerender would freeze in.
export const dynamic = "force-dynamic";

export default async function JourneysPage() {
  let journeys: JourneySummaryOut[] = [];
  let error: string | null = null;
  try {
    journeys = await apiClient().journeys.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return <p className="error">Failed to load journeys: {error}</p>;
  }

  return (
    <div>
      <h1>Journeys</h1>
      <DataTable
        rows={journeys}
        keyFor={(j) => j.journey_id}
        emptyLabel="No journeys yet."
        columns={[
          {
            header: "Journey ID",
            render: (j) => <Link href={`/journeys/${encodeURIComponent(j.journey_id)}`}>{j.journey_id}</Link>,
          },
          { header: "Date", render: (j) => j.date },
          { header: "Complete", render: (j) => (j.complete ? "yes" : "no") },
        ]}
      />
    </div>
  );
}
