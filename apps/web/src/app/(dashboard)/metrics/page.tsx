import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import type { MetricsSnapshotOut } from "@odyssey/sdk";

// Snapshots land continuously (services/collector's POST /metrics), so this
// page must hit services/api on every request rather than serve the
// `next build`-time snapshot the default static prerender would freeze in.
export const dynamic = "force-dynamic";

export default async function MetricsPage() {
  let snapshots: MetricsSnapshotOut[] = [];
  let error: string | null = null;
  try {
    snapshots = await apiClient().metrics.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return <p className="error">Failed to load metrics: {error}</p>;
  }

  return (
    <div>
      <h1>Metrics</h1>
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
