import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Pagination } from "@/components/Pagination";
import type { ExportArtifactOut } from "@odyssey/sdk";

const PAGE_SIZE = 25;

export default async function ExportsPage({ searchParams }: PageProps<"/exports">) {
  const { cursor } = await searchParams;
  const cursorParam = typeof cursor === "string" ? cursor : undefined;

  let exportsList: ExportArtifactOut[] = [];
  let total = 0;
  let hasMore = false;
  let nextCursor: string | null | undefined = null;
  let error: string | null = null;
  try {
    const page = await apiClient().exports.list({ cursor: cursorParam, limit: PAGE_SIZE });
    exportsList = page.items;
    total = page.total;
    hasMore = page.has_more;
    nextCursor = page.next_cursor;
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
      <Pagination total={total} shown={exportsList.length} hasMore={hasMore} nextCursor={nextCursor} />
    </div>
  );
}
