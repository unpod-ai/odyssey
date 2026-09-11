"use client";

import { useMemo, useState } from "react";

export type SortableHeader = { label: string; sortable: boolean };
export type SortableRow = {
  key: string;
  cells: React.ReactNode[];
  /** Precomputed per-column sort keys (server-rendered `cells` are opaque
   * ReactNode, so sorting needs a parallel array of comparable values). */
  sortValues: (string | number | null)[];
};

function SortIcon({ direction }: { direction?: "asc" | "desc" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      {direction === "asc" ? <path d="m6 15 6-6 6 6" /> : direction === "desc" ? <path d="m6 9 6 6 6-6" /> : (
        <>
          <path d="m7 9 5-5 5 5" />
          <path d="m7 15 5 5 5-5" />
        </>
      )}
    </svg>
  );
}

/** Renders the interactive `<table>` for `DataTable` -- a client component
 * so header clicks can reorder rows, while `cells` stay server-rendered
 * ReactNode (passed through, not re-rendered) so column `render` callbacks
 * never have to cross the server/client boundary as functions. */
export function SortableTable({ headers, rows }: { headers: SortableHeader[]; rows: SortableRow[] }) {
  const [sort, setSort] = useState<{ index: number; dir: "asc" | "desc" } | null>(null);

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const { index, dir } = sort;
    const sign = dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = a.sortValues[index];
      const bv = b.sortValues[index];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
      return cmp * sign;
    });
  }, [rows, sort]);

  const toggleSort = (index: number) => {
    setSort((prev) => {
      if (!prev || prev.index !== index) return { index, dir: "asc" };
      if (prev.dir === "asc") return { index, dir: "desc" };
      return null;
    });
  };

  const sortableHeaders = headers
    .map((h, i) => ({ ...h, index: i }))
    .filter((h) => h.sortable);

  return (
    <>
      {/* Below 640px the header row (and its th-sort buttons) is clipped
       * off-screen by the card-collapse layout, so sorting needs a
       * separate, always-visible control on mobile. */}
      {sortableHeaders.length > 0 && (
        <div className="mobile-sort">
          <label htmlFor="mobile-sort-select" className="mobile-sort-label">
            Sort by
          </label>
          <select
            id="mobile-sort-select"
            className="mobile-sort-select"
            value={sort ? `${sort.index}-${sort.dir}` : ""}
            onChange={(e) => {
              const value = e.target.value;
              if (!value) {
                setSort(null);
                return;
              }
              const [indexStr, dir] = value.split("-");
              setSort({ index: Number(indexStr), dir: dir as "asc" | "desc" });
            }}
          >
            <option value="">Default order</option>
            {sortableHeaders.map((h) => (
              <optgroup key={h.index} label={h.label}>
                <option value={`${h.index}-asc`}>{h.label} (ascending)</option>
                <option value={`${h.index}-desc`}>{h.label} (descending)</option>
              </optgroup>
            ))}
          </select>
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {headers.map((h, i) => (
                <th key={h.label}>
                  {h.sortable ? (
                    <button
                      type="button"
                      className="th-sort"
                      data-active={sort?.index === i}
                      onClick={() => toggleSort(i)}
                      aria-label={`Sort by ${h.label}`}
                    >
                      {h.label}
                      <SortIcon direction={sort?.index === i ? sort.dir : undefined} />
                    </button>
                  ) : (
                    h.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row) => (
              <tr key={row.key}>
                {row.cells.map((cell, i) => (
                  <td key={i} data-label={headers[i].label}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
