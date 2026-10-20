import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, apiFetch } from "../api/client";
import type {
  IncidentDetail,
  IncidentEvent,
  JobAccepted,
  PullRequest,
  RCAHypothesis,
  RCAReport,
  RepairAttempt,
  TimelineEntry,
} from "../api/types";
import { SeverityBadge, StatusBadge, formatDate } from "../components/Badges";
import { DiffViewer } from "../components/DiffViewer";
import { EmptyState, ErrorState, Loading } from "../components/StatusViews";
import { useApi, type ApiState } from "../hooks/useApi";

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

const OUTCOME_STYLES: Record<string, string> = {
  completed: "bg-emerald-50 text-emerald-700",
  succeeded: "bg-emerald-50 text-emerald-700",
  published: "bg-emerald-50 text-emerald-700",
  prepared: "bg-blue-50 text-blue-700",
  running: "bg-blue-50 text-blue-700",
  queued: "bg-slate-100 text-slate-700",
  failed: "bg-red-50 text-red-700",
  publish_failed: "bg-red-50 text-red-700",
  stopped: "bg-amber-50 text-amber-800",
};

function OutcomeBadge({ status, testId }: { status: string; testId?: string }) {
  return (
    <span
      data-testid={testId}
      className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${OUTCOME_STYLES[status] ?? "bg-slate-100 text-slate-700"}`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function SectionHeading({ id, children }: { id: string; children: ReactNode }) {
  return (
    <h2 id={id} className="mb-3 text-lg font-semibold">
      {children}
    </h2>
  );
}

/** A 404 from a "latest X" endpoint means there is none yet, not a failure. */
function isMissing(state: ApiState<unknown>): boolean {
  return state.error?.status === 404;
}

function actionErrorMessage(label: string, error: unknown): string {
  if (!(error instanceof ApiError)) return `${label} failed: ${String(error)}`;
  if (error.status === 401) return `${label} failed: authentication required, enter a valid admin token.`;
  if (error.status === 0) return `${label} failed: could not reach the Sentinel API.`;
  return `${label} failed (HTTP ${error.status}): ${error.message}`;
}

type ActionState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; message: string }
  | { kind: "error"; message: string };

function ActionControl({
  id,
  label,
  runningLabel,
  disabledReason,
  run,
}: {
  id: string;
  label: string;
  runningLabel: string;
  disabledReason: string | null;
  run: () => Promise<string>;
}) {
  const [state, setState] = useState<ActionState>({ kind: "idle" });
  const running = state.kind === "running";
  const hintId = `${id}-hint`;

  async function onClick() {
    setState({ kind: "running" });
    try {
      setState({ kind: "done", message: await run() });
    } catch (err) {
      setState({ kind: "error", message: actionErrorMessage(label, err) });
    }
  }

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={onClick}
        disabled={running || disabledReason !== null}
        title={disabledReason ?? undefined}
        aria-describedby={disabledReason ? hintId : undefined}
        className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:text-slate-600"
      >
        {running ? runningLabel : label}
      </button>
      {disabledReason && (
        <p id={hintId} className="text-xs text-slate-500">
          {disabledReason}
        </p>
      )}
      {state.kind === "done" && (
        <p role="status" data-testid={`${id}-result`} className="text-xs text-emerald-700">
          {state.message}
        </p>
      )}
      {state.kind === "error" && (
        <p role="alert" data-testid={`${id}-error`} className="text-xs text-red-700">
          {state.message}
        </p>
      )}
    </div>
  );
}

function jobMessage(kind: string, job: JobAccepted): string {
  return job.status === "queued" ? `${kind} job #${job.job_id} queued.` : `${kind} job #${job.job_id}: ${job.status}.`;
}

function ActionsPanel({
  incidentId,
  rca,
  repairs,
  pr,
}: {
  incidentId: string;
  rca: ApiState<RCAReport>;
  repairs: ApiState<RepairAttempt[]>;
  pr: ApiState<PullRequest>;
}) {
  const base = `/api/v1/incidents/${encodeURIComponent(incidentId)}`;
  const attempts = Array.isArray(repairs.data) ? repairs.data : [];

  const rcaReason = rca.loading
    ? "Checking the RCA status…"
    : rca.data?.status === "running"
      ? "An RCA is already running for this incident."
      : null;
  const repairReason = rca.loading
    ? "Checking the RCA status…"
    : rca.data?.status !== "completed"
      ? "Not available: a completed RCA is required before requesting a repair."
      : null;
  const prReason = repairs.loading
    ? "Checking the repair attempts…"
    : !attempts.some((attempt) => attempt.status === "succeeded")
      ? "Not available: a successful repair verified by the repository tests is required."
      : null;

  return (
    <section aria-labelledby="actions-heading" className="rounded border border-slate-200 bg-white p-4">
      <SectionHeading id="actions-heading">Actions</SectionHeading>
      <div className="grid gap-4 sm:grid-cols-3">
        <ActionControl
          id="action-rca"
          label="Run RCA"
          runningLabel="Requesting RCA…"
          disabledReason={rcaReason}
          run={async () => {
            const job = await apiFetch<JobAccepted>(`${base}/rca`, { method: "POST" });
            rca.reload();
            return jobMessage("RCA", job);
          }}
        />
        <ActionControl
          id="action-repair"
          label="Run repair"
          runningLabel="Requesting repair…"
          disabledReason={repairReason}
          run={async () => {
            const job = await apiFetch<JobAccepted>(`${base}/repair`, { method: "POST" });
            repairs.reload();
            rca.reload();
            return jobMessage("Repair", job);
          }}
        />
        <ActionControl
          id="action-pr"
          label="Prepare draft PR"
          runningLabel="Preparing draft PR…"
          disabledReason={prReason}
          run={async () => {
            const created = await apiFetch<PullRequest>(`${base}/pull-request`, { method: "POST" });
            pr.reload();
            repairs.reload();
            return `Draft pull request ${created.status.replace(/_/g, " ")}.`;
          }}
        />
      </div>
    </section>
  );
}

function HypothesisCard({ hypothesis }: { hypothesis: RCAHypothesis }) {
  const percent = Math.round(Math.min(Math.max(Number(hypothesis.confidence) || 0, 0), 1) * 100);
  const confirmed = hypothesis.status === "confirmed";
  return (
    <li data-testid="hypothesis" className="rounded border border-slate-200 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{hypothesis.title}</span>
        <span
          data-testid="hypothesis-badge"
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${confirmed ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-800"}`}
        >
          {confirmed ? "Confirmed" : "Hypothesis"}
        </span>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <div
          role="progressbar"
          aria-label={`Confidence ${percent}%`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          className="h-2 w-40 overflow-hidden rounded bg-slate-200"
        >
          <div className="h-full bg-blue-500" style={{ width: `${percent}%` }} />
        </div>
        <span data-testid="hypothesis-confidence" className="text-xs text-slate-600">
          {percent}% confidence
        </span>
      </div>
      {hypothesis.description && <p className="mt-2 text-sm">{hypothesis.description}</p>}
      {Array.isArray(hypothesis.evidence) && hypothesis.evidence.length > 0 && (
        <>
          <p className="mt-2 text-xs uppercase text-slate-500">Evidence</p>
          <ul className="ml-5 list-disc text-sm">
            {hypothesis.evidence.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </>
      )}
    </li>
  );
}

function TextList({ title, items }: { title: string; items: string[] | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <h3 className="text-sm font-semibold">{title}</h3>
      <ul className="ml-5 mt-1 list-disc text-sm">
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function RCASection({ rca }: { rca: ApiState<RCAReport> }) {
  const report = rca.data;
  const body = report?.report ?? {};
  const hypotheses = Array.isArray(body.hypotheses) ? body.hypotheses : [];
  const suspects = Array.isArray(body.suspect_locations) ? body.suspect_locations : [];

  return (
    <section aria-labelledby="rca-heading">
      <SectionHeading id="rca-heading">Root cause analysis</SectionHeading>
      {rca.loading ? (
        <Loading label="Loading RCA…" />
      ) : isMissing(rca) ? (
        <EmptyState message="No RCA has been run for this incident yet." />
      ) : rca.error || !report ? (
        <ErrorState error={rca.error ?? new ApiError(0, "No data")} what="the RCA report" onRetry={rca.reload} />
      ) : (
        <div data-testid="rca-report" className="space-y-4 rounded border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <OutcomeBadge status={report.status} testId="rca-status" />
            {report.is_mock ? (
              <span data-testid="rca-provider" className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-800">
                Mock provider
              </span>
            ) : (
              <span data-testid="rca-provider" className="rounded-full bg-slate-100 px-2 py-0.5 font-medium text-slate-700">
                Provider: {report.provider ?? "unknown"}
                {report.model ? ` (${report.model})` : ""}
              </span>
            )}
            <span className="text-slate-500">
              {formatDate(report.finished_at ?? report.created_at)} · cost:{" "}
              {report.cost_usd == null ? "not available" : `$${report.cost_usd.toFixed(4)}`}
            </span>
          </div>
          {report.error && (
            <p className="rounded bg-red-50 p-2 text-sm text-red-800">RCA failed: {report.error}</p>
          )}
          <p className="text-sm">{report.summary || body.summary}</p>
          {hypotheses.length > 0 ? (
            <div>
              <h3 className="mb-2 text-sm font-semibold">Hypotheses</h3>
              <ul className="space-y-3">
                {hypotheses.map((hypothesis, index) => (
                  <HypothesisCard key={index} hypothesis={hypothesis} />
                ))}
              </ul>
            </div>
          ) : (
            report.status === "completed" && <p className="text-sm text-slate-600">No hypotheses were proposed.</p>
          )}
          {suspects.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold">Suspect files and functions</h3>
              <ul className="mt-1 space-y-1 text-sm">
                {suspects.map((suspect, index) => (
                  <li key={index} data-testid="suspect-location">
                    <span className="font-mono">
                      {suspect.file}
                      {suspect.function ? `:${suspect.function}` : ""}
                    </span>
                    {suspect.reason && <span className="text-slate-600"> — {suspect.reason}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <TextList title="Uncertainties" items={body.uncertainties} />
          <TextList title="Next steps" items={body.next_steps} />
        </div>
      )}
    </section>
  );
}

function RepairAttemptCard({ attempt }: { attempt: RepairAttempt }) {
  return (
    <li data-testid="repair-attempt" className="space-y-2 rounded border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="font-medium">Attempt {attempt.attempt_number}</span>
        <OutcomeBadge status={attempt.status} testId="repair-status" />
        {attempt.branch_name && <span className="font-mono text-xs text-slate-600">{attempt.branch_name}</span>}
        {attempt.tests_passed != null && (
          <span className={`text-xs ${attempt.tests_passed ? "text-emerald-700" : "text-red-700"}`}>
            tests {attempt.tests_passed ? "passed" : "failed"}
          </span>
        )}
      </div>
      {attempt.stop_reason && (
        <p data-testid="repair-stop-reason" className="text-sm text-amber-800">
          Stop reason: {attempt.stop_reason}
        </p>
      )}
      {attempt.error && <p className="text-sm text-red-700">Error: {attempt.error}</p>}
      {attempt.explanation && <p className="text-sm">{attempt.explanation}</p>}
      {attempt.test_command && attempt.test_command.length > 0 && (
        <p className="text-xs text-slate-600">
          Test command: <code className="font-mono">{attempt.test_command.join(" ")}</code>
          {attempt.returncode != null &&` (exit code ${attempt.returncode})`}
        </p>
      )}
      {attempt.diff && (
        <div>
          <p className="mb-1 text-xs uppercase text-slate-500">Proposed diff</p>
          <DiffViewer diff={attempt.diff} />
        </div>
      )}
      {attempt.test_output && (
        <details>
          <summary className="cursor-pointer text-xs text-slate-600">Test output (redacted)</summary>
          <pre className="mt-2 max-h-80 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
            {attempt.test_output}
          </pre>
        </details>
      )}
    </li>
  );
}

function RepairsSection({ repairs }: { repairs: ApiState<RepairAttempt[]> }) {
  const attempts = Array.isArray(repairs.data) ? repairs.data : [];
  return (
    <section aria-labelledby="repairs-heading">
      <SectionHeading id="repairs-heading">Repair attempts</SectionHeading>
      {repairs.loading ? (
        <Loading label="Loading repair attempts…" />
      ) : repairs.error ? (
        <ErrorState error={repairs.error} what="the repair attempts" onRetry={repairs.reload} />
      ) : attempts.length === 0 ? (
        <EmptyState message="No repair attempts yet." />
      ) : (
        <ul className="space-y-3">
          {attempts.map((attempt) => (
            <RepairAttemptCard key={attempt.id} attempt={attempt} />
          ))}
        </ul>
      )}
    </section>
  );
}

function PullRequestSection({ pr, repairs }: { pr: ApiState<PullRequest>; repairs: ApiState<RepairAttempt[]> }) {
  const attempts = Array.isArray(repairs.data) ? repairs.data : [];
  const hasSuccess = attempts.some((attempt) => attempt.status === "succeeded");
  const data = pr.data;

  return (
    <section aria-labelledby="pr-heading">
      <SectionHeading id="pr-heading">Draft pull request</SectionHeading>
      {pr.loading ? (
        <Loading label="Loading pull request…" />
      ) : isMissing(pr) ? (
        <p data-testid="pr-status" className="rounded border border-dashed border-slate-300 bg-white p-4 text-sm text-slate-600">
          {hasSuccess
            ? "No pull request prepared yet."
            : "No pull request: a draft PR is only prepared after a repair passes the repository tests."}
        </p>
      ) : pr.error || !data ? (
        <ErrorState error={pr.error ?? new ApiError(0, "No data")} what="the pull request" onRetry={pr.reload} />
      ) : (
        <div data-testid="pr-status" className="space-y-2 rounded border border-slate-200 bg-white p-4 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <OutcomeBadge status={data.status} />
            <span className="font-medium">{data.title}</span>
          </div>
          <p className="font-mono text-xs text-slate-600">
            {data.branch} → {data.base}
            {data.github_repo ? ` · ${data.github_repo}` : ""}
          </p>
          {data.url ? (
            <a href={data.url} target="_blank" rel="noreferrer" className="text-blue-700 hover:underline">
              Open draft pull request
            </a>
          ) : data.status === "prepared" ? (
            <p className="text-slate-600">Prepared locally as a draft; publication to GitHub is disabled.</p>
          ) : null}
          {data.error && <p className="text-red-700">Publication error: {data.error}</p>}
          <details>
            <summary className="cursor-pointer text-xs text-slate-600">Pull request description</summary>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-3 text-xs">{data.body}</pre>
          </details>
        </div>
      )}
    </section>
  );
}

function IncidentWorkspace({ incidentId }: { incidentId: string }) {
  const base = `/api/v1/incidents/${encodeURIComponent(incidentId)}`;
  const rca = useApi<RCAReport>(`${base}/rca`);
  const repairs = useApi<RepairAttempt[]>(`${base}/repairs`);
  const pr = useApi<PullRequest>(`${base}/pull-request`);

  return (
    <>
      <ActionsPanel incidentId={incidentId} rca={rca} repairs={repairs} pr={pr} />
      <RCASection rca={rca} />
      <RepairsSection repairs={repairs} />
      <PullRequestSection pr={pr} repairs={repairs} />
    </>
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
          <IncidentWorkspace incidentId={id} />
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
