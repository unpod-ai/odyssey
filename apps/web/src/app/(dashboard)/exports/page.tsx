import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import type { ExportArtifactOut } from "@odyssey/sdk";

export default async function ExportsPage() {
  let exportsList: ExportArtifactOut[] = [];
  let error: string | null = null;
  try {
    exportsList = await apiClient().exports.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Exports" description="SFT/DPO export shards." />
        <p className="error">Failed to load exports: {error}</p>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Exports" description="SFT/DPO export shards." />
      <DataTable
        title="Exports"
        rows={exportsList}
        keyFor={(e) => e.path}
        emptyLabel="No export shards found — see `odyssey sft`/`odyssey dpo`."
        columns={[
          { header: "Name", render: (e) => e.name, sortValue: (e) => e.name },
          { header: "Rows", render: (e) => e.rows, sortValue: (e) => e.rows },
          {
            header: "sha256",
            render: (e) => <span className="mono">{e.sha256.slice(0, 12)}…</span>,
            sortValue: (e) => e.sha256,
          },
        ]}
      />
    </div>
  );
}
