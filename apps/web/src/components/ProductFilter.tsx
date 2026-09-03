import Link from "next/link";

export function ProductFilterNote({ basePath, product }: { basePath: string; product: string }) {
  return (
    <p className="meta-list">
      Filtered by product <strong>{product}</strong> — <Link href={basePath}>clear filter</Link>
    </p>
  );
}
