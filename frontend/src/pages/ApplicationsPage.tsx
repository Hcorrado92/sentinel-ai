import { Link } from "react-router-dom";
import type { ApplicationSummary } from "../api/types";
import { formatDate } from "../components/Badges";
import { EmptyState, ErrorState, Loading } from "../components/StatusViews";
import { useApi } from "../hooks/useApi";

/** Statuses counted as "active" (anything not resolved). */
const ACTIVE_STATUSES = ["open", "investigating", "fix_proposed", "regressed"];

export function activeCount(app: ApplicationSummary): number {
  return ACTIVE_STATUSES.reduce((sum, s) => sum + (app.incident_counts?.[s] ?? 0), 0);
}

export function ApplicationsPage() {
  const { data, error, loading, reload } = useApi<ApplicationSummary[]>("/api/v1/applications");
  const applications = Array.isArray(data) ? data : [];

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Applications</h1>
      {loading ? (
        <Loading label="Loading applications…" />
      ) : error ? (
        <ErrorState error={error} what="applications" onRetry={reload} />
      ) : applications.length === 0 ? (
        <EmptyState message="No monitored applications yet. Register one through the admin API." />
      ) : (
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-600">
              <tr>
                <th className="px-3 py-2">Application</th>
                <th className="px-3 py-2">Slug</th>
                <th className="px-3 py-2 text-right">Active incidents</th>
                <th className="px-3 py-2 text-right">Resolved incidents</th>
                <th className="px-3 py-2 text-right">Total</th>
                <th className="px-3 py-2 text-right">AI cost</th>
                <th className="px-3 py-2">Registered</th>
              </tr>
            </thead>
            <tbody>
              {applications.map((app) => (
                <tr key={app.id} data-testid={`application-row-${app.id}`} className="border-t border-slate-100">
                  <td className="px-3 py-2 font-medium">
                    <Link to={`/incidents?application_id=${app.id}`} className="text-blue-700 hover:underline">
                      {app.name}
                    </Link>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-600">{app.slug}</td>
                  <td className="px-3 py-2 text-right" data-testid="active-count">
                    {activeCount(app)}
                  </td>
                  <td className="px-3 py-2 text-right" data-testid="resolved-count">
                    {app.incident_counts?.resolved ?? 0}
                  </td>
                  <td className="px-3 py-2 text-right">{app.total_incidents}</td>
                  <td className="px-3 py-2 text-right text-slate-600" data-testid="ai-cost">
                    {typeof app.ai_cost_usd === "number" ? `$${app.ai_cost_usd.toFixed(4)}` : "not available"}
                  </td>
                  <td className="px-3 py-2 text-slate-600">{formatDate(app.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
