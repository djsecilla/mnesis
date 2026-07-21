import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  createAdminUser,
  deleteAdminUser,
  listAdminAudit,
  listAdminUsers,
  patchAdminUser,
  resetAdminUserPassword,
  revokeAdminUserCredentials,
  type AdminUser,
  type AuditEvent,
  type OneTimeCredential,
  type UserRole,
} from "../api/endpoints";
import { useAuth } from "../auth/AuthContext";
import { Spinner } from "../components/IngestReview";

// The admin-only Users (Administration) area. Every action calls an R7 endpoint;
// the admin gate, safety rules (last-admin, self-role-change), and audit are the
// SERVER's job — this screen only renders state and surfaces service messages.

export default function AdminUsersPage() {
  const { session } = useAuth();
  const { data, isLoading, error } = useQuery({ queryKey: ["admin-users"], queryFn: listAdminUsers });
  const users = data?.users ?? [];
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminUser | null>(null);

  return (
    <div className="mx-auto max-w-4xl p-8">
      <header className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Users</h1>
          <p className="mt-1 text-sm text-muted">
            Manage accounts in your administration scope. Each user owns its own tenant and
            data — managing an account never grants access to that user’s knowledge.
          </p>
        </div>
        <button
          onClick={() => setCreating(true)}
          className="shrink-0 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-fg transition hover:opacity-90"
        >
          Create user
        </button>
      </header>

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-sm text-red-400">{(error as Error).message}</p>}
      {data && users.length === 0 && (
        <div className="rounded-lg border border-dashed border-border px-4 py-12 text-center text-muted">
          No users yet.
        </div>
      )}

      {users.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-elev text-left text-xs uppercase text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Username</th>
                <th className="px-3 py-2 font-medium">Role</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.username} className="border-t border-border">
                  <td className="px-3 py-2">
                    <span className="font-medium">{u.username}</span>
                    {u.username === session.principal_id && <span className="ml-2 text-xs text-muted">(you)</span>}
                    {u.must_change_password && (
                      <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400">
                        must change pw
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2"><RoleBadge role={u.role} /></td>
                  <td className="px-3 py-2"><StatusBadge active={u.active} /></td>
                  <td className="px-3 py-2 text-xs text-muted">{formatDate(u.created)}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => setEditing(u)}
                      className="rounded-md border border-border px-2.5 py-1 text-xs hover:border-accent"
                    >
                      Manage
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <RecentActivity />

      {creating && <CreateUserModal onClose={() => setCreating(false)} />}
      {editing && <UserDetailModal user={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

// --- read-only recent activity (audit) --------------------------------------

function RecentActivity() {
  const { data } = useQuery({ queryKey: ["admin-audit"], queryFn: () => listAdminAudit(20) });
  const events = data?.events ?? [];
  if (events.length === 0) return null;
  return (
    <section className="mt-8">
      <h2 className="mb-2 text-sm font-medium text-muted">Recent activity</h2>
      <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border text-sm">
        {events.map((e, i) => (
          <li key={i} className="flex items-center justify-between gap-3 px-3 py-2">
            <span>
              <span className="font-medium">{auditLabel(e)}</span>
              {e.principal_id && <span className="text-muted"> — {e.principal_id}</span>}
              {e.role && <span className="text-muted"> ({e.role})</span>}
            </span>
            <span className="shrink-0 text-xs text-muted">{formatDateTime(e.ts)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

const AUDIT_LABELS: Record<string, string> = {
  user_created: "Created user",
  user_role_assigned: "Changed role",
  user_deactivated: "Deactivated",
  user_reactivated: "Reactivated",
  user_password_reset: "Reset password",
  user_credentials_revoked: "Revoked credentials",
  user_deleted: "Deleted user",
};

function auditLabel(e: AuditEvent): string {
  return AUDIT_LABELS[e.event] ?? e.event;
}

function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleString();
}

// --- shared UI bits ---------------------------------------------------------

function RoleBadge({ role }: { role: string }) {
  const admin = role === "admin";
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${admin ? "bg-accent/15 text-accent" : "bg-border/60 text-muted"}`}>
      {role}
    </span>
  );
}

function StatusBadge({ active }: { active: boolean }) {
  return (
    <span className={`text-xs ${active ? "text-emerald-400" : "text-muted"}`}>
      {active ? "active" : "inactive"}
    </span>
  );
}

function formatDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-xl border border-border bg-elev p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-fg">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** The one-time credential, shown ONCE. Copy affordance + a clear "won’t see again". */
function OneTimeCredentialNotice({ cred, onDone }: { cred: OneTimeCredential; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-3">
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
        One-time password for <span className="font-medium">{cred.username}</span>. Copy it now —{" "}
        <span className="font-semibold">you won’t see it again.</span> The user must change it on first login.
      </div>
      <div className="flex items-center gap-2">
        <code className="flex-1 select-all truncate rounded-md border border-border bg-bg px-3 py-2 text-sm">
          {cred.initial_password}
        </code>
        <button
          onClick={() => {
            void navigator.clipboard?.writeText(cred.initial_password);
            setCopied(true);
          }}
          className="shrink-0 rounded-md border border-border px-3 py-2 text-xs hover:border-accent"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <button
        onClick={onDone}
        className="w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg hover:opacity-90"
      >
        Done
      </button>
    </div>
  );
}

// --- create ----------------------------------------------------------------

function CreateUserModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<UserRole>("user");
  const [password, setPassword] = useState("");

  const create = useMutation({
    mutationFn: () => createAdminUser({ username: username.trim(), role, password: password.trim() || undefined }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-users"] });
      qc.invalidateQueries({ queryKey: ["admin-audit"] });
    },
  });

  if (create.data) {
    return (
      <Modal title="User created" onClose={onClose}>
        <OneTimeCredentialNotice cred={create.data} onDone={onClose} />
      </Modal>
    );
  }

  return (
    <Modal title="Create user" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
        className="space-y-3"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="new-username">Username</label>
          <input
            id="new-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="new-role">Role</label>
          <select
            id="new-role"
            value={role}
            onChange={(e) => setRole(e.target.value as UserRole)}
            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="new-password">
            Initial password <span className="text-muted">(optional — blank generates a strong one)</span>
          </label>
          <input
            id="new-password"
            type="text"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="leave blank to generate"
            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>
        {create.isError && <p className="text-sm text-red-400">{(create.error as Error).message}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-2 text-sm hover:border-accent">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!username.trim() || create.isPending}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
          >
            {create.isPending && <Spinner />}
            Create user
          </button>
        </div>
      </form>
    </Modal>
  );
}

// --- detail / edit / delete -------------------------------------------------

function UserDetailModal({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const qc = useQueryClient();
  const { session } = useAuth();
  const isSelf = user.username === session.principal_id;
  const [role, setRole] = useState<UserRole>(user.role === "admin" ? "admin" : "user");
  const [confirmName, setConfirmName] = useState("");
  const [cred, setCred] = useState<OneTimeCredential | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["admin-users"] });
    qc.invalidateQueries({ queryKey: ["admin-audit"] });
  };
  const done = () => {
    invalidate();
    onClose();
  };

  const saveRole = useMutation({ mutationFn: () => patchAdminUser(user.username, { role }), onSuccess: done });
  const setStatus = useMutation({
    mutationFn: (status: "active" | "inactive") => patchAdminUser(user.username, { status }),
    onSuccess: done,
  });
  const reset = useMutation({
    mutationFn: () => resetAdminUserPassword(user.username),
    onSuccess: (c) => {
      invalidate();
      setCred(c as OneTimeCredential);
    },
  });
  const revoke = useMutation({ mutationFn: () => revokeAdminUserCredentials(user.username), onSuccess: done });
  const remove = useMutation({ mutationFn: () => deleteAdminUser(user.username, confirmName), onSuccess: done });

  const anyError =
    (saveRole.error || setStatus.error || reset.error || revoke.error || remove.error) as Error | undefined;

  if (cred) {
    return (
      <Modal title="Password reset" onClose={done}>
        <OneTimeCredentialNotice cred={cred} onDone={done} />
      </Modal>
    );
  }

  return (
    <Modal title={`Manage ${user.username}`} onClose={onClose}>
      <div className="space-y-5">
        {/* Role */}
        <section className="space-y-2">
          <div className="text-xs font-medium uppercase text-muted">Role</div>
          <div className="flex items-center gap-2">
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
              disabled={isSelf}
              className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-50"
            >
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
            <button
              onClick={() => saveRole.mutate()}
              disabled={isSelf || role === user.role || saveRole.isPending}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
            >
              Save
            </button>
          </div>
          {isSelf && (
            <p className="text-xs text-muted">
              You cannot change your own role — manage your own password in{" "}
              <span className="font-medium">Account</span>.
            </p>
          )}
        </section>

        {/* Status + credentials */}
        <section className="space-y-2">
          <div className="text-xs font-medium uppercase text-muted">Access</div>
          <div className="flex flex-wrap gap-2">
            {user.active ? (
              <button
                onClick={() => setStatus.mutate("inactive")}
                disabled={setStatus.isPending}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:border-accent disabled:opacity-50"
              >
                Deactivate
              </button>
            ) : (
              <button
                onClick={() => setStatus.mutate("active")}
                disabled={setStatus.isPending}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:border-accent disabled:opacity-50"
              >
                Reactivate
              </button>
            )}
            <button
              onClick={() => reset.mutate()}
              disabled={reset.isPending}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:border-accent disabled:opacity-50"
            >
              Reset password
            </button>
            <button
              onClick={() => revoke.mutate()}
              disabled={revoke.isPending}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:border-accent disabled:opacity-50"
            >
              Revoke credentials
            </button>
          </div>
          <p className="text-xs text-muted">
            Deactivate/revoke sign the user out immediately (data retained). Reset issues a new
            one-time password.
          </p>
        </section>

        {/* Delete — typed confirmation */}
        <section className="space-y-2 rounded-lg border border-red-500/30 bg-red-500/5 p-3">
          <div className="text-xs font-medium uppercase text-red-400">Danger zone — delete</div>
          <p className="text-xs text-muted">
            Permanently removes this user and <span className="font-medium">all its data</span> (tenant,
            vaults, credentials). This cannot be undone. Type <span className="font-mono">{user.username}</span> to confirm.
          </p>
          <div className="flex items-center gap-2">
            <input
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              placeholder={user.username}
              className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-red-500"
            />
            <button
              onClick={() => remove.mutate()}
              disabled={confirmName !== user.username || remove.isPending}
              className="inline-flex items-center gap-2 rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
            >
              {remove.isPending && <Spinner />}
              Delete
            </button>
          </div>
        </section>

        {anyError && <p className="text-sm text-red-400">{anyError.message}</p>}
      </div>
    </Modal>
  );
}
