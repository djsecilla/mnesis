import { useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";
import { useVault } from "../vault/VaultContext";
import { VaultIcon } from "./Icon";
import { Spinner } from "./IngestReview";

/** The persistent vault switcher in the app chrome (V8). Always shows the ACTIVE vault name
 * so the user knows which knowledge base they're viewing; the menu lists the principal's own
 * vaults (active marked) + a "Manage vaults" link. Selecting a vault re-authorizes and
 * switches (the VaultProvider clears all vault-scoped caches on switch). Available to every
 * authenticated principal. */
export default function VaultSwitcher() {
  const { active, vaults, loading, switching, error, switchVault } = useVault();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const activeName = vaults.find((v) => v.vault_id === active)?.name || active;

  async function pick(id: string) {
    setOpen(false);
    await switchVault(id);
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        title={`Active vault: ${activeName}`}
        aria-label={`Active vault: ${activeName}`}
        className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted transition hover:border-accent hover:text-fg"
      >
        {switching ? <Spinner /> : <VaultIcon width={14} height={14} />}
        <span className="max-w-[10rem] truncate text-fg">{switching ? "Switching…" : activeName}</span>
        <span aria-hidden className="text-muted">▾</span>
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-1 w-56 rounded-lg border border-border bg-elev py-1 shadow-lg">
          <div className="px-3 py-1 text-[10px] font-medium uppercase text-muted">Vaults</div>
          {loading && <div className="px-3 py-1.5 text-xs text-muted">Loading…</div>}
          {!loading && vaults.length === 0 && (
            <div className="px-3 py-1.5 text-xs text-muted">No vaults.</div>
          )}
          {vaults.map((v) => (
            <button
              key={v.vault_id}
              onClick={() => void pick(v.vault_id)}
              className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm hover:bg-bg"
            >
              <span className="truncate">{v.name}</span>
              {v.vault_id === active && <span className="text-accent">●</span>}
            </button>
          ))}
          <div className="my-1 h-px bg-border" />
          <NavLink
            to="/vaults"
            onClick={() => setOpen(false)}
            className="block px-3 py-1.5 text-sm text-muted hover:bg-bg hover:text-fg"
          >
            Manage vaults…
          </NavLink>
        </div>
      )}

      {error && (
        <div className="absolute right-0 z-50 mt-1 w-56 rounded-md border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-xs text-red-400">
          {error}
        </div>
      )}
    </div>
  );
}
