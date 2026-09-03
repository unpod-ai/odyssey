import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import type { ModelOut } from "@odyssey/sdk";

export default async function ModelsPage() {
  let models: ModelOut[] = [];
  let error: string | null = null;
  try {
    models = await apiClient().models.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Models" description="Registered models and base checkpoints." />
        <p className="error">Failed to load models: {error}</p>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Models" description="Registered models and base checkpoints." />
      <DataTable
        rows={models}
        keyFor={(m) => m.name}
        emptyLabel="No models registered yet."
        columns={[
          { header: "Name", render: (m) => m.name },
          { header: "Versions", render: (m) => m.versions.length },
          {
            header: "Latest base model",
            render: (m) => m.versions.at(-1)?.base_model ?? "—",
          },
        ]}
      />
    </div>
  );
}
