import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { createVault, deleteVault, listVaults, renameVault, type Vault } from "../api/endpoints";
import { Spinner } from "../components/IngestReview";
import { useVault } from "../vault/VaultContext";

// V8 — the vault management screen. Available to EVERY authenticated principal for its OWN
// vaults (not admin-gated, no cross-principal visibility). All actions call the V7 endpoints;
// safety rules (quota, last-vault, name validation) live on the server and are shown verbatim.

const DEFAULT_VAULT = "default";

export default function VaultsPage() {
  const { active, switchVault } = useVault();
  const { data, isLoading, error } = useQuery({ queryKey: ["vaults", active], queryFn: listVaults });
  const vaults = data?.vaults ?? [];
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Vault | null>(null);
  const [deleting, setDeleting] = useState<Vault | null>(null);

  return (
    <div className="mx-auto max-w-3xl p-8">
      <header className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Vaults</h1>
          <p className="mt-1 text-sm text-muted">
            Each vault is a separate knowledge base with its own pages, graph, and schema.
            You are currently viewing <span className="font-medium text-fg">{activeName(vaults, active)}</span>.
          </p>
        </div>
        <button
          onClick={() => setCreating(true)}
          className="shrink-0 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-fg transition hover:opacity-90"
        >
          Create vault
        </button>
      </header>

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-sm text-red-400">{(error as Error).message}</p>}
      {data && vaults.length === 0 && (
        <div className="rounded-lg border border-dashed border-border px-4 py-12 text-center text-muted">
          You have no vaults. Create one to get started.
        </div>
      )}

      {vaults.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-elev text-left text-xs uppercase text-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {vaults.map((v) => (
                <tr key={v.vault_id} className="border-t border-border">
                  <td className="px-3 py-2">
                    <span className="font-medium">{v.name}</span>
                    {v.vault_id === active && (
                      <span className="ml-2 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent">active</span>
                    )}
                    <span className="ml-2 text-xs text-muted">{v.page_count} pages</span>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">{formatDate(v.created)}</td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1.5">
                      {v.vault_id !== active && (
                        <button
                          onClick={() => void switchVault(v.vault_id)}
                          className="rounded-md border border-border px-2.5 py-1 text-xs hover:border-accent"
                        >
                          Switch
                        </button>
                      )}
                      <button
                        onClick={() => setRenaming(v)}
                        className="rounded-md border border-border px-2.5 py-1 text-xs hover:border-accent"
                      >
                        Rename
                      </button>
                      <DeleteButton vault={v} count={vaults.length} onClick={() => setDeleting(v)} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating && <CreateVaultModal onClose={() => setCreating(false)} />}
      {renaming && <RenameVaultModal vault={renaming} onClose={() => setRenaming(null)} />}
      {deleting && <DeleteVaultModal vault={deleting} onClose={() => setDeleting(null)} />}
    </div>
  );
}

function activeName(vaults: Vault[], active: string): string {
  return vaults.find((v) => v.vault_id === active)?.name || active;
}

function formatDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

/** Delete is disabled (UX) for the default vault and when it is the only vault — the server
 * enforces both (protected / last_vault); this just explains it up front. */
function DeleteButton({ vault, count, onClick }: { vault: Vault; count: number; onClick: () => void }) {
  const isLast = count <= 1;
  const isDefault = vault.vault_id === DEFAULT_VAULT;
  const disabled = isLast || isDefault;
  const why = isDefault ? "The default vault cannot be deleted." : isLast ? "Your last remaining vault cannot be deleted." : "";
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={why}
      className="rounded-md border border-red-500/40 px-2.5 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-40"
    >
      Delete
    </button>
  );
}

// --- modals -----------------------------------------------------------------

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-border bg-elev p-6 shadow-lg" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted hover:text-fg">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function CreateVaultModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const { switchVault } = useVault();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => createVault(name.trim()),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["vaults"] }),
  });

  if (create.data) {
    const v = create.data;
    return (
      <Modal title="Vault created" onClose={onClose}>
        <p className="mb-4 text-sm">
          Created <span className="font-medium">{v.name}</span>. Switch to it now?
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border border-border px-3 py-2 text-sm hover:border-accent">
            Stay here
          </button>
          <button
            onClick={async () => {
              await switchVault(v.vault_id);
              onClose();
            }}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg hover:opacity-90"
          >
            Switch to it
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Create vault" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
        className="space-y-3"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="vault-name">Name</label>
          <input
            id="vault-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            placeholder="e.g. Research"
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
            disabled={!name.trim() || create.isPending}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
          >
            {create.isPending && <Spinner />}
            Create
          </button>
        </div>
      </form>
    </Modal>
  );
}

function RenameVaultModal({ vault, onClose }: { vault: Vault; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(vault.name);
  const rename = useMutation({
    mutationFn: () => renameVault(vault.vault_id, name.trim()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vaults"] });
      onClose();
    },
  });

  return (
    <Modal title={`Rename ${vault.name}`} onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          rename.mutate();
        }}
        className="space-y-3"
      >
        <p className="text-xs text-muted">Only the display name changes — the vault and its data are untouched.</p>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoFocus
          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
        />
        {rename.isError && <p className="text-sm text-red-400">{(rename.error as Error).message}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-2 text-sm hover:border-accent">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!name.trim() || name.trim() === vault.name || rename.isPending}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
          >
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteVaultModal({ vault, onClose }: { vault: Vault; onClose: () => void }) {
  const qc = useQueryClient();
  const { active, switchVault } = useVault();
  const [typed, setTyped] = useState("");
  const remove = useMutation({
    mutationFn: async () => {
      // Move off the vault first if it's active, so no request carries a now-deleted selection.
      if (vault.vault_id === active) await switchVault(DEFAULT_VAULT);
      return deleteVault(vault.vault_id, vault.vault_id);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vaults"] });
      onClose();
    },
  });

  return (
    <Modal title={`Delete ${vault.name}`} onClose={onClose}>
      <div className="space-y-3">
        <div className="rounded-md border border-red-500/40 bg-red-500/5 px-3 py-2 text-sm text-red-300">
          This permanently removes the vault <span className="font-medium">{vault.name}</span> and{" "}
          <span className="font-semibold">all knowledge in it</span> (pages, sources, graph, history).
          This cannot be undone.
        </div>
        <p className="text-xs text-muted">
          Type the vault name <span className="font-mono">{vault.name}</span> to confirm.
        </p>
        <input
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={vault.name}
          autoFocus
          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-red-500"
        />
        {remove.isError && <p className="text-sm text-red-400">{(remove.error as Error).message}</p>}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border border-border px-3 py-2 text-sm hover:border-accent">
            Cancel
          </button>
          <button
            onClick={() => remove.mutate()}
            disabled={typed !== vault.name || remove.isPending}
            className="inline-flex items-center gap-2 rounded-md bg-red-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {remove.isPending && <Spinner />}
            Delete vault
          </button>
        </div>
      </div>
    </Modal>
  );
}
