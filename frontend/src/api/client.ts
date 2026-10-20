/** Typed fetch wrapper for the Sentinel API. */

export const TOKEN_STORAGE_KEY = "sentinel_admin_token";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function getToken(): string | null {
  try {
    const token = window.localStorage.getItem(TOKEN_STORAGE_KEY);
    return token && token.trim() ? token : null;
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token.trim());
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
}

function parseBody(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  if (typeof body === "string" && body) return body;
  return `Request failed with status ${status}`;
}

export interface ApiRequestInit extends Omit<RequestInit, "headers"> {
  headers?: Record<string, string>;
}

export async function apiFetch<T>(path: string, init: ApiRequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...init.headers };
  if (init.body !== undefined && !("Content-Type" in headers)) {
    headers["Content-Type"] = "application/json";
  }
  const token = getToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? err.message : "Network error");
  }

  const body = parseBody(await response.text());
  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(response.status, body), body);
  }
  return body as T;
}

export interface Health {
  status: string;
  ai_provider: string;
  ai_provider_mock: boolean;
}

export function getHealth(): Promise<Health> {
  return apiFetch<Health>("/api/v1/health");
}
