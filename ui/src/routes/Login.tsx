import { useState } from "react";
import { login } from "../api/client";
import Logo from "../components/Logo";

/** The login screen (IAM5). Posts username/password to /api/auth/login, which sets
 * the httpOnly session + CSRF cookies; on success the app re-checks the session. */
export default function Login({ onSuccess }: { onSuccess: () => void | Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetSent, setResetSent] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      await onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
      setBusy(false);
    }
  }

  async function onReset() {
    setError(null);
    try {
      const { API_BASE } = await import("../config");
      await fetch(`${API_BASE}/auth/reset/request`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim() }),
      });
    } catch {
      /* the endpoint always accepts; never reveal account existence */
    }
    setResetSent(true);
  }

  return (
    <div className="flex h-screen items-center justify-center bg-bg text-fg">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-xl border border-border bg-elev p-8 shadow-lg"
      >
        <div className="mb-6 flex justify-center">
          <Logo lockup size={40} />
        </div>
        <h1 className="mb-6 text-center text-lg font-medium">Sign in</h1>

        {error && (
          <div className="mb-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
            {error}
          </div>
        )}
        {resetSent && (
          <div className="mb-4 rounded-md border border-border bg-bg px-3 py-2 text-sm text-muted">
            If that account exists, a reset has been initiated — contact your administrator to
            complete it.
          </div>
        )}

        <label className="mb-1 block text-xs font-medium text-muted" htmlFor="username">
          Username
        </label>
        <input
          id="username"
          className="mb-4 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          autoFocus
          required
        />

        <label className="mb-1 block text-xs font-medium text-muted" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          className="mb-6 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />

        <button
          type="submit"
          disabled={busy || !username || !password}
          className="w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg transition hover:opacity-90 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <button
          type="button"
          onClick={onReset}
          disabled={!username}
          className="mt-3 w-full text-center text-xs text-muted underline-offset-2 hover:text-fg hover:underline disabled:opacity-50"
        >
          Forgot password?
        </button>
      </form>
    </div>
  );
}
