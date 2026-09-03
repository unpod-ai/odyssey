/** A plain table over an array of rows — used by every list page
 * (journeys/datasets/models/runs/exports) so column definitions are the
 * only per-page code, not five copies of the same `<table>` markup. */

export interface Column<T> {
  header: string;
  render: (row: T) => React.ReactNode;
}

export function DataTable<T>({
  rows,
  columns,
  keyFor,
  emptyLabel,
}: {
  rows: T[];
  columns: Column<T>[];
  keyFor: (row: T) => string;
  emptyLabel: string;
}) {
  if (rows.length === 0) {
    return <p className="empty">{emptyLabel}</p>;
  }
  return (
    <div className="card table-card">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={col.header}>{col.header}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={keyFor(row)}>
                {columns.map((col) => (
                  <td key={col.header}>{col.render(row)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
