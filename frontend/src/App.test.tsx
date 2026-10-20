import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { TOKEN_STORAGE_KEY } from "./api/client";

function mockHealth(body: unknown, status = 200) {
  const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  }) as unknown as Response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("App", () => {
  it("shows the navigation links", async () => {
    mockHealth({ status: "ok", ai_provider: "mock", ai_provider_mock: true });
    renderApp();

    expect(screen.getByRole("link", { name: "Applications" })).toHaveAttribute("href", "/applications");
    expect(screen.getByRole("link", { name: "Incidents" })).toHaveAttribute("href", "/incidents");
    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("href", "/runs");
    await screen.findByTestId("provider-badge");
  });

  it("shows a mock provider badge from /api/v1/health", async () => {
    const fn = mockHealth({ status: "ok", ai_provider: "mock", ai_provider_mock: true });
    renderApp();

    const badge = await screen.findByTestId("provider-badge");
    expect(badge).toHaveTextContent("AI provider: mock");
    expect(fn.mock.calls[0][0]).toBe("/api/v1/health");
  });

  it("shows the real provider name when not in mock mode", async () => {
    mockHealth({ status: "ok", ai_provider: "anthropic", ai_provider_mock: false });
    renderApp();

    const badge = await screen.findByTestId("provider-badge");
    expect(badge).toHaveTextContent("AI provider: anthropic");
    expect(badge).not.toHaveTextContent("mock");
  });

  it("shows an error badge when the API is unavailable", async () => {
    mockHealth({ detail: "boom" }, 500);
    renderApp();

    expect(await screen.findByTestId("provider-badge")).toHaveTextContent("API unavailable");
  });

  it("navigates between pages", async () => {
    mockHealth({ status: "ok", ai_provider: "mock", ai_provider_mock: true });
    renderApp("/applications");
    expect(screen.getByRole("heading", { name: "Applications" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: "Runs" }));
    expect(screen.getByRole("heading", { name: "Runs" })).toBeInTheDocument();
    await screen.findByTestId("provider-badge");
  });

  it("saves and clears the admin token from the token settings", async () => {
    mockHealth({ status: "ok", ai_provider: "mock", ai_provider_mock: true });
    renderApp();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Admin token", { selector: "input" }), "fake-admin-token");
    await user.click(screen.getByRole("button", { name: "Save token" }));
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("fake-admin-token");

    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    await screen.findByTestId("provider-badge");
  });
});
