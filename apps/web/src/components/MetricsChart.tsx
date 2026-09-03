"use client";

import { useMemo, useState } from "react";
import type { MetricsSnapshotOut } from "@odyssey/sdk";

const WIDTH = 720;
const HEIGHT = 200;
const PAD_X = 8;
const PAD_TOP = 12;
const PAD_BOTTOM = 24;

type Point = { x: number; y: number; pct: number; ts: string; hostname: string };

/** Disk-free % across the most recent snapshots, plotted in reporting order
 * (evenly spaced by index) rather than on a true time scale — snapshots
 * land at irregular intervals per host, and an index axis keeps the line
 * readable without implying elapsed-time precision it can't back up. */
export function MetricsChart({ snapshots }: { snapshots: MetricsSnapshotOut[] }) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const points = useMemo<Point[]>(() => {
    const withPct = snapshots
      .filter((m) => m.disk_free_bytes != null && m.disk_total_bytes != null && m.disk_total_bytes > 0)
      .slice()
      .sort((a, b) => a.ts.localeCompare(b.ts))
      .slice(-40);

    const plotWidth = WIDTH - PAD_X * 2;
    const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
    return withPct.map((m, i) => {
      const pct = (m.disk_free_bytes! / m.disk_total_bytes!) * 100;
      const x = withPct.length > 1 ? PAD_X + (i / (withPct.length - 1)) * plotWidth : PAD_X;
      const y = PAD_TOP + plotHeight - (pct / 100) * plotHeight;
      return { x, y, pct, ts: m.ts, hostname: m.hostname };
    });
  }, [snapshots]);

  if (points.length < 2) {
    return null;
  }

  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const baselineY = PAD_TOP + (HEIGHT - PAD_TOP - PAD_BOTTOM);
  const areaPath = `${linePath} L${points[points.length - 1].x.toFixed(1)},${baselineY} L${points[0].x.toFixed(1)},${baselineY} Z`;
  const active = hoverIndex != null ? points[hoverIndex] : null;

  const handleMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const relX = ((e.clientX - rect.left) / rect.width) * WIDTH;
    let nearest = 0;
    let best = Infinity;
    points.forEach((p, i) => {
      const d = Math.abs(p.x - relX);
      if (d < best) {
        best = d;
        nearest = i;
      }
    });
    setHoverIndex(nearest);
  };

  return (
    <div className="card card-padded chart-card">
      <div className="chart-title">Disk free % — last {points.length} snapshots</div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="chart-svg"
        onPointerMove={handleMove}
        onPointerLeave={() => setHoverIndex(null)}
        role="img"
        aria-label={`Disk free percentage across the last ${points.length} metrics snapshots`}
      >
        {[0, 50, 100].map((tick) => {
          const y = PAD_TOP + (HEIGHT - PAD_TOP - PAD_BOTTOM) - (tick / 100) * (HEIGHT - PAD_TOP - PAD_BOTTOM);
          return (
            <g key={tick}>
              <line x1={PAD_X} x2={WIDTH - PAD_X} y1={y} y2={y} className="chart-gridline" />
              <text x={PAD_X} y={y - 4} className="chart-axis-label">
                {tick}%
              </text>
            </g>
          );
        })}
        <path d={areaPath} className="chart-area" />
        <path d={linePath} className="chart-line" />
        {active && (
          <line x1={active.x} x2={active.x} y1={PAD_TOP} y2={baselineY} className="chart-crosshair" />
        )}
        {points.map((p, i) => (
          <circle
            key={p.ts + p.hostname + i}
            cx={p.x}
            cy={p.y}
            r={i === points.length - 1 || hoverIndex === i ? 4 : 0}
            className="chart-dot"
          />
        ))}
      </svg>
      {active && (
        <div className="chart-tooltip">
          <strong>{active.pct.toFixed(1)}%</strong> free — {active.hostname} · {active.ts}
        </div>
      )}
    </div>
  );
}
