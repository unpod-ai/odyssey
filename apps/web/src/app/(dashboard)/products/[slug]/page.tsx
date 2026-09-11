import Link from "next/link";
import { notFound } from "next/navigation";
import { apiClient } from "@/lib/api";
import { collectAll } from "@/lib/pagination";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { TableFilters } from "@/components/TableFilters";
import { Pagination } from "@/components/Pagination";
import { distinctProjects } from "@/lib/projects";
import { formatBytes } from "@/lib/format";
import type { JourneySummaryOut, MetricsSnapshotOut, ProductOut } from "@odyssey/sdk";

const PAGE_SIZE = 25;

export default async function ProductDetailPage({
  params,
  searchParams,
}: PageProps<"/products/[slug]">) {
  const { slug } = await params;
  const { project, jcursor, mcursor } = await searchParams;
  const projectFilter = typeof project === "string" ? project : "";
  const jCursor = typeof jcursor === "string" ? jcursor : undefined;
  const mCursor = typeof mcursor === "string" ? mcursor : undefined;
  const client = apiClient();

  let products: ProductOut[] = [];
  let journeyCount = 0;
  let journeys: JourneySummaryOut[] = [];
  let journeysHasMore = false;
  let journeysNextCursor: string | null | undefined = null;
  let allSnapshots: MetricsSnapshotOut[] = [];
  let error: string | null = null;
  try {
    products = await client.products.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title={slug} description="Product detail." />
        <p className="error">Failed to load product: {error}</p>
      </div>
    );
  }

  // notFound() throws a Next.js control-flow error that must propagate
  // uncaught -- it must not sit inside the try/catch above, or it gets
  // swallowed as a generic fetch error and the page renders with a 200
  // instead of a real 404.
  const product = products.find((p) => p.slug === slug);
  if (!product) {
    notFound();
  }

  try {
    const [journeyPage, snapshots] = await Promise.all([
      client.journeys.list({ product: slug, cursor: jCursor, limit: PAGE_SIZE }),
      // Project filter/count below need every snapshot, not one page.
      collectAll<MetricsSnapshotOut>((c) => client.metrics.list({ product: slug, cursor: c, limit: 100 })),
    ]);
    journeys = journeyPage.items;
    journeyCount = journeyPage.total;
    journeysHasMore = journeyPage.has_more;
    journeysNextCursor = journeyPage.next_cursor;
    allSnapshots = snapshots;
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title={product.name} description={`Slug: ${product.slug}`} />
        <p className="error">Failed to load journeys/metrics: {error}</p>
      </div>
    );
  }

  const projects = distinctProjects(allSnapshots);
  const filteredSnapshots = projectFilter
    ? allSnapshots.filter((m) => m.project === projectFilter)
    : allSnapshots;
  // Metrics pagination here walks an already-fully-fetched, filtered array
  // (not the server's cursor) since the project filter is client-side --
  // `mcursor` is a plain numeric offset into `filteredSnapshots`, not an
  // opaque server cursor like `/journeys`' `jcursor`.
  const mOffset = mCursor ? Number.parseInt(mCursor, 10) || 0 : 0;
  const metricsShown = filteredSnapshots.slice(mOffset, mOffset + PAGE_SIZE);
  const metricsNextOffset = mOffset + metricsShown.length;
  const metricsShownHasMore = metricsNextOffset < filteredSnapshots.length;

  return (
    <div>
      <PageHeader title={product.name} description={`Slug: ${product.slug}`} />
      <div className="stat-grid">
        <StatCard label="Journeys" value={journeyCount} />
        <StatCard label="Metrics snapshots" value={filteredSnapshots.length} />
        <StatCard label="Projects" value={projects.length} />
      </div>

      <h2>Projects</h2>
      {projects.length ? (
        <div className="badge-list">
          {projects.map((p) => (
            <Link key={p} href={`/products/${encodeURIComponent(slug)}?project=${encodeURIComponent(p)}`}>
              <Badge variant={p === projectFilter ? "success" : "neutral"}>{p}</Badge>
            </Link>
          ))}
        </div>
      ) : (
        <p className="empty">No project tags reported yet for this product.</p>
      )}

      <h2>Journeys</h2>
      <DataTable
        rows={journeys}
        keyFor={(j) => j.journey_id}
        emptyLabel="No journeys yet for this product."
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
      <Pagination
        total={journeyCount}
        shown={journeys.length}
        hasMore={journeysHasMore}
        nextCursor={journeysNextCursor}
        cursorParam="jcursor"
        backParam="jback"
      />

      <h2>Metrics</h2>
      <TableFilters
        fields={[
          {
            key: "project",
            label: "Project",
            value: projectFilter,
            options: projects.map((p) => ({ value: p, label: p })),
          },
        ]}
      />
      <DataTable
        rows={metricsShown}
        keyFor={(m) => `${m.hostname}-${m.ts}`}
        emptyLabel="No metrics snapshots yet for this product."
        columns={[
          { header: "Timestamp", render: (m) => m.ts, sortValue: (m) => m.ts },
          {
            header: "Hostname",
            render: (m) => <span className="mono">{m.hostname}</span>,
            sortValue: (m) => m.hostname,
          },
          { header: "Project", render: (m) => m.project ?? "—", sortValue: (m) => m.project ?? null },
          {
            header: "Disk free / total",
            render: (m) =>
              m.disk_free_bytes != null && m.disk_total_bytes != null
                ? `${formatBytes(m.disk_free_bytes)} / ${formatBytes(m.disk_total_bytes)}`
                : "—",
            sortValue: (m) => m.disk_free_bytes ?? null,
          },
        ]}
      />
      <Pagination
        total={filteredSnapshots.length}
        shown={metricsShown.length}
        hasMore={metricsShownHasMore}
        nextCursor={metricsShownHasMore ? String(metricsNextOffset) : null}
        cursorParam="mcursor"
        backParam="mback"
      />
    </div>
  );
}
