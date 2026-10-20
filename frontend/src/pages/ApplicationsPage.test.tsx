import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApplicationSummary } from "../api/types";
import { ApplicationsPage } from "./ApplicationsPage";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  }) as unknown as Response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function counts(overrides: Record<string, number>): Record<string, number> {
  return { open: 0, investigating: 0, fix_proposed: 0, resolved: 0, regressed: 0, ...overrides };
}

const applications: ApplicationSummary[] = [
  {
    id: 1,
    name: "Fake Shop",
    slug: "fake-shop",
    created_at: "2026-01-01T00:00:00",
    budget_usd: null,
    monthly_spend_usd: 0,
    incident_counts: counts({ open: 2, investigating: 1, regressed: 1, resolved: 4 }),
    total_incidents: 8,
  },
  {
    id: 2,
    name: "Fake Billing",
    slug: "fake-billing",
    created_at: "2026-01-02T00:00:00",
    budget_usd: 10,
    monthly_spend_usd: 0,
    incident_counts: counts({ resolved: 3 }),
    total_incidents: 3,
  },
];

function renderPage() {
  return render(
    <MemoryRouter>
      <ApplicationsPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ApplicationsPage", () => {
  it("lists applications with their open and resolved incident counts", async () => {
    const fn = mockFetch(200, applications);
    renderPage();

    expect(screen.getByRole("status")).toHaveTextContent("Loading applications");
    const shop = within(await screen.findByTestId("application-row-1"));
    expect(shop.getByRole("link", { name: "Fake Shop" })).toHaveAttribute("href", "/incidents?application_id=1");
    expect(shop.getByTestId("active-count")).toHaveTextContent("4");
    expect(shop.getByTestId("resolved-count")).toHaveTextContent("4");

    const billing = within(screen.getByTestId("application-row-2"));
    expect(billing.getByText("Fake Billing")).toBeInTheDocument();
    expect(billing.getByTestId("active-count")).toHaveTextContent("0");
    expect(billing.getByTestId("resolved-count")).toHaveTextContent("3");

    expect(fn.mock.calls[0][0]).toBe("/api/v1/applications");
  });

  it("shows an empty state when no applications exist", async () => {
    mockFetch(200, []);
    renderPage();
    expect(await screen.findByTestId("empty-state")).toHaveTextContent("No monitored applications yet");
  });

  it("asks for the admin token on 401", async () => {
    mockFetch(401, { detail: "Invalid admin token" });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/admin token/i);
  });
});
