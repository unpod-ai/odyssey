export function Badge({
  children,
  variant = "neutral",
}: {
  children: React.ReactNode;
  variant?: "success" | "danger" | "neutral";
}) {
  return <span className={`badge badge-${variant}`}>{children}</span>;
}
