import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IncidentDetail, IncidentEvent, TimelineEntry } from "../api/types";
import { IncidentDetailPage } from "./IncidentDetailPage";

function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const detail: IncidentDetail = {
  id: 42,
  application_id: 1,
  application_name: "Fake Shop",
  fingerprint: "f00dbabe1234567890abcdef",
  title: "ZeroDivisionError: division by zero",
  exception_type: "ZeroDivisionError",
  severity: "critical",
  status: "regressed",
  event_count: 17,
  first_seen: "2026-02-01T08:00:00",
  last_seen: "2026-02-03T09:30:00",
  resolved_at: null,
  created_at: "2026-02-01T08:00:00",
  latest_rca_status: null,
  latest_repair_status: null,
};

const events: IncidentEvent[] = [
  {
    id: 99,
    event_id: "evt-fake-1",
    timestamp: "2026-02-03T09:30:00",
    received_at: "2026-02-03T09:30:01",
    exception_type: "ZeroDivisionError",
    message: "division by zero",
    stack_trace: 'File "shop/discount.py", in apply_discount\nAuthorization: Bearer [REDACTED]',
    environment: "staging",
    correlation_id: "corr-fake-1",
    metadata: {},
  },
];

// Deliberately out of order: the page must sort them chronologically.
const timeline: TimelineEntry[] = [
  { id: 3, type: "incident_regressed", timestamp: "2026-02-03T09:30:02", actor: "system", details: {} },
  { id: 1, type: "event_received", timestamp: "2026-02-01T08:00:00", actor: "ingest", details: { event_id: "evt-0" } },
  { id: 2, type: "incident_grouped", timestamp: "2026-02-01T08:00:01", actor: "system", details: {} },
];

function mockApi(routes: Record<string, { status?: number; body: unknown }>) {
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(input).split("?")[0];
    const route = routes[path];
    return route ? fakeResponse(route.status ?? 200, route.body) : fakeResponse(404, { detail: "Not found" });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function renderPage(id = "42") {
  return render(
    <MemoryRouter initialEntries={[`/incidents/${id}`]}>
      <Routes>
        <Route path="/incidents/:id" element={<IncidentDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IncidentDetailPage", () => {
  it("renders severity, status, event count and the timeline in chronological order", async () => {
    mockApi({
      "/api/v1/incidents/42": { body: detail },
      "/api/v1/incidents/42/events": { body: events },
      "/api/v1/incidents/42/timeline": { body: timeline },
    });
    renderPage();

    expect(screen.getByRole("status")).toHaveTextContent("Loading incident");
    expect(await screen.findByRole("heading", { name: detail.title })).toBeInTheDocument();
    expect(screen.getByTestId("incident-severity")).toHaveTextContent("critical");
    expect(screen.getByTestId("incident-status")).toHaveTextContent("regressed");
    expect(screen.getByTestId("incident-event-count")).toHaveTextContent("17");
    expect(screen.getByTestId("incident-fingerprint")).toHaveTextContent(detail.fingerprint);

    const entries = await screen.findAllByTestId("timeline-entry");
    expect(entries.map((e) => e.querySelector("p")?.textContent)).toEqual([
      "Event received",
      "Grouped into incident",
      "Regression: incident reopened",
    ]);

    const [event] = await screen.findAllByTestId("event-item");
    expect(event).toHaveTextContent("division by zero");
    expect(event).toHaveTextContent("Bearer [REDACTED]");
    expect(event).toHaveTextContent("staging");
  });

  it("shows empty states when there are no events or timeline entries", async () => {
    mockApi({
      "/api/v1/incidents/42": { body: detail },
      "/api/v1/incidents/42/events": { body: [] },
      "/api/v1/incidents/42/timeline": { body: [] },
    });
    renderPage();

    expect(await screen.findByText("No events recorded for this incident.")).toBeInTheDocument();
    expect(await screen.findByText("No timeline entries yet.")).toBeInTheDocument();
  });

  it("shows an error when the incident does not exist", async () => {
    mockApi({});
    renderPage("5");

    expect(await screen.findByRole("alert")).toHaveTextContent("404");
    expect(screen.getByRole("heading", { name: "Incident #5" })).toBeInTheDocument();
  });

  it("asks for the admin token on 401", async () => {
    mockApi({ "/api/v1/incidents/42": { status: 401, body: { detail: "Invalid admin token" } } });
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(/admin token/i);
  });
});
