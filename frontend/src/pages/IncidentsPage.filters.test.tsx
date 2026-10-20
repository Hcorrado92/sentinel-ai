import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApplicationSummary, Incident } from "../api/types";
import { IncidentsPage } from "./IncidentsPage";

function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const incidents: Incident[] = [
  {
    id: 7,
    application_id: 1,
    application_name: "Fake Shop",
    fingerprint: "a1b2c3d4e5f6a7b8c9d0",
    title: "TypeError: 'NoneType' object is not subscriptable",
    exception_type: "TypeError",
    severity: "critical",
    status: "open",
    event_count: 3,
    first_seen: "2026-01-10T10:00:00",
    last_seen: "2026-01-10T12:00:00",
    resolved_at: null,
    created_at: "2026-01-10T10:00:00",
  },
];

const applications: ApplicationSummary[] = [
  {
    id: 1,
    name: "Fake Shop",
    slug: "fake-shop",
    created_at: "2026-01-01T00:00:00",
    budget_usd: null,
    monthly_spend_usd: 0,
    incident_counts: { open: 1 },
    total_incidents: 1,
  },
  {
    id: 2,
    name: "Fake Billing",
    slug: "fake-billing",
    created_at: "2026-01-01T00:00:00",
    budget_usd: null,
    monthly_spend_usd: 0,
    incident_counts: {},
    total_incidents: 0,
  },
];

function mockApi() {
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(input).split("?")[0];
    if (path === "/api/v1/applications") return fakeResponse(200, applications);
    if (path === "/api/v1/incidents") {
      return fakeResponse(200, { items: incidents, total: incidents.length, limit: 100, offset: 0 });
    }
    return fakeResponse(404, { detail: "Not found" });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function incidentRequests(fn: ReturnType<typeof mockApi>): URLSearchParams[] {
  return fn.mock.calls
    .map(([input]) => String(input))
    .filter((url) => url.startsWith("/api/v1/incidents?"))
    .map((url) => new URLSearchParams(url.split("?")[1]));
}

function LocationDisplay() {
  const location = useLocation();
  return <p data-testid="location">{location.search}</p>;
}

function renderPage(entry = "/incidents") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/incidents"
          element={
            <>
              <IncidentsPage />
              <LocationDisplay />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IncidentsPage filters", () => {
  it("updates the URL query and requests severity=<value> when the severity filter changes", async () => {
    const fn = mockApi();
    renderPage();
    await screen.findAllByTestId("incident-row");
    expect(incidentRequests(fn)[0].has("severity")).toBe(false);

    fireEvent.change(screen.getByLabelText("Severity"), { target: { value: "critical" } });

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("severity=critical"));
    await waitFor(() => expect(incidentRequests(fn).some((params) => params.get("severity") === "critical")).toBe(true));
    expect(screen.getByLabelText("Severity")).toHaveValue("critical");
    await screen.findAllByTestId("incident-row");

    // Choosing "All severities" removes the parameter again.
    fireEvent.change(screen.getByLabelText("Severity"), { target: { value: "" } });
    await waitFor(() => expect(screen.getByTestId("location")).not.toHaveTextContent("severity"));
    const last = incidentRequests(fn).at(-1)!;
    expect(last.has("severity")).toBe(false);
  });

  it("passes the status, application and exception type search to the API", async () => {
    const fn = mockApi();
    renderPage();
    await screen.findAllByTestId("incident-row");

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "resolved" } });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("status=resolved"));

    // Application options come from the applications API.
    expect(await screen.findByRole("option", { name: "Fake Billing" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Application"), { target: { value: "2" } });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("application_id=2"));

    fireEvent.change(screen.getByLabelText("Exception type"), { target: { value: " TypeErr " } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("q=TypeErr"));

    await waitFor(() => {
      const last = incidentRequests(fn).at(-1)!;
      expect(last.get("status")).toBe("resolved");
      expect(last.get("application_id")).toBe("2");
      expect(last.get("q")).toBe("TypeErr");
    });

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^$/));
    await waitFor(() => {
      const last = incidentRequests(fn).at(-1)!;
      expect([...last.keys()]).toEqual(["limit"]);
    });
    expect(screen.getByLabelText("Exception type")).toHaveValue("");
  });

  it("reads filters from the initial URL and ignores invalid values", async () => {
    const fn = mockApi();
    renderPage("/incidents?status=active&severity=huge&application_id=abc&q=KeyError");
    await screen.findAllByTestId("incident-row");

    const first = incidentRequests(fn)[0];
    expect(first.get("status")).toBe("active");
    expect(first.get("q")).toBe("KeyError");
    expect(first.has("severity")).toBe(false);
    expect(first.has("application_id")).toBe(false);
    expect(screen.getByLabelText("Status")).toHaveValue("active");
    expect(screen.getByLabelText("Exception type")).toHaveValue("KeyError");
  });

  it("shows the severity breakdown of the listed incidents", async () => {
    mockApi();
    renderPage();

    const chart = await screen.findByTestId("severity-breakdown");
    expect(chart).toHaveAttribute("role", "img");
    const critical = screen.getAllByTestId("severity-bar").find((bar) => bar.getAttribute("data-severity") === "critical");
    expect(critical).toHaveAttribute("data-count", "1");
  });
});
