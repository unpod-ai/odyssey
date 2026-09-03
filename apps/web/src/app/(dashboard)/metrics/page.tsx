import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { TableFilters } from "@/components/TableFilters";
import { MetricsChart } from "@/components/MetricsChart";
import { distinctProjects } from "@/lib/projects";
import type { MetricsSnapshotOut, ProductOut } from "@odyssey/sdk";

export default async function MetricsPage({
  searchParams,
}: PageProps<"/metrics">) {
  const { product, project } = await searchParams;
  const productFilter = typeof product === "string" ? product : "";
  const projectFilter = typeof project === "string" ? project : "";

  let snapshots: MetricsSnapshotOut[] = [];
  let products: ProductOut[] = [];
  let error: string | null = null;
  try {
    const client = apiClient();
    [snapshots, products] = await Promise.all([
      client.metrics.list({ product: productFilter || undefined }),
      client.products.list(),
    ]);
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

  // `project` is a free-text tag, not a directory-scoped concept like
  // `product` (see lib/projects.ts) -- services/api has no `?project=` to
  // filter by server-side, so this narrows the already-fetched list.
  const projectOptions = distinctProjects(snapshots);
  const filteredSnapshots = projectFilter
    ? snapshots.filter((m) => m.project === projectFilter)
    : snapshots;

  const hosts = new Set(filteredSnapshots.map((m) => m.hostname)).size;
  const projects = new Set(filteredSnapshots.map((m) => m.project).filter((p): p is string => !!p)).size;
  const latest = filteredSnapshots.length
    ? filteredSnapshots.reduce((a, b) => (a.ts > b.ts ? a : b))
    : null;

  // One card per reporting host — a flat table of every snapshot became
  // unreadable once more than one machine was reporting, since the same
  // hostname/IP repeated down the hostname column instead of grouping.
  const hostGroups = new Map<string, MetricsSnapshotOut[]>();
  for (const m of filteredSnapshots) {
    const list = hostGroups.get(m.hostname) ?? [];
    list.push(m);
    hostGroups.set(m.hostname, list);
  }
  const sortedHosts = [...hostGroups.keys()].sort((a, b) => a.localeCompare(b));

  return (
    <div>
      <PageHeader title="Metrics" description="Host metrics reported by the collector." />
      <TableFilters
        fields={[
          {
            key: "product",
            label: "Product",
            value: productFilter,
            options: products.map((p) => ({ value: p.slug, label: p.name })),
          },
          {
            key: "project",
            label: "Project",
            value: projectFilter,
            options: projectOptions.map((p) => ({ value: p, label: p })),
          },
        ]}
      />
      <div className="stat-grid">
        <StatCard label="Snapshots" value={filteredSnapshots.length} />
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
      <MetricsChart snapshots={filteredSnapshots} />

      {sortedHosts.length === 0 && (
        <p className="empty">No metrics snapshots yet — see `ODYSSEY_COLLECT_METRICS`.</p>
      )}

      {sortedHosts.map((hostname) => {
        const rows = hostGroups
          .get(hostname)!
          .slice()
          .sort((a, b) => b.ts.localeCompare(a.ts));
        const latestForHost = rows[0];
        return (
          <div key={hostname} className="host-group">
            <div className="host-group-header">
              <span className="host-group-name mono">{hostname}</span>
              <span className="host-group-meta">
                <span className="host-group-chip">{latestForHost.os ?? "unknown OS"}</span>
                {latestForHost.cpu_count != null && (
                  <span className="host-group-chip">{latestForHost.cpu_count} CPUs</span>
                )}
                {latestForHost.public_ip && (
                  <span className="host-group-chip mono">{latestForHost.public_ip}</span>
                )}
              </span>
            </div>
            <DataTable
              rows={rows}
              keyFor={(m) => `${m.hostname}-${m.ts}`}
              emptyLabel="No snapshots for this host."
              columns={[
                { header: "Timestamp", render: (m) => m.ts, sortValue: (m) => m.ts },
                {
                  header: "Disk free / total",
                  render: (m) =>
                    m.disk_free_bytes != null && m.disk_total_bytes != null
                      ? `${(m.disk_free_bytes / 1e9).toFixed(1)} / ${(m.disk_total_bytes / 1e9).toFixed(1)} GB`
                      : "—",
                  sortValue: (m) => m.disk_free_bytes ?? null,
                },
                { header: "Project", render: (m) => m.project ?? "—", sortValue: (m) => m.project ?? null },
              ]}
            />
          </div>
        );
      })}
    </div>
  );
}
