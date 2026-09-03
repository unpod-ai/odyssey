import Link from "next/link";
import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/Badge";
import { TableFilters } from "@/components/TableFilters";
import type { JourneySummaryOut, ProductOut } from "@odyssey/sdk";

export default async function JourneysPage({
  searchParams,
}: PageProps<"/journeys">) {
  const { product } = await searchParams;
  const productFilter = typeof product === "string" ? product : "";

  let journeys: JourneySummaryOut[] = [];
  let products: ProductOut[] = [];
  let error: string | null = null;
  try {
    const client = apiClient();
    [journeys, products] = await Promise.all([
      client.journeys.list({ product: productFilter || undefined }),
      client.products.list(),
    ]);
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
      <TableFilters
        fields={[
          {
            key: "product",
            label: "Product",
            value: productFilter,
            options: products.map((p) => ({ value: p.slug, label: p.name })),
          },
        ]}
      />
      <DataTable
        title="Journeys"
        rows={journeys}
        keyFor={(j) => j.journey_id}
        emptyLabel="No journeys yet."
        columns={[
          {
            header: "Journey ID",
            render: (j) => (
              <Link href={`/journeys/${encodeURIComponent(j.journey_id)}`} className="mono">
                {j.journey_id}
              </Link>
            ),
            sortValue: (j) => j.journey_id,
          },
          { header: "Date", render: (j) => j.date, sortValue: (j) => j.date },
          {
            header: "Complete",
            render: (j) => <Badge variant={j.complete ? "success" : "neutral"}>{j.complete ? "yes" : "no"}</Badge>,
            sortValue: (j) => (j.complete ? 1 : 0),
          },
        ]}
      />
    </div>
  );
}
