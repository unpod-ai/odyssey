import Link from "next/link";

const LINKS = [
  { href: "/journeys", label: "Journeys" },
  { href: "/datasets", label: "Datasets" },
  { href: "/models", label: "Models" },
  { href: "/runs", label: "Eval runs" },
  { href: "/exports", label: "Exports" },
  { href: "/metrics", label: "Metrics" },
];

export function Nav() {
  return (
    <nav className="nav">
      <Link href="/" className="nav-brand">
        odyssey
      </Link>
      {LINKS.map((link) => (
        <Link key={link.href} href={link.href}>
          {link.label}
        </Link>
      ))}
    </nav>
  );
}
