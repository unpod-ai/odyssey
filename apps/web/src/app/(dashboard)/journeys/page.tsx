import Link from "next/link";
import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/Badge";
import { ProductFilterNote } from "@/components/ProductFilter";
import type { JourneySummaryOut } from "@odyssey/sdk";

export default async function JourneysPage({
  searchParams,
}: PageProps<"/journeys">) {
  const { product } = await searchParams;
  const productFilter = typeof product === "string" ? product : undefined;

  let journeys: JourneySummaryOut[] = [];
  let error: string | null = null;
  try {
    journeys = await apiClient().journeys.list({ product: productFilter });
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Journeys" description="Ingested agent journeys and their steps." />
        <p className="error">Failed to load journeys: {error}</p>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Journeys" description="Ingested agent journeys and their steps." />
      {productFilter && <ProductFilterNote basePath="/journeys" product={productFilter} />}
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
          {
            header: "Complete",
            render: (j) => <Badge variant={j.complete ? "success" : "neutral"}>{j.complete ? "yes" : "no"}</Badge>,
          },
        ]}
      />
    </div>
  );
}
