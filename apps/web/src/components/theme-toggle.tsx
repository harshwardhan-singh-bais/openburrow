"use client";

import { useEffect, useState } from "react";

/**
 * Light / dark / system.
 *
 * Three states rather than a boolean, because "follow the OS" is a real
 * preference and a toggle that only has two positions silently converts it into
 * a fixed choice the first time someone touches it.
 *
 * The initial value is read from the DOM rather than from `localStorage`,
 * because the inline script in `layout.tsx` has already resolved it and
 * re-resolving here is a second source of truth that can disagree with what is
 * painted.
 */

type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "openburrow.theme";

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY) as Theme | null;
    setTheme(stored ?? "system");
  }, []);

  // Track the OS while in system mode. Without this listener, someone who
  // switches their laptop to dark at sunset sees a page that disagrees with
  // every other window until they reload.
  useEffect(() => {
    if (theme !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.classList.toggle("dark", media.matches);
      document.documentElement.dataset.theme = media.matches ? "dark" : "light";
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  const choose = (next: Theme) => {
    setTheme(next);
    if (next === "system") {
      window.localStorage.removeItem(STORAGE_KEY);
      const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      document.documentElement.classList.toggle("dark", prefersDark);
      document.documentElement.dataset.theme = prefersDark ? "dark" : "light";
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, next);
    document.documentElement.classList.toggle("dark", next === "dark");
    document.documentElement.dataset.theme = next;
  };

  const options: Array<{ value: Theme; label: string }> = [
    { value: "light", label: "Light" },
    { value: "dark", label: "Dark" },
    { value: "system", label: "System" },
  ];

  return (
    <div
      role="radiogroup"
      aria-label="Colour scheme"
      className="flex items-center rounded-md border border-border p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={theme === option.value}
          onClick={() => choose(option.value)}
          className={
            theme === option.value
              ? "rounded bg-secondary px-2 py-1 text-xs font-medium text-secondary-foreground"
              : "rounded px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
          }
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
