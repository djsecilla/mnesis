import { useState } from "react";
import { changePassword } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Spinner } from "../components/IngestReview";

/** The account area (R9): a principal manages its OWN profile + password here — distinct
 * from the admin Users area (managing OTHER accounts). Changing your password never changes
 * your role; on success the server rotates the session cookie. */
export default function AccountPage() {
  const { session } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < 12;
  const canSubmit = !!current && !!next && next === confirm && !tooShort && !busy;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change the password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-md p-8">
      <header className="mb-5">
        <h1 className="text-xl font-semibold">Account</h1>
        <p className="mt-1 text-sm text-muted">Your profile and password.</p>
      </header>

      <dl className="mb-6 space-y-1 rounded-lg border border-border p-4 text-sm">
        <Row label="Username" value={session.principal_id} />
        <Row label="Role" value={session.roles.join(", ")} />
        <Row label="Tenant" value={session.tenant_id} />
      </dl>

      <form onSubmit={onSubmit} className="space-y-3 rounded-lg border border-border p-4">
        <h2 className="text-sm font-medium">Change password</h2>

        {done && (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400">
            Password changed.
          </div>
        )}
        {error && <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</div>}

        <Field id="cur-pw" label="Current password" value={current} onChange={setCurrent} autoComplete="current-password" />
        <div>
          <Field id="new-pw" label="New password" value={next} onChange={setNext} autoComplete="new-password" />
          <p className={`mt-1 text-xs ${tooShort ? "text-red-400" : "text-muted"}`}>At least 12 characters.</p>
        </div>
        <div>
          <Field id="confirm-pw" label="Confirm new password" value={confirm} onChange={setConfirm} autoComplete="new-password" />
          {mismatch && <p className="mt-1 text-xs text-red-400">The passwords do not match.</p>}
        </div>

        <button
          type="submit"
          disabled={!canSubmit}
          className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
        >
          {busy && <Spinner />}
          Update password
        </button>
      </form>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  autoComplete,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
}) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-muted" htmlFor={id}>{label}</label>
      <input
        id={id}
        type="password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
      />
    </div>
  );
}
