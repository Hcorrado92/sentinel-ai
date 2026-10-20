import { Link, useSearchParams } from "react-router-dom";
import type { IncidentPage } from "../api/types";
import { SeverityBadge, StatusBadge, formatDate } from "../components/Badges";
import { EmptyState, ErrorState, Loading } from "../components/StatusViews";
import { useApi } from "../hooks/useApi";

export function IncidentsPage() {
  const [searchParams] = useSearchParams();
  const applicationId = searchParams.get("application_id");
  const query = new URLSearchParams({ limit: "100" });
  if (applicationId && /^\d+$/.test(applicationId)) query.set("application_id", applicationId);

  const { data, error, loading, reload } = useApi<IncidentPage>(`/api/v1/incidents?${query.toString()}`);
  const items = data?.items;
  const incidents = Array.isArray(items) ? items : [];

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Incidents</h1>
      {query.has("application_id") && (
        <p className="mb-3 text-sm text-slate-600">
          Showing incidents of application #{applicationId}.{" "}
          <Link to="/incidents" className="text-blue-700 hover:underline">
            Show all applications
          </Link>
        </p>
      )}
      {loading ? (
        <Loading label="Loading incidents…" />
      ) : error ? (
        <ErrorState error={error} what="incidents" onRetry={reload} />
      ) : incidents.length === 0 ? (
        <EmptyState message="No incidents yet. Errors reported to the ingestion endpoint will appear here." />
      ) : (
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-600">
              <tr>
                <th className="px-3 py-2">Incident</th>
                <th className="px-3 py-2">Application</th>
                <th className="px-3 py-2">Exception</th>
                <th className="px-3 py-2">Severity</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2 text-right">Events</th>
                <th className="px-3 py-2">First seen</th>
                <th className="px-3 py-2">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((incident) => (
                <tr key={incident.id} data-testid="incident-row" className="border-t border-slate-100 align-top">
                  <td className="px-3 py-2">
                    <Link to={`/incidents/${incident.id}`} className="font-medium text-blue-700 hover:underline">
                      {incident.title}
                    </Link>
                    <div className="font-mono text-xs text-slate-500" title="Fingerprint">
                      {incident.fingerprint.slice(0, 12)}
                    </div>
                  </td>
                  <td className="px-3 py-2">{incident.application_name}</td>
                  <td className="px-3 py-2 font-mono text-xs">{incident.exception_type}</td>
                  <td className="px-3 py-2">
                    <SeverityBadge severity={incident.severity} />
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={incident.status} />
                  </td>
                  <td className="px-3 py-2 text-right">{incident.event_count}</td>
                  <td className="px-3 py-2 text-slate-600">{formatDate(incident.first_seen)}</td>
                  <td className="px-3 py-2 text-slate-600">{formatDate(incident.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && data.total > incidents.length && (
            <p className="border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              Showing the {incidents.length} most recent of {data.total} incidents.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
