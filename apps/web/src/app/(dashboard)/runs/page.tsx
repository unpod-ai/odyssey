import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Pagination } from "@/components/Pagination";
import type { EvalRunOut } from "@odyssey/sdk";

const PAGE_SIZE = 25;

export default async function RunsPage({ searchParams }: PageProps<"/runs">) {
  const { cursor } = await searchParams;
  const cursorParam = typeof cursor === "string" ? cursor : undefined;

  let runs: EvalRunOut[] = [];
  let total = 0;
  let hasMore = false;
  let nextCursor: string | null | undefined = null;
  let error: string | null = null;
  try {
    const page = await apiClient().runs.list({ cursor: cursorParam, limit: PAGE_SIZE });
    runs = page.items;
    total = page.total;
    hasMore = page.has_more;
    nextCursor = page.next_cursor;
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
      <Pagination total={total} shown={runs.length} hasMore={hasMore} nextCursor={nextCursor} />
    </div>
  );
}
