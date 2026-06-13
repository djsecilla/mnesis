import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getGraph, search } from "../api/endpoints";
import { entityColor, entityTypeOf } from "../design/tokens";

interface Item {
  kind: "page" | "entity" | "action";
  key: string;
  label: string;
  sublabel: string;
  to: string;
}

export default function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const q = query.trim();

  // Pages via the search endpoint; entities via a bounded graph overview, filtered.
  const pages = useQuery({
    queryKey: ["palette-search", q],
    queryFn: () => search(q, 8),
    enabled: open && q.length > 0,
    staleTime: 10_000,
  });
  const graph = useQuery({
    queryKey: ["palette-graph"],
    queryFn: () => getGraph({}),
    enabled: open,
    staleTime: 60_000,
  });

  const items = useMemo<Item[]>(() => {
    const ql0 = q.toLowerCase();
    const ACTIONS = [
      { key: "action:add", label: "Add to Mnesis", sublabel: "paste or upload a new source", to: "/add" },
      { key: "action:batch", label: "Add several", sublabel: "batch ingest multiple files", to: "/add/batch" },
      { key: "action:sources", label: "Sources", sublabel: "what you fed in", to: "/sources" },
      { key: "action:review", label: "Review contradictions", sublabel: "resolve conflicting pages", to: "/review" },
    ];
    const actionItems: Item[] = ACTIONS.filter(
      (a) => !q || a.label.toLowerCase().includes(ql0),
    ).map((a) => ({ kind: "action", ...a }));
    const pageItems: Item[] = (pages.data?.hits ?? []).map((h) => ({
      kind: "page",
      key: `page:${h.id}`,
      label: h.title,
      sublabel: h.id,
      to: `/pages/${encodeURIComponent(h.id)}`,
    }));
    const ql = q.toLowerCase();
    const entityItems: Item[] = (graph.data?.nodes ?? [])
      .filter((n) => !ql || n.ref.toLowerCase().includes(ql))
      .filter((n) => n.type !== "page")
      .slice(0, 6)
      .map((n) => ({
        kind: "entity",
        key: `entity:${n.ref}`,
        label: n.ref,
        sublabel: `${n.type} · degree ${n.degree}`,
        to: `/graph?root=${encodeURIComponent(n.ref)}`,
      }));
    return [...actionItems, ...pageItems, ...entityItems];
  }, [pages.data, graph.data, q]);

  useEffect(() => {
    if (active >= items.length) setActive(0);
  }, [items.length, active]);

  if (!open) return null;

  function go(item: Item | undefined) {
    if (!item) return;
    onClose();
    navigate(item.to);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(items[active]);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Search pages and entities…"
          className="w-full border-b border-border bg-transparent px-4 py-3 text-fg placeholder:text-muted focus:outline-none"
        />
        <ul className="max-h-80 overflow-auto py-1">
          {items.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-muted">
              {q ? "No matches" : "Type to search pages and entities"}
            </li>
          )}
          {items.map((item, i) => (
            <li key={item.key}>
              <button
                onMouseEnter={() => setActive(i)}
                onClick={() => go(item)}
                className={`flex w-full items-center gap-3 px-4 py-2 text-left ${
                  i === active ? "bg-elev" : ""
                }`}
              >
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{
                    background:
                      item.kind === "entity" ? entityColor(entityTypeOf(item.label)) : "var(--accent)",
                  }}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm text-fg">{item.label}</span>
                  <span className="block truncate text-xs text-muted">{item.sublabel}</span>
                </span>
                <span className="text-[10px] uppercase text-muted">{item.kind}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
