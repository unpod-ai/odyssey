"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  RouteIcon,
  DatabaseIcon,
  CpuIcon,
  PlayIcon,
  ExportIcon,
  ActivityIcon,
  PackageIcon,
} from "@/components/icons";
import { LogoMark } from "@/components/Logo";

const LINKS = [
  { href: "/products", label: "Products", icon: PackageIcon },
  { href: "/journeys", label: "Journeys", icon: RouteIcon },
  { href: "/datasets", label: "Datasets", icon: DatabaseIcon },
  { href: "/models", label: "Models", icon: CpuIcon },
  { href: "/runs", label: "Eval runs", icon: PlayIcon },
  { href: "/exports", label: "Exports", icon: ExportIcon },
  { href: "/metrics", label: "Metrics", icon: ActivityIcon },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <Link href="/" className="sidebar-brand">
        <LogoMark size={26} />
        odyssey
      </Link>
      <nav className="sidebar-nav">
        {LINKS.map((link) => {
          const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
          const Icon = link.icon;
          return (
            <Link
              key={link.href}
              href={link.href}
              className={`sidebar-link${active ? " active" : ""}`}
              aria-current={active ? "page" : undefined}
            >
              <Icon />
              {link.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
