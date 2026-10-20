import { useCallback, useEffect, useState } from "react";
import { ApiError, apiFetch } from "../api/client";

export interface ApiState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/** Fetches `path` from the Sentinel API and tracks loading/error state. */
export function useApi<T>(path: string): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    apiFetch<T>(path)
      .then((body) => {
        if (!active) return;
        setData(body);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setData(null);
        setError(err instanceof ApiError ? err : new ApiError(0, String(err)));
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [path, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { data, error, loading, reload };
}
