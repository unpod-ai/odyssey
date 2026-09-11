import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import type { DatasetOut } from "@odyssey/sdk";

export default async function DatasetsPage() {
  let datasets: DatasetOut[] = [];
  let error: string | null = null;
  try {
    datasets = await apiClient().datasets.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Datasets" description="Registered datasets and their versions." />
        <p className="error">Failed to load datasets: {error}</p>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Datasets" description="Registered datasets and their versions." />
      <DataTable
        title="Datasets"
        rows={datasets}
        keyFor={(d) => d.name}
        emptyLabel="No datasets registered yet."
        columns={[
          { header: "Name", render: (d) => d.name, sortValue: (d) => d.name },
          { header: "Versions", render: (d) => d.versions.length, sortValue: (d) => d.versions.length },
          {
            header: "Latest",
            render: (d) => (d.versions.length ? `v${Math.max(...d.versions.map((v) => v.version))}` : "—"),
            sortValue: (d) => (d.versions.length ? Math.max(...d.versions.map((v) => v.version)) : null),
          },
        ]}
      />
    </div>
  );
}
