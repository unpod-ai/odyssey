"use client";

import { useMemo, useState } from "react";
import type { MetricsSnapshotOut } from "@odyssey/sdk";

const WIDTH = 720;
const HEIGHT = 220;
const PAD_X = 8;
const PAD_TOP = 12;
const PAD_BOTTOM = 24;
const PAD_RIGHT_LABEL = 92; // room for the direct end-of-line hostname label

// Fixed categorical order -- color follows the entity (hostname), never
// rank/count, so a host keeps its color across re-sorts and filters.
const SERIES_COLOR_VARS = [
  "--series-1",
  "--series-2",
  "--series-3",
  "--series-4",
  "--series-5",
  "--series-6",
];

type Point = { x: number; y: number; pct: number; ts: string };
type Series = { hostname: string; color: string; points: Point[] };

/** Disk-free % per host, one line per hostname on a shared real-time x-axis
 * (unlike a single-series index axis, comparing hosts needs their actual
 * report times aligned, not just reporting order) -- lets you see which
 * host is trending down relative to the others, not just one host in
 * isolation. */
export function MetricsChart({ snapshots, title }: { snapshots: MetricsSnapshotOut[]; title?: string }) {
  const [hoverX, setHoverX] = useState<number | null>(null);
  const [hiddenHosts, setHiddenHosts] = useState<Set<string>>(new Set());

  const series = useMemo(() => {
    const byHost = new Map<string, { ts: string; time: number; pct: number }[]>();
    for (const m of snapshots) {
      if (m.disk_free_bytes == null || m.disk_total_bytes == null || m.disk_total_bytes <= 0) continue;
      const time = Date.parse(m.ts);
      if (Number.isNaN(time)) continue;
      const list = byHost.get(m.hostname) ?? [];
      list.push({ ts: m.ts, time, pct: (m.disk_free_bytes / m.disk_total_bytes) * 100 });
      byHost.set(m.hostname, list);
    }

    const hostnames = [...byHost.keys()].sort((a, b) => a.localeCompare(b));
    let min = Infinity;
    let max = -Infinity;
    for (const list of byHost.values()) {
      for (const p of list) {
        if (p.time < min) min = p.time;
        if (p.time > max) max = p.time;
      }
    }

    const plotWidth = WIDTH - PAD_X - PAD_RIGHT_LABEL;
    const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
    const toX = (time: number) => (max > min ? PAD_X + ((time - min) / (max - min)) * plotWidth : PAD_X);
    const toY = (pct: number) => PAD_TOP + plotHeight - (pct / 100) * plotHeight;

    const series: Series[] = hostnames.map((hostname, i) => {
      const raw = (byHost.get(hostname) ?? []).slice().sort((a, b) => a.time - b.time).slice(-60);
      return {
        hostname,
        color: `var(${SERIES_COLOR_VARS[i % SERIES_COLOR_VARS.length]})`,
        points: raw.map((p) => ({ x: toX(p.time), y: toY(p.pct), pct: p.pct, ts: p.ts })),
      };
    });

    return series;
  }, [snapshots]);

  const visibleSeries = series.filter((s) => s.points.length >= 2 && !hiddenHosts.has(s.hostname));

  if (series.every((s) => s.points.length < 2)) {
    return null;
  }

  const baselineY = PAD_TOP + (HEIGHT - PAD_TOP - PAD_BOTTOM);
  const totalPoints = series.reduce((sum, s) => sum + s.points.length, 0);
  const chartTitle = title ?? `Disk free % by host — last ${totalPoints} snapshots`;
  const directLabel = series.length <= 4;

  const toggleHost = (hostname: string) => {
    setHiddenHosts((prev) => {
      const next = new Set(prev);
      if (next.has(hostname)) next.delete(hostname);
      else next.add(hostname);
      return next;
    });
  };

  const handleMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    setHoverX(((e.clientX - rect.left) / rect.width) * WIDTH);
  };

  // Nearest point per visible series to the cursor's x -- each series has
  // its own timestamps, so "nearest" is computed per line, not shared.
  const active =
    hoverX == null
      ? []
      : visibleSeries
          .map((s) => {
            let nearest = s.points[0];
            let best = Infinity;
            for (const p of s.points) {
              const d = Math.abs(p.x - hoverX);
              if (d < best) {
                best = d;
                nearest = p;
              }
            }
            return { hostname: s.hostname, color: s.color, point: nearest };
          })
          .filter((a) => a.point);

  return (
    <div className="card card-padded chart-card">
      <div className="chart-title">{chartTitle}</div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="chart-svg"
        onPointerMove={handleMove}
        onPointerLeave={() => setHoverX(null)}
        role="img"
        aria-label={`${chartTitle}, one line per host: ${visibleSeries.map((s) => s.hostname).join(", ")}`}
      >
        {[0, 50, 100].map((tick) => {
          const y = PAD_TOP + (HEIGHT - PAD_TOP - PAD_BOTTOM) - (tick / 100) * (HEIGHT - PAD_TOP - PAD_BOTTOM);
          return (
            <g key={tick}>
              <line x1={PAD_X} x2={WIDTH - PAD_RIGHT_LABEL} y1={y} y2={y} className="chart-gridline" />
              <text x={PAD_X} y={y - 4} className="chart-axis-label">
                {tick}%
              </text>
            </g>
          );
        })}

        {hoverX != null && (
          <line x1={hoverX} x2={hoverX} y1={PAD_TOP} y2={baselineY} className="chart-crosshair" />
        )}

        {visibleSeries.map((s) => {
          const linePath = s.points
            .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`)
            .join(" ");
          const last = s.points[s.points.length - 1];
          const activeForHost = active.find((a) => a.hostname === s.hostname);
          return (
            <g key={s.hostname}>
              <path d={linePath} className="chart-line" style={{ stroke: s.color }} />
              <circle cx={last.x} cy={last.y} r={4} style={{ fill: s.color }} className="chart-dot" />
              {activeForHost && (
                <circle
                  cx={activeForHost.point.x}
                  cy={activeForHost.point.y}
                  r={5}
                  style={{ fill: s.color }}
                  className="chart-dot"
                />
              )}
              {directLabel && (
                <text
                  x={last.x + 7}
                  y={last.y + 3}
                  className="chart-series-label"
                  style={{ fill: s.color }}
                >
                  {s.hostname}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      {series.length >= 2 && (
        <div className="chart-legend">
          {series.map((s) => (
            <button
              key={s.hostname}
              type="button"
              className="chart-legend-item"
              data-hidden={hiddenHosts.has(s.hostname)}
              aria-pressed={!hiddenHosts.has(s.hostname)}
              onClick={() => toggleHost(s.hostname)}
            >
              <span className="chart-legend-swatch" style={{ background: s.color }} />
              {s.hostname}
            </button>
          ))}
        </div>
      )}

      {active.length > 0 && (
        <div className="chart-tooltip">
          {active.map((a) => (
            <div key={a.hostname} className="chart-tooltip-row">
              <span className="chart-legend-swatch" style={{ background: a.color }} />
              <strong>{a.point.pct.toFixed(1)}%</strong> free — {a.hostname} · {a.point.ts}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
