import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CostSummary, Run } from "../api/types";
import { RunsPage, formatCost, formatDuration } from "./RunsPage";

function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function run(overrides: Partial<Run>): Run {
  return {
    id: 1,
    type: "rca",
    status: "succeeded",
    incident_id: 7,
    incident_title: "TypeError: 'NoneType' object is not subscriptable",
    application_id: 1,
    attempts: 1,
    max_attempts: 3,
    error: null,
    result_ref: "rca_report:1",
    created_at: "2026-01-10T10:00:00",
    started_at: "2026-01-10T10:00:00",
    finished_at: "2026-01-10T10:00:01",
    duration_ms: 1200,
    result: "completed",
    stop_reason: null,
    provider: "mock",
    input_tokens: null,
    output_tokens: null,
    cost_usd: null,
    ...overrides,
  };
}

const RUNS: Run[] = [
  run({ id: 11 }),
  run({
    id: 12,
    type: "repair",
    status: "failed",
    incident_id: 8,
    incident_title: "ValueError: invalid quantity",
    attempts: 1,
    max_attempts: 1,
    duration_ms: 65_000,
    result: "stopped",
    stop_reason: "attempts_exhausted",
    provider: "anthropic",
    input_tokens: 1500,
    output_tokens: 300,
    cost_usd: 0.042,
  }),
];

const COSTS: CostSummary = {
  items: [
    {
      application_id: 1,
      application_name: "Fake Shop",
      rca_runs: 2,
      repair_attempts: 1,
      runs_without_cost: 3,
      input_tokens: null,
      output_tokens: null,
      cost_usd: null,
    },
    {
      application_id: 2,
      application_name: "Fake Billing",
      rca_runs: 1,
      repair_attempts: 2,
      runs_without_cost: 0,
      input_tokens: 1500,
      output_tokens: 300,
      cost_usd: 0.125,
    },
  ],
  input_tokens: 1500,
  output_tokens: 300,
  cost_usd: 0.125,
};

/** Fake API: /runs honours the ``type`` and ``status`` filters like the backend. */
function mockApi({ runs = RUNS, costs = COSTS, runsStatus = 200 }: { runs?: Run[]; costs?: CostSummary; runsStatus?: number } = {}) {
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/api/v1/costs") return fakeResponse(200, costs);
    if (runsStatus !== 200) return fakeResponse(runsStatus, { detail: "Internal Server Error" });
    const type = url.searchParams.get("type");
    const status = url.searchParams.get("status");
    const items = runs.filter((r) => (!type || r.type === type) && (!status || r.status === status));
    return fakeResponse(200, { items, total: items.length, limit: 100, offset: 0 });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function renderPage(path = "/runs") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/incidents/:id" element={<p>detail page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("formatters", () => {
  it("formats durations and costs", () => {
    expect(formatDuration(1200)).toBe("1.2 s");
    expect(formatDuration(850)).toBe("850 ms");
    expect(formatDuration(65_000)).toBe("1 min 5 s");
    expect(formatDuration(null)).toBe("—");
    expect(formatCost(null)).toBe("not available");
    expect(formatCost(0.042)).toBe("$0.0420");
  });
});

describe("RunsPage", () => {
  it("shows a loading indicator and then renders runs with durations and incident links", async () => {
    let resolve: (r: Response) => void = () => {};
    const fn = vi.fn((input: RequestInfo | URL, _init?: RequestInit) =>
      String(input).includes("/api/v1/costs")
        ? Promise.resolve(fakeResponse(200, COSTS))
        : new Promise<Response>((r) => {
            resolve = r;
          }),
    );
    vi.stubGlobal("fetch", fn);

    renderPage();
    expect(screen.getByText("Loading runs…")).toBeInTheDocument();

    resolve(fakeResponse(200, { items: RUNS, total: 2, limit: 100, offset: 0 }));
    const rows = await screen.findAllByTestId("run-row");
    expect(rows).toHaveLength(2);
    expect(screen.queryByText("Loading runs…")).not.toBeInTheDocument();

    const first = within(rows[0]);
    expect(first.getByTestId("run-type")).toHaveTextContent("RCA");
    expect(first.getByRole("link", { name: /NoneType/ })).toHaveAttribute("href", "/incidents/7");
    expect(first.getByTestId("status-badge")).toHaveTextContent("succeeded");
    expect(first.getByTestId("run-duration")).toHaveTextContent("1.2 s");
    expect(first.getByText("1/3")).toBeInTheDocument();
    expect(first.getByText("mock")).toBeInTheDocument();

    const second = within(rows[1]);
    expect(second.getByTestId("run-type")).toHaveTextContent("Repair");
    expect(second.getByRole("link", { name: /invalid quantity/ })).toHaveAttribute("href", "/incidents/8");
    expect(second.getByTestId("run-duration")).toHaveTextContent("1 min 5 s");
    expect(second.getByText("anthropic")).toBeInTheDocument();
    expect(second.getByText(/attempts exhausted/)).toBeInTheDocument();
    expect(second.getByText(/300 out/)).toBeInTheDocument();
  });

  it("shows 'not available' for runs without cost and the USD amount otherwise", async () => {
    mockApi();
    renderPage();

    const rows = await screen.findAllByTestId("run-row");
    expect(within(rows[0]).getByTestId("run-cost")).toHaveTextContent("not available");
    expect(within(rows[1]).getByTestId("run-cost")).toHaveTextContent("$0.0420");
  });

  it("filters by type: 'repair' hides the RCA rows", async () => {
    const fn = mockApi();
    renderPage();
    expect(await screen.findAllByTestId("run-row")).toHaveLength(2);

    await userEvent.selectOptions(screen.getByLabelText("Type"), "repair");

    await vi.waitFor(() => expect(screen.getAllByTestId("run-row")).toHaveLength(1));
    const [row] = screen.getAllByTestId("run-row");
    expect(within(row).getByTestId("run-type")).toHaveTextContent("Repair");
    expect(screen.queryByRole("link", { name: /NoneType/ })).not.toBeInTheDocument();
    const urls = fn.mock.calls.map((call) => String(call[0]));
    expect(urls.some((u) => u.includes("/api/v1/runs") && u.includes("type=repair"))).toBe(true);
  });

  it("filters by status from the URL", async () => {
    const fn = mockApi();
    renderPage("/runs?status=failed");

    const rows = await screen.findAllByTestId("run-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByTestId("status-badge")).toHaveTextContent("failed");
    expect(screen.getByLabelText("Status")).toHaveValue("failed");
    expect(fn.mock.calls.some((call) => String(call[0]).includes("status=failed"))).toBe(true);
  });

  it("shows empty states when there are no runs or no matching runs", async () => {
    mockApi({ runs: [] });
    renderPage();
    const empty = await screen.findAllByTestId("empty-state");
    expect(empty.some((e) => /No runs yet/.test(e.textContent ?? ""))).toBe(true);

    await userEvent.selectOptions(screen.getByLabelText("Type"), "rca");
    expect(await screen.findByText("No runs match the selected filters.")).toBeInTheDocument();
  });

  it("shows an error message when the runs API fails", async () => {
    mockApi({ runsStatus: 500 });
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load runs");
    expect(alert).toHaveTextContent("500");
  });

  it("shows the AI costs per application with 'not available' for mock-only applications", async () => {
    mockApi();
    renderPage();

    const rows = await screen.findAllByTestId("cost-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Fake Shop")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("cost-usd")).toHaveTextContent("not available");
    expect(within(rows[1]).getByText("Fake Billing")).toBeInTheDocument();
    expect(within(rows[1]).getByTestId("cost-usd")).toHaveTextContent("$0.1250");
    expect(screen.getByTestId("cost-total")).toHaveTextContent("$0.1250");
  });

  it("shows an empty state when there are no applications to cost", async () => {
    mockApi({ costs: { items: [], input_tokens: null, output_tokens: null, cost_usd: null } });
    renderPage();

    const empty = await screen.findAllByTestId("empty-state");
    expect(empty.some((e) => /AI costs appear here/.test(e.textContent ?? ""))).toBe(true);
  });
});
