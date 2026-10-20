import { useEffect, useState } from "react";
import { getHealth, type Health } from "../api/client";

type State =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ready"; health: Health };

export function ProviderBadge() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    getHealth()
      .then((health) => active && setState({ kind: "ready", health }))
      .catch(() => active && setState({ kind: "error" }));
    return () => {
      active = false;
    };
  }, []);

  const base = "rounded-full px-3 py-1 text-xs font-medium";
  if (state.kind === "loading") {
    return <span className={`${base} bg-slate-200 text-slate-600`}>AI provider: checking…</span>;
  }
  if (state.kind === "error") {
    return (
      <span className={`${base} bg-red-100 text-red-700`} data-testid="provider-badge">
        AI provider: API unavailable
      </span>
    );
  }
  const { health } = state;
  return health.ai_provider_mock ? (
    <span className={`${base} bg-amber-100 text-amber-800`} data-testid="provider-badge">
      AI provider: mock
    </span>
  ) : (
    <span className={`${base} bg-emerald-100 text-emerald-800`} data-testid="provider-badge">
      AI provider: {health.ai_provider}
    </span>
  );
}
