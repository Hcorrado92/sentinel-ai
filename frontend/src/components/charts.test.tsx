import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { BucketCount } from "../api/types";
import { FrequencySparkline, SeverityBreakdown, countBySeverity } from "./Charts";

const buckets: BucketCount[] = [
  { start: "2026-01-10T10:00:00Z", count: 2 },
  { start: "2026-01-10T11:00:00Z", count: 0 },
  { start: "2026-01-10T12:00:00Z", count: 5 },
  { start: "2026-01-10T13:00:00Z", count: 1 },
];

describe("FrequencySparkline", () => {
  it("renders one SVG bar per bucket with an accessible title", () => {
    render(<FrequencySparkline buckets={buckets} title="Events per hour" />);

    const chart = screen.getByRole("img", { name: /Events per hour: 8 events in 4 buckets/ });
    expect(chart.tagName.toLowerCase()).toBe("svg");
    const bars = screen.getAllByTestId("sparkline-bar");
    expect(bars).toHaveLength(buckets.length);
    expect(bars.map((bar) => bar.getAttribute("data-count"))).toEqual(["2", "0", "5", "1"]);

    // The tallest bucket gets the tallest bar; an empty bucket has no height.
    const heights = bars.map((bar) => Number(bar.getAttribute("height")));
    expect(Math.max(...heights)).toBe(heights[2]);
    expect(heights[1]).toBe(0);
    expect(bars[2].querySelector("title")).toHaveTextContent("5 events");
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
  });

  it("shows an empty state when there is no data", () => {
    render(<FrequencySparkline buckets={[]} />);

    expect(screen.getByTestId("empty-state")).toHaveTextContent("No events to chart yet.");
    expect(screen.queryByTestId("frequency-sparkline")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("sparkline-bar")).toHaveLength(0);
  });
});

describe("SeverityBreakdown", () => {
  it("renders one bar per severity level with counts and an accessible title", () => {
    const counts = countBySeverity([
      { severity: "critical" },
      { severity: "high" },
      { severity: "high" },
      { severity: "low" },
    ]);
    expect(counts).toEqual({ low: 1, medium: 0, high: 2, critical: 1 });

    render(<SeverityBreakdown counts={counts} title="Severity of listed incidents" />);

    expect(screen.getByRole("img", { name: /Severity of listed incidents: low 1, medium 0, high 2, critical 1/ })).toBeInTheDocument();
    const bars = screen.getAllByTestId("severity-bar");
    expect(bars.map((bar) => bar.getAttribute("data-severity"))).toEqual(["low", "medium", "high", "critical"]);
    expect(bars.map((bar) => bar.getAttribute("data-count"))).toEqual(["1", "0", "2", "1"]);

    const widths = bars.map((bar) => Number(bar.querySelector("rect")?.getAttribute("width")));
    expect(widths[2]).toBeGreaterThan(widths[0]);
    expect(widths[1]).toBe(0);
  });

  it("shows an empty state when every count is zero", () => {
    render(<SeverityBreakdown counts={countBySeverity([])} />);

    expect(screen.getByTestId("empty-state")).toHaveTextContent("No incidents to break down by severity.");
    expect(screen.queryByTestId("severity-breakdown")).not.toBeInTheDocument();
  });
});
