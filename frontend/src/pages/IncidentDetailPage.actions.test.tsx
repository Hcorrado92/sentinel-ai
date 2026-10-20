import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IncidentDetail, PullRequest, RCAReport, RepairAttempt } from "../api/types";
import { IncidentDetailPage } from "./IncidentDetailPage";

type MockRoute = { status?: number; body: unknown };

function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

/** Routes are keyed by "METHOD /path"; unknown routes answer 404. */
function mockApi(routes: Record<string, MockRoute>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).split("?")[0];
    const route = routes[`${init?.method ?? "GET"} ${path}`];
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

const detail: IncidentDetail = {
  id: 42,
  application_id: 1,
  application_name: "Fake Shop",
  fingerprint: "f00dbabe1234567890abcdef",
  title: "ZeroDivisionError: division by zero",
  exception_type: "ZeroDivisionError",
  severity: "high",
  status: "investigating",
  event_count: 3,
  first_seen: "2026-02-01T08:00:00",
  last_seen: "2026-02-03T09:30:00",
  resolved_at: null,
  created_at: "2026-02-01T08:00:00",
  latest_rca_status: "completed",
  latest_repair_status: null,
};

const rcaReport: RCAReport = {
  id: 5,
  incident_id: 42,
  job_id: 3,
  status: "completed",
  summary: "The discount is divided by a zero percentage.",
  report: {
    summary: "The discount is divided by a zero percentage.",
    hypotheses: [
      {
        title: "Division by the discount percentage",
        description: "apply_discount divides by percent, which can be zero.",
        confidence: 0.85,
        evidence: ["Stack trace ends in shop/discount.py:apply_discount", "Message: division by zero"],
        status: "hypothesis",
      },
      {
        title: "Missing input validation",
        description: "The percentage is not validated.",
        confidence: 0.4,
        evidence: ["No validator on the request model"],
        status: "confirmed",
      },
    ],
    suspect_locations: [{ file: "shop/discount.py", function: "apply_discount", reason: "Top frame" }],
    uncertainties: ["Only one event was analysed"],
    next_steps: ["Add a regression test for percent=0"],
  },
  provider: "mock",
  model: "mock-rca-1",
  is_mock: true,
  input_tokens: null,
  output_tokens: null,
  cost_usd: null,
  error: null,
  duration_ms: 12,
  created_at: "2026-02-03T10:00:00",
  finished_at: "2026-02-03T10:00:01",
};

const failedRepair: RepairAttempt = {
  id: 8,
  incident_id: 42,
  job_id: 4,
  repository_id: 1,
  attempt_number: 1,
  branch_name: "sentinel/fix-42-1",
  status: "failed",
  explanation: "Guard against a zero percentage.",
  diff: "--- a/shop/discount.py\n+++ b/shop/discount.py\n@@ -1 +1 @@\n-    return total / percent\n+    return total\n",
  test_command: ["python", "-m", "pytest", "-q"],
  test_output: "1 failed, 4 passed",
  test_summary: null,
  returncode: 1,
  tests_passed: false,
  regression_test_failed_before: true,
  commit_sha: null,
  stop_reason: "max_attempts_reached",
  error: null,
  pr_url: null,
  duration_ms: 900,
  input_tokens: null,
  output_tokens: null,
  cost_usd: null,
  created_at: "2026-02-03T10:05:00",
  finished_at: "2026-02-03T10:05:01",
};

const baseRoutes: Record<string, MockRoute> = {
  "GET /api/v1/incidents/42": { body: detail },
  "GET /api/v1/incidents/42/events": { body: [] },
  "GET /api/v1/incidents/42/timeline": { body: [] },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("IncidentDetailPage RCA, repairs and actions", () => {
  it("renders hypotheses with confidence percentages and the mock provider label", async () => {
    mockApi({
      ...baseRoutes,
      "GET /api/v1/incidents/42/rca": { body: rcaReport },
      "GET /api/v1/incidents/42/repairs": { body: [failedRepair] },
    });
    renderPage();

    const report = await screen.findByTestId("rca-report");
    expect(within(report).getByTestId("rca-provider")).toHaveTextContent("Mock provider");
    expect(report).toHaveTextContent("The discount is divided by a zero percentage.");

    const hypotheses = within(report).getAllByTestId("hypothesis");
    expect(hypotheses).toHaveLength(2);
    expect(within(hypotheses[0]).getByTestId("hypothesis-confidence")).toHaveTextContent("85%");
    expect(within(hypotheses[0]).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "85");
    expect(within(hypotheses[0]).getByTestId("hypothesis-badge")).toHaveTextContent("Hypothesis");
    expect(hypotheses[0]).toHaveTextContent("Stack trace ends in shop/discount.py:apply_discount");
    expect(within(hypotheses[1]).getByTestId("hypothesis-confidence")).toHaveTextContent("40%");
    expect(within(hypotheses[1]).getByTestId("hypothesis-badge")).toHaveTextContent("Confirmed");

    expect(within(report).getByTestId("suspect-location")).toHaveTextContent("shop/discount.py:apply_discount");
    expect(report).toHaveTextContent("Only one event was analysed");
    expect(report).toHaveTextContent("Add a regression test for percent=0");

    const attempt = await screen.findByTestId("repair-attempt");
    expect(within(attempt).getByTestId("repair-status")).toHaveTextContent("failed");
    expect(within(attempt).getByTestId("repair-stop-reason")).toHaveTextContent("max_attempts_reached");
    expect(attempt).toHaveTextContent("Attempt 1");
    expect(attempt).toHaveTextContent("python -m pytest -q");
    expect(within(attempt).getByText("1 failed, 4 passed")).toBeInTheDocument();
    expect(within(attempt).getByTestId("diff-viewer")).toBeInTheDocument();

    // RCA completed: repair is available; no successful repair: PR is not.
    await waitFor(() => expect(screen.getByRole("button", { name: "Run repair" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Prepare draft PR" })).toBeDisabled();
  });

  it("sends POST /rca on 'Run RCA', shows the queued state and disables 'Prepare draft PR' with an explanation", async () => {
    const fetchMock = mockApi({
      ...baseRoutes,
      "GET /api/v1/incidents/42/repairs": { body: [] },
      "POST /api/v1/incidents/42/rca": { status: 202, body: { job_id: 7, status: "queued" } },
    });
    renderPage();

    expect(await screen.findByText("No RCA has been run for this incident yet.")).toBeInTheDocument();

    const prButton = screen.getByRole("button", { name: "Prepare draft PR" });
    await waitFor(() =>
      expect(prButton).toHaveAccessibleDescription(/successful repair verified by the repository tests is required/),
    );
    expect(prButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run repair" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run repair" })).toHaveAccessibleDescription(/completed RCA is required/);

    const rcaButton = screen.getByRole("button", { name: "Run RCA" });
    await waitFor(() => expect(rcaButton).toBeEnabled());
    fireEvent.click(rcaButton);

    expect(await screen.findByTestId("action-rca-result")).toHaveTextContent("RCA job #7 queued.");
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post).toBeDefined();
    expect(String(post![0])).toBe("/api/v1/incidents/42/rca");
  });

  it("shows a 409 response from an action as an error message", async () => {
    mockApi({
      ...baseRoutes,
      "GET /api/v1/incidents/42/repairs": { body: [] },
      "POST /api/v1/incidents/42/rca": {
        status: 409,
        body: { detail: "An RCA job for this incident is already queued (job 7)" },
      },
    });
    renderPage();

    const rcaButton = await screen.findByRole("button", { name: "Run RCA" });
    await waitFor(() => expect(rcaButton).toBeEnabled());
    fireEvent.click(rcaButton);

    const error = await screen.findByTestId("action-rca-error");
    expect(error).toHaveAttribute("role", "alert");
    expect(error).toHaveTextContent("HTTP 409");
    expect(error).toHaveTextContent("An RCA job for this incident is already queued (job 7)");
  });

  it("shows the published pull request with a link", async () => {
    const pr: PullRequest = {
      id: 2,
      incident_id: 42,
      repair_attempt_id: 9,
      repository_id: 1,
      status: "published",
      branch: "sentinel/fix-42-2",
      base: "main",
      title: "Fix ZeroDivisionError in apply_discount",
      body: "RCA, diff and test results",
      github_repo: "fake-org/fake-shop",
      url: "https://github.example/fake-org/fake-shop/pull/1",
      error: null,
      draft: true,
      created_at: "2026-02-03T10:10:00",
      published_at: "2026-02-03T10:10:05",
    };
    mockApi({
      ...baseRoutes,
      "GET /api/v1/incidents/42/rca": { body: rcaReport },
      "GET /api/v1/incidents/42/repairs": {
        body: [{ ...failedRepair, id: 9, attempt_number: 2, status: "succeeded", tests_passed: true, stop_reason: null }],
      },
      "GET /api/v1/incidents/42/pull-request": { body: pr },
    });
    renderPage();

    const status = await screen.findByTestId("pr-status");
    expect(status).toHaveTextContent("published");
    expect(within(status).getByRole("link", { name: "Open draft pull request" })).toHaveAttribute("href", pr.url);
    await waitFor(() => expect(screen.getByRole("button", { name: "Prepare draft PR" })).toBeEnabled());
  });
});
