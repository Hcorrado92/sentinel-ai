import { useState, type FormEvent } from "react";
import { clearToken, getToken, setToken } from "../api/client";

/** Stores the admin token used for authenticated dashboard requests. */
export function TokenSettings() {
  const [hasToken, setHasToken] = useState<boolean>(() => getToken() !== null);
  const [value, setValue] = useState("");

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!value.trim()) return;
    setToken(value);
    setValue("");
    setHasToken(true);
  }

  function onClear() {
    clearToken();
    setHasToken(false);
  }

  return (
    <form onSubmit={onSubmit} className="flex items-center gap-2 text-sm" aria-label="Admin token">
      <label htmlFor="admin-token" className="sr-only">
        Admin token
      </label>
      <input
        id="admin-token"
        type="password"
        autoComplete="off"
        placeholder={hasToken ? "Token saved" : "Admin token"}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="rounded border border-slate-300 px-2 py-1"
      />
      <button
        type="submit"
        disabled={!value.trim()}
        className="rounded bg-slate-800 px-3 py-1 text-white disabled:opacity-50"
      >
        Save token
      </button>
      {hasToken && (
        <button type="button" onClick={onClear} className="rounded border border-slate-300 px-3 py-1">
          Clear
        </button>
      )}
    </form>
  );
}
