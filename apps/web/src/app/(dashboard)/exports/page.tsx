import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
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
    return <p className="error">Failed to load exports: {error}</p>;
  }

  return (
    <div>
      <h1>Exports</h1>
      <DataTable
        rows={exportsList}
        keyFor={(e) => e.path}
        emptyLabel="No export shards found — see `odyssey sft`/`odyssey dpo`."
        columns={[
          { header: "Name", render: (e) => e.name },
          { header: "Rows", render: (e) => e.rows },
          { header: "sha256", render: (e) => e.sha256.slice(0, 12) + "…" },
        ]}
      />
    </div>
  );
}
