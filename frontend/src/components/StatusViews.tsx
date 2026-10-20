import type { ApiError } from "../api/client";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" className="py-6 text-slate-500">
      {label}
    </p>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <p data-testid="empty-state" className="rounded border border-dashed border-slate-300 bg-white p-6 text-slate-600">
      {message}
    </p>
  );
}

export function ErrorState({ error, what, onRetry }: { error: ApiError; what: string; onRetry?: () => void }) {
  const message =
    error.status === 401
      ? "Authentication required: enter a valid admin token in the token settings at the top of the page."
      : error.status === 0
        ? `Could not reach the Sentinel API while loading ${what}.`
        : `Could not load ${what} (HTTP ${error.status}): ${error.message}`;
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded border border-red-200 bg-red-50 p-4 text-red-800">
      <span>{message}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="rounded border border-red-300 bg-white px-3 py-1 text-sm hover:bg-red-100"
        >
          Retry
        </button>
      )}
    </div>
  );
}
