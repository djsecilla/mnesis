import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { setActiveVault } from "../api/client";
import { activateVault, listVaults, type Vault } from "../api/endpoints";
import { useAuth } from "../auth/AuthContext";

// V8 — the active vault is a client selection (V5) carried on every request as the
// `X-Mnesis-Vault` header and re-authorized server-side per request. This provider owns it:
// it exposes the principal's OWN vaults + the active one, and a `switchVault` that
// re-authorizes, CLEARS all vault-scoped caches, and re-points the header — so no data from
// the previous vault can remain. Available to EVERY authenticated principal (not admin-gated).

const DEFAULT_VAULT = "default";

interface VaultState {
  active: string;
  vaults: Vault[];
  loading: boolean;
  switching: boolean;
  error: string | null;
  switchVault: (id: string) => Promise<void>;
}

const VaultContext = createContext<VaultState | null>(null);

export function VaultProvider({ children }: { children: React.ReactNode }) {
  const { session } = useAuth();
  const qc = useQueryClient();
  const storageKey = `mnesis.activeVault.${session.tenant_id}.${session.principal_id}`;

  const [active, setActive] = useState<string>(() => {
    // Restore the last active vault and set the request header SYNCHRONOUSLY, before any
    // child screen fires a vault-scoped query.
    let stored = DEFAULT_VAULT;
    try {
      stored = localStorage.getItem(storageKey) || DEFAULT_VAULT;
    } catch {
      /* no storage — default */
    }
    setActiveVault(stored);
    return stored;
  });
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  const apply = useCallback(
    (id: string) => {
      setActiveVault(id);
      try {
        localStorage.setItem(storageKey, id);
      } catch {
        /* ignore */
      }
      setActive(id);
    },
    [storageKey],
  );

  const { data, isLoading, error } = useQuery({ queryKey: ["vaults", active], queryFn: listVaults, retry: false });
  const vaults = data?.vaults ?? [];

  // Graceful fallback: if the restored active vault is no longer accessible (deleted, or a
  // revoked grant → the middleware 403s, or simply absent from the list), drop to the
  // always-available default and clear anything fetched under the stale selection.
  useEffect(() => {
    if (active === DEFAULT_VAULT) return;
    const gone = error != null || (data != null && !vaults.some((v) => v.vault_id === active));
    if (gone) {
      apply(DEFAULT_VAULT);
      qc.clear();
    }
  }, [error, data, active, apply, qc, vaults]);

  const switchVault = useCallback(
    async (id: string) => {
      if (id === active) return;
      setSwitching(true);
      setSwitchError(null);
      try {
        await activateVault(id); // re-authorize the grant server-side (throws on 403/404)
        qc.clear(); // CLEAR every vault-scoped cache — no stale cross-vault data survives
        apply(id); // re-point the header + persist + state; screens remount via the Shell key
      } catch (e) {
        setSwitchError(e instanceof Error ? e.message : "Could not switch vault");
      } finally {
        setSwitching(false);
      }
    },
    [active, apply, qc],
  );

  // Clear the request header when the provider unmounts (logout / session end).
  useEffect(() => () => setActiveVault(null), []);

  const value: VaultState = { active, vaults, loading: isLoading, switching, error: switchError, switchVault };
  return <VaultContext.Provider value={value}>{children}</VaultContext.Provider>;
}

export function useVault(): VaultState {
  const ctx = useContext(VaultContext);
  if (!ctx) throw new Error("useVault must be used within <VaultProvider>");
  return ctx;
}
