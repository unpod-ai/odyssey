import { SortableTable } from "@/components/SortableTable";

/** A plain table over an array of rows — used by every list page
 * (journeys/datasets/models/runs/exports) so column definitions are the
 * only per-page code, not five copies of the same `<table>` markup. */

export interface Column<T> {
  header: string;
  render: (row: T) => React.ReactNode;
  /** Enables click-to-sort on this column's header. Return the raw
   * comparable value (not the rendered node) -- e.g. a number for byte
   * counts, or the ISO string for a timestamp. */
  sortValue?: (row: T) => string | number | null;
}

export function DataTable<T>({
  rows,
  columns,
  keyFor,
  emptyLabel,
  title,
}: {
  rows: T[];
  columns: Column<T>[];
  keyFor: (row: T) => string;
  emptyLabel: string;
  /** Optional header bar showing a title and row count above the table. */
  title?: string;
}) {
  if (rows.length === 0) {
    return <p className="empty">{emptyLabel}</p>;
  }
  return (
    <div className="card table-card">
      {title && (
        <div className="table-card-header">
          <span>{title}</span>
          <span className="table-card-count">{rows.length}</span>
        </div>
      )}
      <SortableTable
        headers={columns.map((col) => ({ label: col.header, sortable: !!col.sortValue }))}
        rows={rows.map((row) => ({
          key: keyFor(row),
          cells: columns.map((col) => col.render(row)),
          sortValues: columns.map((col) => col.sortValue?.(row) ?? null),
        }))}
      />
    </div>
  );
}
