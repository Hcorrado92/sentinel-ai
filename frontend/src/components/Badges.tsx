const SEVERITY_STYLES: Record<string, string> = {
  critical: "bg-red-600 text-white",
  high: "bg-orange-100 text-orange-800",
  medium: "bg-yellow-100 text-yellow-800",
  low: "bg-slate-200 text-slate-700",
};

const STATUS_STYLES: Record<string, string> = {
  open: "bg-red-50 text-red-700",
  investigating: "bg-blue-50 text-blue-700",
  fix_proposed: "bg-violet-50 text-violet-700",
  resolved: "bg-emerald-50 text-emerald-700",
  regressed: "bg-rose-100 text-rose-800",
};

const base = "inline-block rounded-full px-2 py-0.5 text-xs font-medium";

export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span data-testid="severity-badge" className={`${base} ${SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.low}`}>
      {severity}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span data-testid="status-badge" className={`${base} ${STATUS_STYLES[status] ?? "bg-slate-100 text-slate-700"}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
