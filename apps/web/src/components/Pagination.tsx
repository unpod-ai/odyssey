"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

/** Prev/Next controls for a cursor-paginated table -- `cursor` is opaque
 * (see `odyssey_api.pagination`), so unlike page numbers there's no way to
 * jump to an arbitrary page or compute "page N of M"; a back-stack of the
 * cursors seen so far is kept in the URL itself (`?back=<cursor>,<cursor>`)
 * so "Previous" can pop it without a second round-trip to the API. */
export function Pagination({
  total,
  shown,
  hasMore,
  nextCursor,
  cursorParam = "cursor",
  backParam = "back",
}: {
  total: number;
  shown: number;
  hasMore: boolean;
  nextCursor: string | null | undefined;
  /** Distinguishes two paginated tables on the same page (e.g. a product's
   * Journeys and Metrics tables) so their cursors don't collide in the URL. */
  cursorParam?: string;
  backParam?: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  if (total === 0) {
    return null;
  }

  const currentCursor = searchParams.get(cursorParam) ?? "";
  const back = searchParams.get(backParam);
  const backStack = back ? back.split(",").filter(Boolean) : [];

  const goTo = (cursor: string, nextBackStack: string[]) => {
    const params = new URLSearchParams(searchParams.toString());
    if (cursor) {
      params.set(cursorParam, cursor);
    } else {
      params.delete(cursorParam);
    }
    if (nextBackStack.length) {
      params.set(backParam, nextBackStack.join(","));
    } else {
      params.delete(backParam);
    }
    const query = params.toString();
    router.push(query ? `${pathname}?${query}` : pathname);
  };

  const goNext = () => {
    if (!nextCursor) return;
    goTo(nextCursor, currentCursor ? [...backStack, currentCursor] : backStack);
  };

  const goPrev = () => {
    const prevStack = [...backStack];
    const prevCursor = prevStack.pop() ?? "";
    goTo(prevCursor, prevStack);
  };

  const canGoPrev = backStack.length > 0 || currentCursor !== "";

  return (
    <div className="pagination-bar">
      <span className="pagination-summary">
        Showing {shown} of {total}
      </span>
      <div className="pagination-controls">
        <button type="button" className="btn" onClick={goPrev} disabled={!canGoPrev}>
          ← Previous
        </button>
        <button type="button" className="btn" onClick={goNext} disabled={!hasMore}>
          Next →
        </button>
      </div>
    </div>
  );
}
