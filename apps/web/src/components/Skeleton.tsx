/** Static markup shown by each route's `loading.tsx` while its Server
 * Component awaits the API — without this, navigating to a slow page is a
 * frozen/blank `main` until the fetch resolves. */

function SkeletonBlock({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`.trim()} />;
}

export function SkeletonPage({ statCount = 3 }: { statCount?: number }) {
  return (
    <div role="status" aria-busy="true" aria-label="Loading">
      <div className="page-header">
        <SkeletonBlock className="skeleton-title" />
        <SkeletonBlock className="skeleton-desc" />
      </div>
      {statCount > 0 && (
        <div className="stat-grid">
          {Array.from({ length: statCount }).map((_, i) => (
            <div key={i} className="card stat-card">
              <SkeletonBlock className="skeleton-label" />
              <SkeletonBlock className="skeleton-value" />
            </div>
          ))}
        </div>
      )}
      <div className="card table-card">
        <div className="skeleton-table">
          {Array.from({ length: 5 }).map((_, i) => (
            <SkeletonBlock key={i} className="skeleton-row" />
          ))}
        </div>
      </div>
    </div>
  );
}
