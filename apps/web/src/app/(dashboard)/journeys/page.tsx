import Link from "next/link";
import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/Badge";
import { TableFilters } from "@/components/TableFilters";
import { DateCounts } from "@/components/DateCounts";
import { Pagination } from "@/components/Pagination";
import type { JourneySummaryOut, ProductOut } from "@odyssey/sdk";

const PAGE_SIZE = 25;

export default async function JourneysPage({
  searchParams,
}: PageProps<"/journeys">) {
  const { product, date, cursor } = await searchParams;
  const productFilter = typeof product === "string" ? product : "";
  const dateFilter = typeof date === "string" ? date : "";
  const cursorParam = typeof cursor === "string" ? cursor : undefined;

  let journeys: JourneySummaryOut[] = [];
  let total = 0;
  let hasMore = false;
  let nextCursor: string | null | undefined = null;
  let dateCounts: { date: string; count: number }[] = [];
  let products: ProductOut[] = [];
  let error: string | null = null;
  let effectiveDate = dateFilter;
  try {
    const client = apiClient();
    // Counts (and thus the latest date) must be known before the actual
    // list query -- when no ?date= is given, this page defaults to the
    // latest date rather than mixing every date together.
    const [counts, productList] = await Promise.all([
      client.journeys.counts(),
      client.products.list(),
    ]);
    dateCounts = (counts.by_date ?? [])
      .map(({ date, count }) => ({ date, count }))
      .sort((a, b) => b.date.localeCompare(a.date));
    products = productList;
    effectiveDate = dateFilter || dateCounts[0]?.date || "";

    const page = await client.journeys.list({
      product: productFilter || undefined,
      date: effectiveDate || undefined,
      cursor: cursorParam,
      limit: PAGE_SIZE,
    });
    journeys = page.items;
    total = page.total;
    hasMore = page.has_more;
    nextCursor = page.next_cursor;
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

  const hrefFor = (d: string) => {
    const params = new URLSearchParams();
    if (productFilter) params.set("product", productFilter);
    if (d) params.set("date", d);
    const query = params.toString();
    return query ? `/journeys?${query}` : "/journeys";
  };

  return (
    <div>
      <PageHeader title="Journeys" description="Ingested agent journeys and their steps." />
      <TableFilters
        fields={[
          {
            key: "product",
            label: "Product",
            value: productFilter,
            options: products.map((p) => ({ value: p.slug, label: `${p.name} (${p.slug})` })),
          },
          {
            key: "date",
            label: "Date",
            value: effectiveDate,
            options: dateCounts.map((d) => ({ value: d.date, label: `${d.date} (${d.count})` })),
          },
        ]}
      />
      <DateCounts counts={dateCounts} activeDate={effectiveDate} hrefFor={hrefFor} />
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
      <Pagination total={total} shown={journeys.length} hasMore={hasMore} nextCursor={nextCursor} />
    </div>
  );
}
