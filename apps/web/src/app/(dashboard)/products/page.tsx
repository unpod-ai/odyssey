import Link from "next/link";
import { apiClient } from "@/lib/api";
import { DataTable } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { distinctProjects } from "@/lib/projects";
import type { ProductOut } from "@odyssey/sdk";

export default async function ProductsPage() {
  let products: ProductOut[] = [];
  let projectCounts: Record<string, number> = {};
  let error: string | null = null;
  try {
    const client = apiClient();
    products = await client.products.list();
    const counts = await Promise.all(
      products.map(async (p) => {
        const snapshots = await client.metrics.list({ product: p.slug });
        return [p.slug, distinctProjects(snapshots).length] as const;
      }),
    );
    projectCounts = Object.fromEntries(counts);
  } catch (err) {
    error = (err as Error).message;
  }

  if (error) {
    return (
      <div>
        <PageHeader title="Products" description="Registered tenants in a multi-product deployment." />
        <p className="error">Failed to load products: {error}</p>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Products"
        description="Registered tenants (`services/collector --products-file` roster). Only populated in a multi-product deployment."
      />
      <DataTable
        rows={products}
        keyFor={(p) => p.slug}
        emptyLabel="No products configured — this is a single-tenant deployment, or ODYSSEY_API_PRODUCTS_FILE is unset."
        columns={[
          {
            header: "Name",
            render: (p) => <Link href={`/products/${encodeURIComponent(p.slug)}`}>{p.name}</Link>,
          },
          { header: "Slug", render: (p) => p.slug },
          { header: "Projects", render: (p) => projectCounts[p.slug] ?? 0 },
          {
            header: "Journeys",
            render: (p) => (
              <Link href={`/journeys?product=${encodeURIComponent(p.slug)}`}>View journeys</Link>
            ),
          },
          {
            header: "Metrics",
            render: (p) => (
              <Link href={`/metrics?product=${encodeURIComponent(p.slug)}`}>View metrics</Link>
            ),
          },
        ]}
      />
    </div>
  );
}
