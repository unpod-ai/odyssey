import { api } from "@/lib/api/client";
import { DataTable } from "@/components/DataTable";
import type { ModelOut } from "@/lib/api/types";

export default async function ModelsPage() {
  let models: ModelOut[] = [];
  let error: string | null = null;
  try {
    models = await api.listModels();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return <p className="error">Failed to load models: {error}</p>;
  }

  return (
    <div>
      <h1>Models</h1>
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
