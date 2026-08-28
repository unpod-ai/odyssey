import { api } from "@/lib/api/client";
import { DataTable } from "@/components/DataTable";
import type { EvalRunOut } from "@/lib/api/types";

export default async function RunsPage() {
  let runs: EvalRunOut[] = [];
  let error: string | null = null;
  try {
    runs = await api.listRuns();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return <p className="error">Failed to load eval runs: {error}</p>;
  }

  return (
    <div>
      <h1>Eval runs</h1>
      <DataTable
        rows={runs}
        keyFor={(r) => r.report_path}
        emptyLabel="No eval reports yet — see `odyssey eval run`."
        columns={[
          { header: "Benchmark", render: (r) => r.benchmark_name },
          { header: "Metric", render: (r) => r.metric_name },
          { header: "Mean score", render: (r) => r.mean_score.toFixed(3) },
        ]}
      />
    </div>
  );
}
