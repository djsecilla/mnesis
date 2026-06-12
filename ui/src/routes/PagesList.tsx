import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listPages } from "../api/endpoints";
import type { PageSummary } from "../api/types";
import { KindBadge, StatusBadge } from "../components/Badges";
import { ConfidenceBar } from "../components/Confidence";

export default function PagesList() {
  const navigate = useNavigate();
  const { data, isLoading, error } = useQuery({ queryKey: ["pages"], queryFn: () => listPages() });

  const [kind, setKind] = useState("all");
  const [status, setStatus] = useState("all");
  const [tag, setTag] = useState("all");
  const [text, setText] = useState("");
  const [active, setActive] = useState(0);
  const activeRef = useRef<HTMLLIElement>(null);

  const pages: PageSummary[] = data?.pages ?? [];
  const allTags = useMemo(
    () => Array.from(new Set(pages.flatMap((p) => p.tags))).sort(),
    [pages],
  );

  const filtered = useMemo(() => {
    const t = text.trim().toLowerCase();
    return pages.filter(
      (p) =>
        (kind === "all" || p.kind === kind) &&
        (status === "all" || p.status === status) &&
        (tag === "all" || p.tags.includes(tag)) &&
        (!t || p.title.toLowerCase().includes(t) || p.id.toLowerCase().includes(t)),
    );
  }, [pages, kind, status, tag, text]);

  useEffect(() => {
    setActive(0);
  }, [kind, status, tag, text]);

  // j/k or arrow navigation (when not typing); Enter opens. Cmd-K stays global.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || el.tagName === "INPUT" || el.tagName === "SELECT") return;
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        setActive((a) => Math.min(a + 1, filtered.length - 1));
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => Math.max(a - 1, 0));
      } else if (e.key === "Enter" && filtered[active]) {
        navigate(`/pages/${encodeURIComponent(filtered[active].id)}`);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [filtered, active, navigate]);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest" });
  }, [active]);

  return (
    <div className="mx-auto max-w-3xl p-8">
      <header className="mb-4 flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Pages</h1>
        <span className="text-sm text-muted">{filtered.length} shown</span>
      </header>

      <div className="mb-4 flex flex-wrap gap-2">
        <input
          className="input flex-1 py-1.5"
          placeholder="Filter by title or id…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <Select value={kind} onChange={setKind} options={["all", "fact", "digest", "note"]} />
        <Select value={status} onChange={setStatus} options={["all", "active", "stale"]} />
        <Select value={tag} onChange={setTag} options={["all", ...allTags]} />
      </div>

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-muted">Could not load pages — is the API running?</p>}

      <ul className="divide-y divide-border">
        {filtered.map((p, i) => (
          <li key={p.id} ref={i === active ? activeRef : null}>
            <button
              onMouseEnter={() => setActive(i)}
              onClick={() => navigate(`/pages/${encodeURIComponent(p.id)}`)}
              className={`flex w-full items-center gap-3 py-3 text-left ${
                i === active ? "bg-elev" : ""
              } ${p.status === "stale" ? "opacity-60" : ""}`}
            >
              <KindBadge kind={p.kind} />
              <span className="min-w-0 flex-1">
                <span className="flex items-center">
                  <span className="truncate">{p.title}</span>
                  <StatusBadge status={p.status} />
                </span>
                <span className="block truncate text-xs text-muted">{p.id}</span>
              </span>
              <ConfidenceBar value={p.confidence} />
              <span className="w-20 shrink-0 text-right text-xs text-muted">
                {p.updated.slice(0, 10)}
              </span>
            </button>
          </li>
        ))}
        {data && filtered.length === 0 && (
          <li className="py-6 text-muted">No pages match these filters.</li>
        )}
      </ul>
    </div>
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-border bg-elev px-2 py-1.5 text-sm text-fg focus:border-accent focus:outline-none"
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}
