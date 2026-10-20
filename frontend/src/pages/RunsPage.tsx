import { Link, useSearchParams } from "react-router-dom";
import type { CostSummary, RunPage } from "../api/types";
import { StatusBadge, formatDate } from "../components/Badges";
import { EmptyState, ErrorState, Loading } from "../components/StatusViews";
import { useApi } from "../hooks/useApi";

const RUN_TYPES = [
  { value: "", label: "All types" },
  { value: "rca", label: "RCA" },
  { value: "repair", label: "Repair" },
];

const RUN_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"];

const NOT_AVAILABLE = "not available";

/** Formats a duration in milliseconds, e.g. 850 → "850 ms", 1200 → "1.2 s". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

/** USD amount reported by the provider, or "not available" (e.g. mock runs). */
export function formatCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return NOT_AVAILABLE;
  return `$${usd.toFixed(4)}`;
}

function formatTokens(input: number | null | undefined, output: number | null | undefined): string {
  if ((input === null || input === undefined) && (output === null || output === undefined)) return NOT_AVAILABLE;
  return `${(input ?? 0).toLocaleString()} in / ${(output ?? 0).toLocaleString()} out`;
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-700">
      {label}
      <select
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-slate-300 bg-white px-2 py-1"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function CostsSummary() {
  const { data, error, loading, reload } = useApi<CostSummary>("/api/v1/costs");
  const items = data?.items;
  const costs = Array.isArray(items) ? items : [];

  return (
    <section className="mb-8">
      <h2 className="mb-1 text-lg font-semibold">AI costs by application</h2>
      <p className="mb-3 text-sm text-slate-600">
        Tokens and USD as reported by the AI provider; "{NOT_AVAILABLE}" when no run reported them (mock provider).
      </p>
      {loading ? (
        <Loading label="Loading AI costs…" />
      ) : error ? (
        <ErrorState error={error} what="AI costs" onRetry={reload} />
      ) : costs.length === 0 ? (
        <EmptyState message="No applications yet. AI costs appear here once applications have RCA or repair runs." />
      ) : (
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-600">
              <tr>
                <th className="px-3 py-2">Application</th>
                <th className="px-3 py-2 text-right">RCA runs</th>
                <th className="px-3 py-2 text-right">Repair attempts</th>
                <th className="px-3 py-2">Tokens</th>
                <th className="px-3 py-2 text-right">Cost (USD)</th>
              </tr>
            </thead>
            <tbody>
              {costs.map((cost) => (
                <tr key={cost.application_id} data-testid="cost-row" className="border-t border-slate-100">
                  <td className="px-3 py-2">{cost.application_name}</td>
                  <td className="px-3 py-2 text-right">{cost.rca_runs}</td>
                  <td className="px-3 py-2 text-right">{cost.repair_attempts}</td>
                  <td className="px-3 py-2 text-slate-600">{formatTokens(cost.input_tokens, cost.output_tokens)}</td>
                  <td className="px-3 py-2 text-right" data-testid="cost-usd">
                    {formatCost(cost.cost_usd)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-slate-200 font-medium">
                <td className="px-3 py-2" colSpan={3}>
                  Total
                </td>
                <td className="px-3 py-2 text-slate-600">{formatTokens(data?.input_tokens, data?.output_tokens)}</td>
                <td className="px-3 py-2 text-right" data-testid="cost-total">
                  {formatCost(data?.cost_usd)}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </section>
  );
}

export function RunsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const typeParam = searchParams.get("type") ?? "";
  const statusParam = searchParams.get("status") ?? "";
  const type = RUN_TYPES.some((t) => t.value === typeParam) ? typeParam : "";
  const status = RUN_STATUSES.includes(statusParam) ? statusParam : "";

  const query = new URLSearchParams({ limit: "100" });
  if (type) query.set("type", type);
  if (status) query.set("status", status);
  const { data, error, loading, reload } = useApi<RunPage>(`/api/v1/runs?${query.toString()}`);
  const items = data?.items;
  const runs = Array.isArray(items) ? items : [];

  const setFilter = (name: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value);
    else next.delete(name);
    setSearchParams(next);
  };

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Runs</h1>
      <CostsSummary />

      <h2 className="mb-3 text-lg font-semibold">Run history</h2>
      <div className="mb-3 flex flex-wrap gap-4">
        <FilterSelect label="Type" value={type} options={RUN_TYPES} onChange={(v) => setFilter("type", v)} />
        <FilterSelect
          label="Status"
          value={status}
          options={[{ value: "", label: "All statuses" }, ...RUN_STATUSES.map((s) => ({ value: s, label: s }))]}
          onChange={(v) => setFilter("status", v)}
        />
      </div>
      {loading ? (
        <Loading label="Loading runs…" />
      ) : error ? (
        <ErrorState error={error} what="runs" onRetry={reload} />
      ) : runs.length === 0 ? (
        <EmptyState
          message={
            type || status
              ? "No runs match the selected filters."
              : "No runs yet. RCA and repair runs requested from an incident will appear here."
          }
        />
      ) : (
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-600">
              <tr>
                <th className="px-3 py-2">Run</th>
                <th className="px-3 py-2">Type</th>
                <th className="px-3 py-2">Incident</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2 text-right">Duration</th>
                <th className="px-3 py-2 text-right">Attempts</th>
                <th className="px-3 py-2">Provider</th>
                <th className="px-3 py-2">Tokens</th>
                <th className="px-3 py-2 text-right">Cost (USD)</th>
                <th className="px-3 py-2">Created</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} data-testid="run-row" className="border-t border-slate-100 align-top">
                  <td className="px-3 py-2 font-mono text-xs">#{run.id}</td>
                  <td className="px-3 py-2" data-testid="run-type">
                    {run.type === "rca" ? "RCA" : run.type === "repair" ? "Repair" : run.type}
                  </td>
                  <td className="px-3 py-2">
                    {run.incident_id !== null ? (
                      <Link to={`/incidents/${run.incident_id}`} className="text-blue-700 hover:underline">
                        {run.incident_title || `Incident #${run.incident_id}`}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={run.status} />
                    {run.result && <div className="text-xs text-slate-500">result: {run.result.replace(/_/g, " ")}</div>}
                    {run.stop_reason && (
                      <div className="text-xs text-slate-500">stopped: {run.stop_reason.replace(/_/g, " ")}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right" data-testid="run-duration">
                    {formatDuration(run.duration_ms)}
                  </td>
                  <td className="px-3 py-2 text-right">{`${run.attempts}/${run.max_attempts}`}</td>
                  <td className="px-3 py-2">{run.provider ?? "—"}</td>
                  <td className="px-3 py-2 text-slate-600">{formatTokens(run.input_tokens, run.output_tokens)}</td>
                  <td className="px-3 py-2 text-right" data-testid="run-cost">
                    {formatCost(run.cost_usd)}
                  </td>
                  <td className="px-3 py-2 text-slate-600">{formatDate(run.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && data.total > runs.length && (
            <p className="border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              Showing the {runs.length} most recent of {data.total} runs.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
