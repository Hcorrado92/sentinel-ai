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

export interface TimelineEntry {
  id: number;
  type: string;
  timestamp: string;
  actor: string;
  details: Record<string, unknown>;
}
