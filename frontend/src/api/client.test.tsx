import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, TOKEN_STORAGE_KEY, apiFetch, clearToken, getToken, setToken } from "./client";

function fakeResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  } as unknown as Response;
}

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => fakeResponse(status, body));
  vi.stubGlobal("fetch", fn);
  return fn;
}

function sentHeaders(fn: ReturnType<typeof mockFetch>): Record<string, string> {
  const init = fn.mock.calls[0][1] as RequestInit;
  return init.headers as Record<string, string>;
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("apiFetch", () => {
  it("attaches the stored admin token as a Bearer header", async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "fake-admin-token");
    const fn = mockFetch(200, { items: [] });

    const data = await apiFetch<{ items: unknown[] }>("/api/v1/applications");

    expect(data).toEqual({ items: [] });
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0][0]).toBe("/api/v1/applications");
    expect(sentHeaders(fn)["Authorization"]).toBe("Bearer fake-admin-token");
  });

  it("omits the Authorization header when no token is stored", async () => {
    const fn = mockFetch(200, { status: "ok" });
    await apiFetch("/api/v1/health");
    expect(sentHeaders(fn)["Authorization"]).toBeUndefined();
  });

  it("throws ApiError with the status on a 401 response", async () => {
    mockFetch(401, { detail: "Invalid admin token" });

    const error = await apiFetch("/api/v1/incidents").catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).message).toBe("Invalid admin token");
  });

  it("throws ApiError with status 0 on network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const error = await apiFetch("/api/v1/health").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
  });
});

describe("token storage", () => {
  it("saves and clears the token", () => {
    expect(getToken()).toBeNull();
    setToken("  fake-token  ");
    expect(getToken()).toBe("fake-token");
    clearToken();
    expect(getToken()).toBeNull();
  });
});
