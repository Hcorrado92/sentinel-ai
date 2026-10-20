/** Response shapes of the dashboard read API (backend/app/api/dashboard.py). */

export type IncidentStatus = "open" | "investigating" | "fix_proposed" | "resolved" | "regressed";
export type Severity = "low" | "medium" | "high" | "critical";

export interface ApplicationSummary {
  id: number;
  name: string;
  slug: string;
  created_at: string;
  budget_usd: number | null;
  monthly_spend_usd: number;
  incident_counts: Record<string, number>;
  total_incidents: number;
  /** Aggregate AI cost in USD; null when no provider reported one (mock). */
  ai_cost_usd?: number | null;
}

export interface Incident {
  id: number;
  application_id: number;
  application_name: string;
  fingerprint: string;
  title: string;
  exception_type: string;
  severity: string;
  status: string;
  event_count: number;
  first_seen: string;
  last_seen: string;
  resolved_at: string | null;
  created_at: string;
}

export interface IncidentPage {
  items: Incident[];
  total: number;
  limit: number;
  offset: number;
}

export interface IncidentDetail extends Incident {
  latest_rca_status: string | null;
  latest_repair_status: string | null;
}

export interface IncidentEvent {
  id: number;
  event_id: string | null;
  timestamp: string;
  received_at: string;
  exception_type: string;
  message: string;
  stack_trace: string;
  environment: string;
  correlation_id: string | null;
  metadata: Record<string, unknown>;
}

/** Response shapes of the job and pull request APIs (backend/app/api/jobs.py, pull_requests.py). */

export interface JobAccepted {
  job_id: number;
  status: string;
}

export interface RCAHypothesis {
  title: string;
  description: string;
  confidence: number;
  evidence: string[];
  status: "hypothesis" | "confirmed" | string;
}

export interface SuspectLocation {
  file: string;
  function: string | null;
  reason: string;
}

export interface RCAReportBody {
  summary?: string;
  hypotheses?: RCAHypothesis[];
  suspect_locations?: SuspectLocation[];
  uncertainties?: string[];
  next_steps?: string[];
}

export interface RCAReport {
  id: number;
  incident_id: number;
  job_id: number | null;
  status: string;
  summary: string;
  report: RCAReportBody;
  provider: string | null;
  model: string | null;
  is_mock: boolean;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface RepairAttempt {
  id: number;
  incident_id: number;
  job_id: number | null;
  repository_id: number | null;
  attempt_number: number;
  branch_name: string | null;
  status: string;
  explanation: string | null;
  diff: string | null;
  test_command: string[] | null;
  test_output: string | null;
  test_summary: Record<string, unknown> | null;
  returncode: number | null;
  tests_passed: boolean | null;
  regression_test_failed_before: boolean | null;
  commit_sha: string | null;
  stop_reason: string | null;
  error: string | null;
  pr_url: string | null;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface PullRequest {
  id: number;
  incident_id: number;
  repair_attempt_id: number;
  repository_id: number | null;
  status: "prepared" | "published" | "publish_failed" | string;
  branch: string;
  base: string;
  title: string;
  body: string;
  github_repo: string | null;
  url: string | null;
  error: string | null;
  draft: boolean;
  created_at: string;
  published_at: string | null;
}

export type RunType = "rca" | "repair";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Run {
  id: number;
  type: RunType | string;
  status: RunStatus | string;
  incident_id: number | null;
  incident_title: string | null;
  application_id: number | null;
  attempts: number;
  max_attempts: number;
  error: string | null;
  result_ref: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  result: string | null;
  stop_reason: string | null;
  provider: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  /** USD reported by the provider; null when not available (mock). */
  cost_usd: number | null;
}

export interface RunPage {
  items: Run[];
  total: number;
  limit: number;
  offset: number;
}

/** Response shape of GET /api/v1/costs (backend/app/api/costs.py). */
export interface ApplicationCost {
  application_id: number;
  application_name: string;
  rca_runs: number;
  repair_attempts: number;
  runs_without_cost: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
}

export interface CostSummary {
  items: ApplicationCost[];
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
}

/** Response shape of GET /api/v1/incidents/{id}/stats and /api/v1/stats/events (backend/app/api/stats.py). */
export interface BucketCount {
  start: string;
  count: number;
}

export interface EventStats {
  bucket: "hour" | "day" | string;
  incident_id: number | null;
  application_id: number | null;
  total: number;
  buckets: BucketCount[];
}

export interface TimelineEntry {
  id: number;
  type: string;
  timestamp: string;
  actor: string;
  details: Record<string, unknown>;
}
