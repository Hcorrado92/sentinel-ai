import { useEffect, useId, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { apiFetch } from "../api/client";
import type { ApplicationSummary, IncidentPage } from "../api/types";
import { SeverityBadge, StatusBadge, formatDate } from "../components/Badges";
import { SEVERITY_ORDER, SeverityBreakdown, countBySeverity } from "../components/Charts";
import { EmptyState, ErrorState, Loading } from "../components/StatusViews";
import { useApi } from "../hooks/useApi";

const STATUS_OPTIONS: [string, string][] = [
  ["", "All statuses"],
  ["active", "Active (not resolved)"],
  ["resolved", "Resolved"],
  ["open", "Open"],
  ["investigating", "Investigating"],
  ["fix_proposed", "Fix proposed"],
  ["regressed", "Regressed"],
];

const STATUS_VALUES = new Set(STATUS_OPTIONS.map(([value]) => value).filter(Boolean));
const SEVERITY_VALUES = new Set<string>(SEVERITY_ORDER);
const MAX_SEARCH_LENGTH = 200;
const FILTER_KEYS = ["application_id", "status", "severity", "q"] as const;

type FilterKey = (typeof FILTER_KEYS)[number];

/** Valid filters from the URL query; invalid values are ignored rather than sent to the API. */
function readFilters(params: URLSearchParams): Record<FilterKey, string> {
  const applicationId = params.get("application_id") ?? "";
  const status = params.get("status") ?? "";
  const severity = params.get("severity") ?? "";
  return {
    application_id: /^\d+$/.test(applicationId) ? applicationId : "",
    status: STATUS_VALUES.has(status) ? status : "",
    severity: SEVERITY_VALUES.has(severity) ? severity : "",
    q: (params.get("q") ?? "").trim().slice(0, MAX_SEARCH_LENGTH),
  };
}

/**
 * Applications for the filter, loaded once the incidents request has settled
 * so the incident list is always the first request of the page.
 */
function useApplications(enabled: boolean): ApplicationSummary[] | null {
  const [applications, setApplications] = useState<ApplicationSummary[] | null>(null);
  useEffect(() => {
    if (!enabled || applications !== null) return;
    let active = true;
    apiFetch<ApplicationSummary[]>("/api/v1/applications")
      .then((body) => {
        if (active) setApplications(Array.isArray(body) ? body : []);
      })
      .catch(() => {
        // The filter falls back to the current id; the page shows API errors already.
        if (active) setApplications([]);
      });
    return () => {
      active = false;
    };
  }, [enabled, applications]);
  return applications;
}

const selectClass = "rounded border border-slate-300 bg-white px-2 py-1 text-sm";
const labelClass = "text-xs text-slate-600";

export function IncidentsPage() {
  const idPrefix = useId();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = readFilters(searchParams);
  const query = new URLSearchParams({ limit: "100" });
  for (const key of FILTER_KEYS) {
    if (filters[key]) query.set(key, filters[key]);
  }
  const filtered = FILTER_KEYS.some((key) => filters[key]);

  const { data, error, loading, reload } = useApi<IncidentPage>(`/api/v1/incidents?${query.toString()}`);
  const items = data?.items;
  const incidents = Array.isArray(items) ? items : [];

  const [settled, setSettled] = useState(false);
  useEffect(() => {
    if (!loading) setSettled(true);
  }, [loading]);
  const applications = useApplications(settled);

  const [search, setSearch] = useState(filters.q);
  useEffect(() => setSearch(filters.q), [filters.q]);

  function setFilter(key: FilterKey, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next);
  }

  function onSearch(event: FormEvent) {
    event.preventDefault();
    setFilter("q", search.trim().slice(0, MAX_SEARCH_LENGTH));
  }

  function clearFilters() {
    const next = new URLSearchParams(searchParams);
    for (const key of FILTER_KEYS) next.delete(key);
    setSearchParams(next);
  }

  const applicationOptions = applications ?? [];
  const unknownApplication =
    filters.application_id && !applicationOptions.some((a) => String(a.id) === filters.application_id);

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Incidents</h1>
      <div
        role="search"
        aria-label="Incident filters"
        className="mb-4 flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-3"
      >
        <div className="flex flex-col">
          <label htmlFor={`${idPrefix}-application`} className={labelClass}>
            Application
          </label>
          <select
            id={`${idPrefix}-application`}
            className={selectClass}
            value={filters.application_id}
            onChange={(e) => setFilter("application_id", e.target.value)}
          >
            <option value="">All applications</option>
            {applicationOptions.map((application) => (
              <option key={application.id} value={String(application.id)}>
                {application.name}
              </option>
            ))}
            {unknownApplication && <option value={filters.application_id}>{`Application #${filters.application_id}`}</option>}
          </select>
        </div>
        <div className="flex flex-col">
          <label htmlFor={`${idPrefix}-status`} className={labelClass}>
            Status
          </label>
          <select
            id={`${idPrefix}-status`}
            className={selectClass}
            value={filters.status}
            onChange={(e) => setFilter("status", e.target.value)}
          >
            {STATUS_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col">
          <label htmlFor={`${idPrefix}-severity`} className={labelClass}>
            Severity
          </label>
          <select
            id={`${idPrefix}-severity`}
            className={selectClass}
            value={filters.severity}
            onChange={(e) => setFilter("severity", e.target.value)}
          >
            <option value="">All severities</option>
            {SEVERITY_ORDER.map((severity) => (
              <option key={severity} value={severity}>
                {severity}
              </option>
            ))}
          </select>
        </div>
        <form onSubmit={onSearch} className="flex items-end gap-2">
          <div className="flex flex-col">
            <label htmlFor={`${idPrefix}-search`} className={labelClass}>
              Exception type
            </label>
            <input
              id={`${idPrefix}-search`}
              type="search"
              value={search}
              maxLength={MAX_SEARCH_LENGTH}
              placeholder="e.g. TypeError"
              onChange={(e) => setSearch(e.target.value)}
              className={selectClass}
            />
          </div>
          <button type="submit" className="rounded bg-slate-900 px-3 py-1 text-sm font-medium text-white hover:bg-slate-700">
            Search
          </button>
        </form>
        {filtered && (
          <button type="button" onClick={clearFilters} className="text-sm text-blue-700 hover:underline">
            Clear filters
          </button>
        )}
      </div>
      {loading ? (
        <Loading label="Loading incidents…" />
      ) : error ? (
        <ErrorState error={error} what="incidents" onRetry={reload} />
      ) : incidents.length === 0 ? (
        <EmptyState
          message={
            filtered
              ? "No incidents match the current filters."
              : "No incidents yet. Errors reported to the ingestion endpoint will appear here."
          }
        />
      ) : (
        <>
          <div className="mb-4 rounded border border-slate-200 bg-white p-3">
            <h2 className="mb-2 text-sm font-semibold">Severity of the listed incidents</h2>
            <SeverityBreakdown counts={countBySeverity(incidents)} title="Severity of the listed incidents" />
          </div>
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
        </>
      )}
    </section>
  );
}
