import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Incident } from "../api/types";
import { IncidentsPage } from "./IncidentsPage";

function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => fakeResponse(status, body));
  vi.stubGlobal("fetch", fn);
  return fn;
}

function incident(overrides: Partial<Incident>): Incident {
  return {
    id: 1,
    application_id: 1,
    application_name: "Fake Shop",
    fingerprint: "a1b2c3d4e5f6a7b8c9d0",
    title: "TypeError: 'NoneType' object is not subscriptable",
    exception_type: "TypeError",
    severity: "high",
    status: "open",
    event_count: 3,
    first_seen: "2026-01-10T10:00:00",
    last_seen: "2026-01-10T12:00:00",
    resolved_at: null,
    created_at: "2026-01-10T10:00:00",
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/incidents"]}>
      <Routes>
        <Route path="/incidents" element={<IncidentsPage />} />
        <Route path="/incidents/:id" element={<p>detail page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IncidentsPage", () => {
  it("shows a loading indicator and then renders rows linking to the detail page", async () => {
    let resolve: (r: Response) => void = () => {};
    const fn = vi.fn(
      (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Promise<Response>((r) => {
          resolve = r;
        }),
    );
    vi.stubGlobal("fetch", fn);

    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("Loading incidents");

    resolve(
      fakeResponse(200, {
        items: [
          incident({ id: 7 }),
          incident({
            id: 8,
            title: "ValueError: invalid quantity",
            exception_type: "ValueError",
            severity: "medium",
            status: "resolved",
            event_count: 12,
            application_name: "Fake Billing",
          }),
        ],
        total: 2,
        limit: 100,
        offset: 0,
      }),
    );

    const rows = await screen.findAllByTestId("incident-row");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(rows).toHaveLength(2);
    expect(String(fn.mock.calls[0][0])).toContain("/api/v1/incidents");

    const first = within(rows[0]);
    expect(first.getByRole("link", { name: /NoneType/ })).toHaveAttribute("href", "/incidents/7");
    expect(first.getByText("Fake Shop")).toBeInTheDocument();
    expect(first.getByText("TypeError")).toBeInTheDocument();
    expect(first.getByTestId("severity-badge")).toHaveTextContent("high");
    expect(first.getByTestId("status-badge")).toHaveTextContent("open");
    expect(first.getByText("3")).toBeInTheDocument();

    const second = within(rows[1]);
    expect(second.getByRole("link", { name: /invalid quantity/ })).toHaveAttribute("href", "/incidents/8");
    expect(second.getByTestId("severity-badge")).toHaveTextContent("medium");
    expect(second.getByTestId("status-badge")).toHaveTextContent("resolved");
    expect(second.getByText("12")).toBeInTheDocument();
  });

  it("shows an empty-state message when there are no incidents", async () => {
    mockFetch(200, { items: [], total: 0, limit: 100, offset: 0 });
    renderPage();

    expect(await screen.findByTestId("empty-state")).toHaveTextContent("No incidents yet");
    expect(screen.queryAllByTestId("incident-row")).toHaveLength(0);
  });

  it("shows an error message when the API returns 500", async () => {
    mockFetch(500, { detail: "Internal Server Error" });
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load incidents");
    expect(alert).toHaveTextContent("500");
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
  });

  it("asks for the admin token when the API returns 401", async () => {
    mockFetch(401, { detail: "Invalid admin token" });
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(/admin token/i);
  });
});
