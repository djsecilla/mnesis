import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { getSession, logout as apiLogout, setUnauthorizedHandler, type SessionInfo } from "../api/client";
import { BrandSplash } from "../components/Logo";
import Login from "../routes/Login";
import ChangePassword from "../routes/ChangePassword";

interface AuthState {
  session: SessionInfo;
  logout: () => Promise<void>;
  can: (permission: string) => boolean;
  /** Server-resolved admin role (from the session, never a client guess) — gates the
   * admin nav entry + routes for UX; the server (R7) is the real control. */
  isAdmin: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

/** Gate the whole app behind a session (IAM5). On mount it checks the server-side
 * session (cookie); unauthenticated → the login screen; a 401 anywhere later drops
 * back to login. Identity is never read from the client — only from the server. */
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setSession(await getSession());
    setLoading(false);
  }, []);

  useEffect(() => {
    // A 401 from any request means the session ended — show login immediately.
    setUnauthorizedHandler(() => setSession(null));
    void refresh();
    return () => setUnauthorizedHandler(null);
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      setSession(null);
    }
  }, []);

  if (loading) return <BrandSplash animate tagline="Loading…" />;
  if (!session) return <Login onSuccess={refresh} />;
  // R3: a first-login / reset principal is restricted to changing its own password. The
  // server denies every other call, so gate the whole app until the change succeeds.
  if (session.must_change_password)
    return <ChangePassword principalId={session.principal_id} onDone={refresh} onLogout={logout} />;

  const can = (permission: string) => session.permissions.includes(permission);
  const isAdmin = session.roles.includes("admin");
  return <AuthContext.Provider value={{ session, logout, can, isAdmin }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
