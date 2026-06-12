import { useTheme } from "../hooks/useTheme";
import { MoonIcon, SunIcon } from "./Icon";

export default function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button className="rail-btn" onClick={toggle} title="Toggle theme" aria-label="Toggle theme">
      {theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}
