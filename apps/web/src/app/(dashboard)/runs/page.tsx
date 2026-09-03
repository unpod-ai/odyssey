import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import type { EvalRunOut } from "@odyssey/sdk";

export default async function RunsPage() {
  let runs: EvalRunOut[] = [];
  let error: string | null = null;
  try {
    runs = await apiClient().runs.list();
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Eval runs" description="Benchmark scores from `odyssey eval run`." />
        <p className="error">Failed to load eval runs: {error}</p>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Eval runs" description="Benchmark scores from `odyssey eval run`." />
      <DataTable
        title="Eval runs"
        rows={runs}
        keyFor={(r) => r.report_path}
        emptyLabel="No eval reports yet — see `odyssey eval run`."
        columns={[
          { header: "Benchmark", render: (r) => r.benchmark_name, sortValue: (r) => r.benchmark_name },
          { header: "Metric", render: (r) => r.metric_name, sortValue: (r) => r.metric_name },
          { header: "Mean score", render: (r) => r.mean_score.toFixed(3), sortValue: (r) => r.mean_score },
        ]}
      />
    </div>
  );
}
