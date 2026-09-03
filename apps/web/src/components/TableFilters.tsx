"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

export type FilterField = {
  key: string;
  label: string;
  value: string;
  options: { value: string; label: string }[];
};

/** A row of `<select>` filters that drive the page via URL query params --
 * kept in the URL (not local state) so a filtered view stays shareable and
 * survives a refresh, same as the `?product=` links Products already
 * points here with. Renders nothing if every field has no options to pick
 * from (e.g. a single-tenant deployment with no products configured). */
export function TableFilters({ fields }: { fields: FilterField[] }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const visibleFields = fields.filter((f) => f.options.length > 0);
  if (visibleFields.length === 0) {
    return null;
  }

  const updateParam = (key: string, value: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (value) {
      params.set(key, value);
    } else {
      params.delete(key);
    }
    const query = params.toString();
    router.push(query ? `${pathname}?${query}` : pathname);
  };

  return (
    <div className="filter-bar">
      {visibleFields.map((field) => (
        <div key={field.key} className="filter-field">
          <label htmlFor={`filter-${field.key}`}>{field.label}</label>
          <select
            id={`filter-${field.key}`}
            className="filter-select"
            value={field.value}
            onChange={(e) => updateParam(field.key, e.target.value)}
          >
            <option value="">All {field.label.toLowerCase()}</option>
            {field.options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      ))}
    </div>
  );
}
