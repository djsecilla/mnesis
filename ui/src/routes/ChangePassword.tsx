import { useState } from "react";
import { changePassword } from "../api/client";
import Logo from "../components/Logo";

/** The mandatory first-login password change (R3). Shown when the session is
 * `must_change_password` — the server restricts such a session to this one action, so the
 * app cannot proceed until it succeeds. On success the server rotates the session cookie to
 * a full session; the caller re-checks the session and the app unlocks. */
export default function ChangePassword({
  principalId,
  onDone,
  onLogout,
}: {
  principalId: string;
  onDone: () => void | Promise<void>;
  onLogout: () => void | Promise<void>;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < 12;
  const canSubmit = !!current && !!next && next === confirm && !tooShort && !busy;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await changePassword(current, next);
      await onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change the password");
      setBusy(false);
    }
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
        <h1 className="mb-1 text-center text-lg font-medium">Set a new password</h1>
        <p className="mb-6 text-center text-xs text-muted">
          Signed in as <span className="text-fg">{principalId}</span>. You must choose a new
          password before continuing.
        </p>

        {error && (
          <div className="mb-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
            {error}
          </div>
        )}

        <label className="mb-1 block text-xs font-medium text-muted" htmlFor="current">
          Current password
        </label>
        <input
          id="current"
          type="password"
          className="mb-4 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          autoComplete="current-password"
          autoFocus
          required
        />

        <label className="mb-1 block text-xs font-medium text-muted" htmlFor="new-password">
          New password
        </label>
        <input
          id="new-password"
          type="password"
          className="mb-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          autoComplete="new-password"
          required
        />
        <p className={`mb-3 text-xs ${tooShort ? "text-red-400" : "text-muted"}`}>
          At least 12 characters.
        </p>

        <label className="mb-1 block text-xs font-medium text-muted" htmlFor="confirm-password">
          Confirm new password
        </label>
        <input
          id="confirm-password"
          type="password"
          className="mb-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="new-password"
          required
        />
        <p className={`mb-6 text-xs ${mismatch ? "text-red-400" : "text-muted"}`}>
          {mismatch ? "The passwords do not match." : " "}
        </p>

        <button
          type="submit"
          disabled={!canSubmit}
          className="w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg transition hover:opacity-90 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Set new password"}
        </button>

        <button
          type="button"
          onClick={() => void onLogout()}
          className="mt-3 w-full text-center text-xs text-muted underline-offset-2 hover:text-fg hover:underline"
        >
          Sign out
        </button>
      </form>
    </div>
  );
}
