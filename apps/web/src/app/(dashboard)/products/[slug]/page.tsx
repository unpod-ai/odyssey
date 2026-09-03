import Link from "next/link";
import { notFound } from "next/navigation";
import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { Badge } from "@/components/Badge";
import { distinctProjects } from "@/lib/projects";
import type { JourneySummaryOut, MetricsSnapshotOut, ProductOut } from "@odyssey/sdk";

export default async function ProductDetailPage({
  params,
}: PageProps<"/products/[slug]">) {
  const { slug } = await params;
  const client = apiClient();

  let products: ProductOut[] = [];
  let journeys: JourneySummaryOut[] = [];
  let snapshots: MetricsSnapshotOut[] = [];
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
    [journeys, snapshots] = await Promise.all([
      client.journeys.list({ product: slug }),
      client.metrics.list({ product: slug }),
    ]);
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

  const projects = distinctProjects(snapshots);

  return (
    <div>
      <PageHeader title={product.name} description={`Slug: ${product.slug}`} />
      <div className="stat-grid">
        <StatCard label="Journeys" value={journeys.length} />
        <StatCard label="Metrics snapshots" value={snapshots.length} />
        <StatCard label="Projects" value={projects.length} />
      </div>

      <h2>Projects</h2>
      {projects.length ? (
        <div className="badge-list">
          {projects.map((p) => (
            <Badge key={p} variant="neutral">
              {p}
            </Badge>
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

      <h2>Metrics</h2>
      <DataTable
        rows={snapshots}
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
                ? `${(m.disk_free_bytes / 1e9).toFixed(1)} / ${(m.disk_total_bytes / 1e9).toFixed(1)} GB`
                : "—",
            sortValue: (m) => m.disk_free_bytes ?? null,
          },
        ]}
      />
    </div>
  );
}
