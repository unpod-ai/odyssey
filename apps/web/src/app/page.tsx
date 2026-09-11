import Link from "next/link";
import { apiClient } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/Card";
import { Badge } from "@/components/Badge";
import {
  RouteIcon,
  DatabaseIcon,
  CpuIcon,
  PlayIcon,
  ExportIcon,
  ActivityIcon,
  PackageIcon,
} from "@/components/icons";

const SECTIONS = [
  { href: "/products", label: "Products", description: "Registered tenants (multi-product deployments)", icon: PackageIcon },
  { href: "/journeys", label: "Journeys", description: "Ingested agent journeys and their steps", icon: RouteIcon },
  { href: "/datasets", label: "Datasets", description: "Registered datasets and their versions", icon: DatabaseIcon },
  { href: "/models", label: "Models", description: "Registered models and base checkpoints", icon: CpuIcon },
  { href: "/runs", label: "Eval runs", description: "Benchmark scores from `odyssey eval run`", icon: PlayIcon },
  { href: "/exports", label: "Exports", description: "SFT/DPO export shards", icon: ExportIcon },
  { href: "/metrics", label: "Metrics", description: "Host metrics reported by the collector", icon: ActivityIcon },
];

export default async function HomePage() {
  let status: string;
  let healthy = false;
  try {
    status = (await apiClient().health()).status;
    healthy = true;
  } catch (err) {
    status = `unreachable (${(err as Error).message})`;
  }

  return (
    <div>
      <PageHeader title="Overview" description="services/api status and quick links into the dashboard." />
      <div className="stat-grid">
        <StatCard
          label="API status"
          value={<Badge variant={healthy ? "success" : "danger"}>{healthy ? "healthy" : "unreachable"}</Badge>}
          sub={status}
        />
      </div>
      <div className="nav-card-grid">
        {SECTIONS.map(({ href, label, description, icon: Icon }) => (
          <Link key={href} href={href} className="card nav-card">
            <span className="nav-card-icon">
              <Icon />
            </span>
            <span>
              <span className="nav-card-title">{label}</span>
              <p className="nav-card-desc">{description}</p>
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}
