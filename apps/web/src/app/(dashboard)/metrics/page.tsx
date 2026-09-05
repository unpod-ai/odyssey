import { apiClient } from "@/lib/api";
import { collectAll } from "@/lib/pagination";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { TableFilters } from "@/components/TableFilters";
import { MetricsChart } from "@/components/MetricsChart";
import { SnapshotsByDateChart } from "@/components/SnapshotsByDateChart";
import { distinctProjects } from "@/lib/projects";
import { formatBytes } from "@/lib/format";
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
    // The charts below need every snapshot at once (not one page) --
    // /metrics is paginated server-side, so this walks every page via
    // next_cursor. This is a graphs/analysis view, not a row-by-row
    // listing, so there is no separate paginated table here.
    [snapshots, products] = await Promise.all([
      collectAll<MetricsSnapshotOut>((cursor) =>
        client.metrics.list({ product: productFilter || undefined, cursor, limit: 100 }),
      ),
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
  const latestCpuByHost = new Map<string, number | null | undefined>();
  for (const m of filteredSnapshots) {
    latestCpuByHost.set(m.hostname, m.cpu_count);
  }
  const totalCpus = [...latestCpuByHost.values()].reduce((sum: number, c) => sum + (c ?? 0), 0);

  // One compact card per reporting host, latest snapshot only -- a raw
  // per-snapshot table became unreadable once more than one machine was
  // reporting; the charts above already show the trend, this is just
  // "what does each host look like right now."
  const latestByHost = new Map<string, MetricsSnapshotOut>();
  for (const m of filteredSnapshots) {
    const current = latestByHost.get(m.hostname);
    if (!current || m.ts > current.ts) {
      latestByHost.set(m.hostname, m);
    }
  }
  const sortedHosts = [...latestByHost.keys()].sort((a, b) => a.localeCompare(b));

  return (
    <div>
      <PageHeader title="Metrics" description="Host metrics reported by the collector." />
      <TableFilters
        fields={[
          {
            key: "product",
            label: "Product",
            value: productFilter,
            options: products.map((p) => ({ value: p.slug, label: `${p.name} (${p.slug})` })),
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
        <StatCard label="Total CPUs" value={totalCpus} sub="sum of each host's latest cpu_count" />
        <StatCard label="Projects" value={projects} sub="distinct project tags seen" />
        <StatCard label="Latest snapshot" value={latest?.ts ?? "—"} sub={latest?.hostname} />
      </div>

      {sortedHosts.length === 0 ? (
        <p className="empty">No metrics snapshots yet — see `ODYSSEY_COLLECT_METRICS`.</p>
      ) : (
        <>
          <MetricsChart snapshots={filteredSnapshots} />
          <SnapshotsByDateChart snapshots={filteredSnapshots} />

          <h2>Hosts</h2>
          <div className="host-card-grid">
            {sortedHosts.map((hostname) => {
              const m = latestByHost.get(hostname)!;
              return (
                <div key={hostname} className="card card-padded host-card">
                  <div className="host-card-name mono">{hostname}</div>
                  <div className="host-card-meta">
                    <span className="host-group-chip">{m.os ?? "unknown OS"}</span>
                    {m.cpu_count != null && <span className="host-group-chip">{m.cpu_count} CPUs</span>}
                    {m.public_ip && <span className="host-group-chip mono">{m.public_ip}</span>}
                  </div>
                  <dl className="host-card-stats">
                    <div>
                      <dt>Disk free / total</dt>
                      <dd>
                        {formatBytes(m.disk_free_bytes)} / {formatBytes(m.disk_total_bytes)}
                      </dd>
                    </div>
                    <div>
                      <dt>Memory available / total</dt>
                      <dd>
                        {formatBytes(m.memory_available_bytes)} / {formatBytes(m.memory_total_bytes)}
                      </dd>
                    </div>
                    <div>
                      <dt>Project</dt>
                      <dd>{m.project ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>Last reported</dt>
                      <dd className="mono">{m.ts}</dd>
                    </div>
                  </dl>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
