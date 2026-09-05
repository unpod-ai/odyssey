/** Shape shared by every cursor-paginated `*PageOut` envelope
 * (`JourneyPageOut`, `MetricsPageOut`, `EvalRunPageOut`, `ExportPageOut`)
 * generated from `services/api`'s OpenAPI schema. */
export type Page<T> = {
  items: T[];
  next_cursor?: string | null;
  has_more: boolean;
  total: number;
};

/** Follows `next_cursor` until a paginated listing is exhausted --
 * `fetchPage` gets the cursor to fetch next (`undefined` for the first
 * page). Only for view logic that genuinely needs the whole collection
 * (per-host grouping, a multi-line chart across every snapshot, a
 * distinct-value count) and not for a page a user pages through by hand,
 * which should stay on the single-page `client.<resource>.list(...)` call. */
export async function collectAll<T>(
  fetchPage: (cursor: string | undefined) => Promise<Page<T>>,
): Promise<T[]> {
  const all: T[] = [];
  let cursor: string | undefined;
  for (;;) {
    const page = await fetchPage(cursor);
    all.push(...page.items);
    if (!page.has_more || !page.next_cursor) {
      break;
    }
    cursor = page.next_cursor;
  }
  return all;
}
