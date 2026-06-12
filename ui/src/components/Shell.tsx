import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import CommandPalette from "./CommandPalette";
import { ChatIcon, GraphIcon, PagesIcon, SearchIcon } from "./Icon";
import ThemeToggle from "./ThemeToggle";

const rail = [
  { to: "/graph", label: "Graph", Icon: GraphIcon },
  { to: "/pages", label: "Pages", Icon: PagesIcon },
  { to: "/chat", label: "Chat", Icon: ChatIcon },
];

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
    <div className="flex h-screen bg-bg text-fg">
      <nav className="flex w-14 flex-col items-center gap-1 border-r border-border py-3">
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
        <div className="mt-auto">
          <ThemeToggle />
        </div>
      </nav>

      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
