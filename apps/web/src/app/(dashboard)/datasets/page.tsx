import { api } from "@/lib/api/client";
import { DataTable } from "@/components/DataTable";
import type { DatasetOut } from "@/lib/api/types";

export default async function DatasetsPage() {
  let datasets: DatasetOut[] = [];
  let error: string | null = null;
  try {
    datasets = await api.listDatasets();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return <p className="error">Failed to load datasets: {error}</p>;
  }

  return (
    <div>
      <h1>Datasets</h1>
      <DataTable
        rows={datasets}
        keyFor={(d) => d.name}
        emptyLabel="No datasets registered yet."
        columns={[
          { header: "Name", render: (d) => d.name },
          { header: "Versions", render: (d) => d.versions.length },
          {
            header: "Latest",
            render: (d) => (d.versions.length ? `v${Math.max(...d.versions.map((v) => v.version))}` : "—"),
          },
        ]}
      />
    </div>
  );
}
