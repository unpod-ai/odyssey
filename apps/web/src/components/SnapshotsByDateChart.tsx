"use client";

import { useMemo, useState } from "react";
import type { MetricsSnapshotOut } from "@odyssey/sdk";

const WIDTH = 720;
const HEIGHT = 160;
const PAD_X = 8;
const PAD_TOP = 10;
const PAD_BOTTOM = 26;
const BAR_GAP = 2;

/** Snapshot count per report date, single-hue bars (a magnitude, not a
 * per-host identity — the accent color, not the per-host categorical
 * palette `MetricsChart` uses) with no legend, since one series needs
 * none. Answers "is the fleet still reporting" at a glance, distinct from
 * `MetricsChart`'s per-host trend lines. */
export function SnapshotsByDateChart({ snapshots }: { snapshots: MetricsSnapshotOut[] }) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const bars = useMemo(() => {
    const byDate = new Map<string, number>();
    for (const m of snapshots) {
      const date = m.ts.slice(0, 10);
      if (!date) continue;
      byDate.set(date, (byDate.get(date) ?? 0) + 1);
    }
    return [...byDate.entries()]
      .map(([date, count]) => ({ date, count }))
      .sort((a, b) => a.date.localeCompare(b.date))
      .slice(-30);
  }, [snapshots]);

  if (bars.length === 0) {
    return null;
  }

  const maxCount = Math.max(...bars.map((b) => b.count));
  const plotWidth = WIDTH - PAD_X * 2;
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const barWidth = Math.max(plotWidth / bars.length - BAR_GAP, 2);
  const toY = (count: number) => (maxCount > 0 ? (count / maxCount) * plotHeight : 0);

  // Thin out x-axis date labels when there are many bars, so they never
  // overlap -- every bar still gets a hover tooltip regardless.
  const labelEvery = Math.max(1, Math.ceil(bars.length / 8));

  return (
    <div className="card card-padded chart-card">
      <div className="chart-title">Snapshots reported by date</div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="chart-svg"
        role="img"
        aria-label={`Snapshot count per date, ${bars.length} dates shown`}
        onPointerLeave={() => setHoverIndex(null)}
      >
        {[0, 0.5, 1].map((frac) => {
          const y = PAD_TOP + plotHeight - frac * plotHeight;
          return <line key={frac} x1={PAD_X} x2={WIDTH - PAD_X} y1={y} y2={y} className="chart-gridline" />;
        })}
        {bars.map((b, i) => {
          const x = PAD_X + i * (barWidth + BAR_GAP);
          const barHeight = toY(b.count);
          const y = PAD_TOP + plotHeight - barHeight;
          return (
            <g key={b.date} onPointerEnter={() => setHoverIndex(i)}>
              <rect
                x={x}
                y={y}
                width={barWidth}
                height={Math.max(barHeight, 1)}
                rx={3}
                className="chart-bar"
                data-active={hoverIndex === i}
              />
              {i % labelEvery === 0 && (
                <text
                  x={x + barWidth / 2}
                  y={HEIGHT - 8}
                  textAnchor="middle"
                  className="chart-axis-label"
                >
                  {b.date.slice(5)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {hoverIndex != null && (
        <div className="chart-tooltip">
          <div className="chart-tooltip-row">
            <strong>{bars[hoverIndex].count}</strong> snapshot{bars[hoverIndex].count === 1 ? "" : "s"} —{" "}
            {bars[hoverIndex].date}
          </div>
        </div>
      )}
    </div>
  );
}
