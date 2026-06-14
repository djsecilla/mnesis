import { useQuery } from "@tanstack/react-query";
import { Suspense, useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { listReviews } from "../api/endpoints";
import CommandPalette from "./CommandPalette";
import { ChatIcon, GraphIcon, PagesIcon, PlusIcon, ReviewIcon, SearchIcon, SourcesIcon } from "./Icon";
import Logo, { BrandSplash } from "./Logo";
import ThemeToggle from "./ThemeToggle";

const rail = [
  { to: "/graph", label: "Graph", Icon: GraphIcon },
  { to: "/pages", label: "Pages", Icon: PagesIcon },
  { to: "/sources", label: "Sources", Icon: SourcesIcon },
  { to: "/chat", label: "Chat", Icon: ChatIcon },
];

function ReviewRailLink() {
  // Open-contradiction count, polled so the badge reflects new conflicts and
  // clears as they are resolved (resolving invalidates this query immediately).
  const { data } = useQuery({ queryKey: ["reviews"], queryFn: listReviews, refetchInterval: 30_000 });
  const count = data?.total ?? 0;
  return (
    <NavLink
      to="/review"
      title={`Review${count ? ` (${count} open)` : ""}`}
      aria-label="Review"
      className={({ isActive }) => `rail-btn relative ${isActive ? "rail-active" : ""}`}
    >
      <ReviewIcon />
      {count > 0 && (
        <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-medium text-black">
          {count}
        </span>
      )}
    </NavLink>
  );
}

export default function Shell() {
  const [paletteOpen, setPaletteOpen] = useState(false);

  // Global Cmd/Ctrl-K opens the command palette.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="flex h-screen flex-col bg-bg text-fg">
      {/* Brand header — persistent across every page. The lockup is home. */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <NavLink
          to="/graph"
          title="mnesis — home"
          aria-label="mnesis — home"
          className="flex items-center rounded-md text-fg transition-opacity hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Logo lockup size={34} />
        </NavLink>
        <ThemeToggle />
      </header>

      <div className="flex flex-1 overflow-hidden">
        <nav className="flex w-14 flex-col items-center gap-1 border-r border-border py-3">
          <NavLink
            to="/add"
            title="Add to Mnesis"
            aria-label="Add to Mnesis"
            className="mb-1 flex h-9 w-9 items-center justify-center rounded-lg bg-accent text-accent-fg transition hover:opacity-90"
          >
            <PlusIcon />
          </NavLink>
          <button
            onClick={() => setPaletteOpen(true)}
            className="rail-btn"
            title="Search (⌘K)"
            aria-label="Search"
          >
            <SearchIcon />
          </button>
          <div className="my-2 h-px w-6 bg-border" />
          {rail.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              title={label}
              aria-label={label}
              className={({ isActive }) => `rail-btn ${isActive ? "rail-active" : ""}`}
            >
              <Icon />
            </NavLink>
          ))}
          <ReviewRailLink />
        </nav>

        <main className="flex-1 overflow-auto">
          <Suspense fallback={<BrandSplash animate tagline="Loading…" />}>
            <Outlet />
          </Suspense>
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
