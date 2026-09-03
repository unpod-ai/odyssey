import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { ProductFilterNote } from "@/components/ProductFilter";
import { MetricsChart } from "@/components/MetricsChart";
import type { MetricsSnapshotOut } from "@odyssey/sdk";

// Snapshots land continuously (services/collector's POST /metrics), so this
// page must hit services/api on every request rather than serve the
// `next build`-time snapshot the default static prerender would freeze in.
export const dynamic = "force-dynamic";

export default async function MetricsPage({
  searchParams,
}: PageProps<"/metrics">) {
  const { product } = await searchParams;
  const productFilter = typeof product === "string" ? product : undefined;

  let snapshots: MetricsSnapshotOut[] = [];
  let error: string | null = null;
  try {
    snapshots = await apiClient().metrics.list({ product: productFilter });
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Metrics" description="Host metrics reported by the collector." />
        <p className="error">Failed to load metrics: {error}</p>
      </div>
    );
  }

  const hosts = new Set(snapshots.map((m) => m.hostname)).size;
  const projects = new Set(snapshots.map((m) => m.project).filter((p): p is string => !!p)).size;
  const latest = snapshots.length
    ? snapshots.reduce((a, b) => (a.ts > b.ts ? a : b))
    : null;

  return (
    <div>
      <PageHeader title="Metrics" description="Host metrics reported by the collector." />
      {productFilter && <ProductFilterNote basePath="/metrics" product={productFilter} />}
      <div className="stat-grid">
        <StatCard label="Snapshots" value={snapshots.length} />
        <StatCard label="Hosts reporting" value={hosts} />
        <StatCard label="Projects" value={projects} sub="distinct project tags seen" />
        <StatCard label="Latest snapshot" value={latest?.ts ?? "—"} sub={latest?.hostname} />
        <StatCard
          label="Latest disk free"
          value={
            latest?.disk_free_bytes != null ? `${(latest.disk_free_bytes / 1e9).toFixed(1)} GB` : "—"
          }
          sub={
            latest?.disk_total_bytes != null
              ? `of ${(latest.disk_total_bytes / 1e9).toFixed(1)} GB total`
              : undefined
          }
        />
      </div>
      <MetricsChart snapshots={snapshots} />
      <DataTable
        rows={snapshots}
        keyFor={(m) => `${m.hostname}-${m.ts}`}
        emptyLabel="No metrics snapshots yet — see `ODYSSEY_COLLECT_METRICS`."
        columns={[
          { header: "Timestamp", render: (m) => m.ts },
          { header: "Hostname", render: (m) => m.hostname },
          { header: "OS", render: (m) => m.os },
          { header: "CPUs", render: (m) => m.cpu_count ?? "—" },
          {
            header: "Disk free / total",
            render: (m) =>
              m.disk_free_bytes != null && m.disk_total_bytes != null
                ? `${(m.disk_free_bytes / 1e9).toFixed(1)} / ${(m.disk_total_bytes / 1e9).toFixed(1)} GB`
                : "—",
          },
          { header: "Project", render: (m) => m.project ?? "—" },
          { header: "Public IP", render: (m) => m.public_ip ?? "—" },
        ]}
      />
    </div>
  );
}
