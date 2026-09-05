import Link from "next/link";

/** A single place to see how many rows fall on each date, each a link that
 * sets `?date=` (and clears pagination/cursor state, since the filtered
 * collection changes) -- the count-and-filter-by-date affordance the
 * journeys table didn't have when every date was buried down a column. */
export function DateCounts({
  counts,
  activeDate,
  hrefFor,
}: {
  counts: { date: string; count: number }[];
  activeDate: string;
  hrefFor: (date: string) => string;
}) {
  if (counts.length === 0) {
    return null;
  }

  return (
    <div className="date-counts">
      <span className="date-counts-label">By date:</span>
      <div className="date-counts-list">
        {counts.map(({ date, count }) => (
          <Link
            key={date}
            href={hrefFor(date === activeDate ? "" : date)}
            className={`date-count-chip${date === activeDate ? " date-count-chip-active" : ""}`}
          >
            <span className="mono">{date}</span>
            <span className="date-count-badge">{count}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
