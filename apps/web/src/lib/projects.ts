import type { MetricsSnapshotOut } from "@odyssey/sdk";

/** Distinct `project` tags seen across a set of metrics snapshots — there's
 * no projects registry (see components/ProductFilter.tsx's product/project
 * distinction), so "the projects for a product" is always derived from
 * whatever snapshots that product has reported, not looked up. */
export function distinctProjects(snapshots: MetricsSnapshotOut[]): string[] {
  const seen = new Set<string>();
  for (const m of snapshots) {
    if (m.project) seen.add(m.project);
  }
  return [...seen].sort();
}
