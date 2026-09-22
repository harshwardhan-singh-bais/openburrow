"use client";

import { useEffect } from "react";

import { useLocalStorage } from "@/lib/local-storage";

/**
 * Light / dark / system.
 *
 * Three states rather than a boolean, because "follow the OS" is a real
 * preference and a toggle that only has two positions silently converts it into
 * a fixed choice the first time someone touches it.
 *
 * The stored value is read through `useLocalStorage`, not seeded by a mount
 * effect. The effect version rendered once with a value known to be wrong and
 * then rendered again synchronously; `useSyncExternalStore` is the API for
 * reading an external store, and it is the one that gives the server a snapshot
 * so the markup matches before the client re-reads.
 *
 * **`system` is stored, not erased.** It used to remove the key, which made
 * "never chose" and "chose System" the same state — and they are not the same
 * once `NEXT_PUBLIC_OPENBURROW_THEME` supplies a default for "never chose".
 * Keeping the three values distinct is what makes that variable mean anything:
 * without it the toggle reads the build-time default as the selection, and
 * clicking System appears to do nothing.
 *
 * Applying the theme to the DOM happens in one effect keyed on `theme`, not in
 * the click handler. Two places writing `classList` and `data-theme` is how they
 * drift, and a theme applied imperatively is a theme that is wrong after any
 * render that did not go through the handler.
 */

type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "openburrow.theme";

/**
 * The build-time default, for a deployment that wants to start dark without
 * asking. Empty — the shipped default — means "no opinion, follow the OS".
 *
 * Read here rather than in the click path because Next substitutes
 * `process.env.NEXT_PUBLIC_*` at build time; a name assembled at runtime would
 * be `undefined` in the bundle with no error to say so.
 */
const ENV_THEME = process.env.NEXT_PUBLIC_OPENBURROW_THEME ?? "";

const OPTIONS: Array<{ value: Theme; label: string }> = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

function isTheme(value: string | null): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

export function ThemeToggle() {
  const [stored, setStored] = useLocalStorage(STORAGE_KEY);

  // Precedence mirrors the pre-paint script in `layout.tsx` exactly: a stored
  // choice wins, then the build-time default, then the OS. If the two ever
  // disagree the toggle shows one theme while the page is painted in another,
  // and the disagreement is invisible until someone notices the colours.
  const theme: Theme = isTheme(stored) ? stored : isTheme(ENV_THEME) ? ENV_THEME : "system";

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");

    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && media.matches);
      document.documentElement.classList.toggle("dark", dark);
      // `setAttribute`, not `dataset.theme = …`. The assignment form mutates a
      // property of a host object, which the React Compiler's immutability rule
      // cannot tell is contained — and the attribute is what the CSS reads, so
      // there is nothing lost by going through the DOM API.
      document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    };

    apply();

    // Only "system" has anything to follow. Listening in the other two modes
    // would repaint the page against the user's explicit choice the moment their
    // OS switched, which is the bug the three-state toggle exists to avoid.
    if (theme !== "system") return;
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  return (
    <div
      role="radiogroup"
      aria-label="Colour scheme"
      className="flex items-center rounded-md border border-border p-0.5"
    >
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={theme === option.value}
          // Writing the store is the whole action; the effect above applies it.
          onClick={() => setStored(option.value)}
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
