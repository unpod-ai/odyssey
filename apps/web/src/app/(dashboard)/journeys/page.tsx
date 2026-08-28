import Link from "next/link";
import { api } from "@/lib/api/client";
import { DataTable } from "@/components/DataTable";
import type { JourneySummaryOut } from "@/lib/api/types";

export default async function JourneysPage() {
  let journeys: JourneySummaryOut[] = [];
  let error: string | null = null;
  try {
    journeys = await api.listJourneys();
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
