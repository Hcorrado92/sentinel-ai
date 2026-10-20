import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import type { IncidentDetail, IncidentEvent, TimelineEntry } from "../api/types";
import { SeverityBadge, StatusBadge, formatDate } from "../components/Badges";
import { EmptyState, ErrorState, Loading } from "../components/StatusViews";
import { useApi } from "../hooks/useApi";

const LATEST_EVENTS = 10;

const TIMELINE_LABELS: Record<string, string> = {
  event_received: "Event received",
  incident_grouped: "Grouped into incident",
  incident_regressed: "Regression: incident reopened",
};

const TIMELINE_DOTS: Record<string, string> = {
  rca: "bg-blue-500",
  repair: "bg-violet-500",
  pr: "bg-emerald-500",
  incident: "bg-orange-500",
};

function timelineLabel(type: string): string {
  if (TIMELINE_LABELS[type]) return TIMELINE_LABELS[type];
  const text = type.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function timelineDot(type: string): string {
  return TIMELINE_DOTS[type.split("_")[0]] ?? "bg-slate-400";
}

function summarizeDetails(details: Record<string, unknown>): string {
  return Object.entries(details ?? {})
    .filter(([, value]) => value !== null && typeof value !== "object")
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(" · ");
}

/** Timeline entries sorted oldest first (ties broken by id). */
export function chronological(entries: TimelineEntry[]): TimelineEntry[] {
  return [...entries].sort((a, b) => {
    const diff = new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime();
    return diff !== 0 && !Number.isNaN(diff) ? diff : a.id - b.id;
  });
}

function Field({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div>
      <dt className="text-xs uppercase text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

function IncidentHeader({ incident }: { incident: IncidentDetail }) {
  return (
    <header className="rounded border border-slate-200 bg-white p-4">
      <h1 className="text-xl font-semibold">{incident.title}</h1>
      <p className="mt-1 text-sm text-slate-600">
        {incident.application_name} · <span className="font-mono">{incident.exception_type}</span>
      </p>
      <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Field label="Severity" testId="incident-severity">
          <SeverityBadge severity={incident.severity} />
        </Field>
        <Field label="Status" testId="incident-status">
          <StatusBadge status={incident.status} />
        </Field>
        <Field label="Events" testId="incident-event-count">
          {incident.event_count}
        </Field>
        <Field label="Fingerprint" testId="incident-fingerprint">
          <span className="break-all font-mono text-xs">{incident.fingerprint}</span>
        </Field>
        <Field label="First seen">{formatDate(incident.first_seen)}</Field>
        <Field label="Last seen">{formatDate(incident.last_seen)}</Field>
        <Field label="RCA">{incident.latest_rca_status ?? "Not started"}</Field>
        <Field label="Repair">{incident.latest_repair_status ?? "Not started"}</Field>
      </dl>
    </header>
  );
}

function EventsSection({ incidentId }: { incidentId: string }) {
  const { data, error, loading, reload } = useApi<IncidentEvent[]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/events?limit=${LATEST_EVENTS}`,
  );
  const events = Array.isArray(data) ? data : [];

  return (
    <section aria-labelledby="events-heading">
      <h2 id="events-heading" className="mb-3 text-lg font-semibold">
        Latest events
      </h2>
      {loading ? (
        <Loading label="Loading events…" />
      ) : error ? (
        <ErrorState error={error} what="events" onRetry={reload} />
      ) : events.length === 0 ? (
        <EmptyState message="No events recorded for this incident." />
      ) : (
        <ul className="space-y-3">
          {events.map((event) => (
            <li key={event.id} data-testid="event-item" className="rounded border border-slate-200 bg-white p-3">
              <div className="flex flex-wrap items-baseline gap-x-3 text-sm">
                <span className="font-mono font-medium">{event.exception_type}</span>
                <span className="text-slate-600">{formatDate(event.timestamp)}</span>
                <span className="rounded bg-slate-100 px-1.5 text-xs text-slate-700">{event.environment}</span>
                {event.correlation_id && (
                  <span className="font-mono text-xs text-slate-500">correlation: {event.correlation_id}</span>
                )}
              </div>
              <p className="mt-1 text-sm">{event.message}</p>
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-slate-600">Stack trace (redacted)</summary>
                <pre className="mt-2 max-h-80 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
                  {event.stack_trace}
                </pre>
              </details>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function TimelineSection({ incidentId }: { incidentId: string }) {
  const { data, error, loading, reload } = useApi<TimelineEntry[]>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/timeline`,
  );
  const entries = chronological(Array.isArray(data) ? data : []);

  return (
    <section aria-labelledby="timeline-heading">
      <h2 id="timeline-heading" className="mb-3 text-lg font-semibold">
        Timeline
      </h2>
      {loading ? (
        <Loading label="Loading timeline…" />
      ) : error ? (
        <ErrorState error={error} what="the timeline" onRetry={reload} />
      ) : entries.length === 0 ? (
        <EmptyState message="No timeline entries yet." />
      ) : (
        <ol className="relative ml-2 border-l-2 border-slate-200">
          {entries.map((entry) => {
            const summary = summarizeDetails(entry.details);
            return (
              <li key={entry.id} data-testid="timeline-entry" className="relative mb-4 pl-5 last:mb-0">
                <span
                  aria-hidden="true"
                  className={`absolute -left-[7px] top-1.5 h-3 w-3 rounded-full ring-2 ring-white ${timelineDot(entry.type)}`}
                />
                <p className="text-sm font-medium">{timelineLabel(entry.type)}</p>
                <p className="text-xs text-slate-500">
                  {formatDate(entry.timestamp)} · {entry.actor}
                </p>
                {summary && <p className="mt-0.5 break-all text-xs text-slate-600">{summary}</p>}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

export function IncidentDetailPage() {
  const { id = "" } = useParams();
  const { data, error, loading, reload } = useApi<IncidentDetail>(`/api/v1/incidents/${encodeURIComponent(id)}`);

  return (
    <div className="space-y-6">
      <Link to="/incidents" className="text-sm text-blue-700 hover:underline">
        ← Back to incidents
      </Link>
      {loading ? (
        <Loading label="Loading incident…" />
      ) : error || !data ? (
        <>
          <h1 className="text-xl font-semibold">Incident #{id}</h1>
          {error ? (
            <ErrorState error={error} what="the incident" onRetry={reload} />
          ) : (
            <EmptyState message="Incident not found." />
          )}
        </>
      ) : (
        <>
          <IncidentHeader incident={data} />
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <EventsSection incidentId={id} />
            </div>
            <TimelineSection incidentId={id} />
          </div>
        </>
      )}
    </div>
  );
}
