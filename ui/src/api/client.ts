import { API_BASE } from "../config";

// IAM5 web auth: identity is a real login + an httpOnly session cookie. Every
// request sends the cookie (`credentials: "include"`); state-changing requests
// carry the double-submit CSRF token (the readable `mnesis_csrf` cookie echoed in
// the `X-CSRF-Token` header). There is no bearer token in the browser anymore.

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)mnesis_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/** Headers for a state-changing request: the CSRF token plus any extras. */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return { ...extra, "X-CSRF-Token": csrfToken() };
}

// A 401 anywhere means the session is gone/expired — the app shows the login
// screen. The AuthProvider registers this so it can react without prop-drilling.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    onUnauthorized?.();
    throw new Error("401 Unauthorized");
  }
  if (!res.ok) {
    let detail = "";
    try {
      detail = JSON.stringify(await res.json());
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ""}`);
  }
  return (await res.json()) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  return handle<T>(await fetch(`${API_BASE}${path}`, { credentials: "include" }));
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  return handle<T>(
    await fetch(`${API_BASE}${path}`, {
      method: "POST",
      credentials: "include",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }),
  );
}

// --- session / auth endpoints ----------------------------------------------

export interface SessionInfo {
  principal_id: string;
  tenant_id: string;
  roles: string[];
  scopes: string[];
  kind: string;
  permissions: string[];
  // R3: when true the principal must change its password before anything else works
  // (the server restricts the session to exactly the change-password action).
  must_change_password: boolean;
}

/** The current session, or `null` when unauthenticated (does NOT trigger the
 * global unauthorized handler — the caller decides to show the login screen). */
export async function getSession(): Promise<SessionInfo | null> {
  const res = await fetch(`${API_BASE}/auth/session`, { credentials: "include" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`session check failed: ${res.status}`);
  return (await res.json()) as SessionInfo;
}

export async function login(username: string, password: string, tenantId?: string): Promise<SessionInfo> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, ...(tenantId ? { tenant_id: tenantId } : {}) }),
  });
  if (!res.ok) {
    let msg = `login failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.error === "account_locked") msg = "Too many attempts — try again later.";
      else if (res.status === 401) msg = "Invalid username or password.";
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  // The session/CSRF cookies are set by the server; read the current principal.
  return (await getSession()) as SessionInfo;
}

/** R3 — change the current principal's own password. This is the ONE action a restricted
 * (must_change_password) session may perform; on success the server rotates the session
 * cookie to a full session and clears the restriction. Surfaces the server's policy
 * message (too-short / reused) so the user can correct it. */
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/change-password`, {
    method: "POST",
    credentials: "include",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!res.ok) {
    let msg = `change failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.error === "weak_or_reused_password") msg = body.message || "Password does not meet the policy.";
      else if (body?.error === "invalid_current_password") msg = "Current password is incorrect.";
      else if (body?.error === "account_locked") msg = "Too many attempts — try again later.";
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  // The rotated session/CSRF cookies are set by the server; the caller re-checks the session.
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, {
    method: "POST",
    credentials: "include",
    headers: authHeaders(),
  });
}

export { API_BASE };
