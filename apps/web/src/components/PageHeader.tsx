export function PageHeader({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {description && <p>{description}</p>}
    </div>
  );
}
