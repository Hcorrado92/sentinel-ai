/** Plain-SVG charts (no chart library is installed). */
import { useId } from "react";
import type { BucketCount } from "../api/types";
import { formatDate } from "./Badges";
import { EmptyState } from "./StatusViews";

export const SEVERITY_ORDER = ["low", "medium", "high", "critical"] as const;

const SEVERITY_FILLS: Record<string, string> = {
  critical: "fill-red-600",
  high: "fill-orange-500",
  medium: "fill-yellow-400",
  low: "fill-slate-400",
};

/** Counts per severity, always including the four known levels. */
export function countBySeverity(items: { severity: string }[]): Record<string, number> {
  const counts: Record<string, number> = Object.fromEntries(SEVERITY_ORDER.map((s) => [s, 0]));
  for (const item of items) counts[item.severity] = (counts[item.severity] ?? 0) + 1;
  return counts;
}

const BAR_HEIGHT = 18;
const BAR_GAP = 6;
const LABEL_WIDTH = 64;
const CHART_WIDTH = 320;
const COUNT_WIDTH = 32;

export function SeverityBreakdown({
  counts,
  title = "Incidents by severity",
}: {
  counts: Record<string, number>;
  title?: string;
}) {
  const titleId = useId();
  const levels = [...SEVERITY_ORDER, ...Object.keys(counts).filter((s) => !(SEVERITY_ORDER as readonly string[]).includes(s))];
  const total = levels.reduce((sum, level) => sum + (counts[level] ?? 0), 0);
  if (total === 0) return <EmptyState message="No incidents to break down by severity." />;

  const max = Math.max(...levels.map((level) => counts[level] ?? 0), 1);
  const barArea = CHART_WIDTH - LABEL_WIDTH - COUNT_WIDTH;
  const height = levels.length * (BAR_HEIGHT + BAR_GAP);

  return (
    <svg
      role="img"
      aria-labelledby={titleId}
      data-testid="severity-breakdown"
      viewBox={`0 0 ${CHART_WIDTH} ${height}`}
      className="h-auto w-full max-w-sm"
    >
      <title id={titleId}>{`${title}: ${levels.map((l) => `${l} ${counts[l] ?? 0}`).join(", ")}`}</title>
      {levels.map((level, index) => {
        const count = counts[level] ?? 0;
        const y = index * (BAR_HEIGHT + BAR_GAP);
        return (
          <g key={level} data-testid="severity-bar" data-severity={level} data-count={count}>
            <text x={0} y={y + BAR_HEIGHT - 5} className="fill-slate-600 text-[11px]">
              {level}
            </text>
            <rect
              x={LABEL_WIDTH}
              y={y}
              width={(count / max) * barArea}
              height={BAR_HEIGHT}
              rx={2}
              className={SEVERITY_FILLS[level] ?? "fill-slate-400"}
            />
            <text
              x={LABEL_WIDTH + (count / max) * barArea + 4}
              y={y + BAR_HEIGHT - 5}
              className="fill-slate-700 text-[11px]"
            >
              {count}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

const SPARK_WIDTH = 320;
const SPARK_HEIGHT = 48;

/** One bar per time bucket of an incident's event counts. */
export function FrequencySparkline({
  buckets,
  title = "Events over time",
}: {
  buckets: BucketCount[];
  title?: string;
}) {
  const titleId = useId();
  if (buckets.length === 0) return <EmptyState message="No events to chart yet." />;

  const max = Math.max(...buckets.map((b) => b.count), 1);
  const slot = SPARK_WIDTH / buckets.length;
  const barWidth = Math.max(slot * 0.8, 1);
  const total = buckets.reduce((sum, b) => sum + b.count, 0);

  return (
    <svg
      role="img"
      aria-labelledby={titleId}
      data-testid="frequency-sparkline"
      viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
      preserveAspectRatio="none"
      className="h-12 w-full max-w-md"
    >
      <title id={titleId}>{`${title}: ${total} events in ${buckets.length} buckets`}</title>
      <line x1={0} y1={SPARK_HEIGHT - 0.5} x2={SPARK_WIDTH} y2={SPARK_HEIGHT - 0.5} className="stroke-slate-300" />
      {buckets.map((bucket, index) => {
        // Non-empty buckets get at least 2px so they remain visible.
        const height = bucket.count === 0 ? 0 : Math.max((bucket.count / max) * (SPARK_HEIGHT - 2), 2);
        return (
          <rect
            key={`${bucket.start}-${index}`}
            data-testid="sparkline-bar"
            data-count={bucket.count}
            x={index * slot + (slot - barWidth) / 2}
            y={SPARK_HEIGHT - height}
            width={barWidth}
            height={height}
            className="fill-blue-500"
          >
            <title>{`${formatDate(bucket.start)}: ${bucket.count} events`}</title>
          </rect>
        );
      })}
    </svg>
  );
}
