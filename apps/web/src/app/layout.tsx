import type { Metadata, Viewport } from "next";

import { AppShell } from "@/components/app-shell";

import "./globals.css";

/**
 * The root layout.
 *
 * Two decisions worth reading.
 *
 * **No `next/font/google`.** The app has to build and run offline — the whole
 * premise is a tool you point at your own repo on your own machine, often with
 * no outbound network. A webfont that fails to fetch turns a build into a
 * mystery or a page into a flash of invisible text. The stack is a system
 * monospace for transcripts (where it genuinely matters) and a system sans for
 * chrome, declared in `globals.css`.
 *
 * **The theme is applied by an inline script, not by a hook.** A React effect
 * runs after paint, so a stored dark preference produces a white flash on every
 * navigation. The script below runs before the first paint and is the only way
 * to avoid that. It is deliberately tiny and has no dependencies.
 */

export const metadata: Metadata = {
  title: {
    default: "OpenBurrow",
    template: "%s · OpenBurrow",
  },
  description:
    "Terminal-native, protocol-grounded multi-harness agent collaboration. Watch lanes work, read the bus, replay the session.",
  applicationName: "OpenBurrow",
  // A local operator tool. Being indexed by a search engine is not a feature.
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Both themes are real, so the browser chrome should follow the page rather
  // than pick one and look wrong half the time.
  colorScheme: "light dark",
};

/**
 * Runs before paint. Reads the stored preference, falling back to the OS.
 *
 * Kept as a string rather than a module because it has to be inlined into the
 * HTML head — a separate file would be a second request that races the first
 * paint, which is the exact problem it exists to solve.
 */
const THEME_SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem("openburrow.theme");
    var explicit = ${JSON.stringify(process.env.NEXT_PUBLIC_OPENBURROW_THEME || "")};
    var resolved = stored || explicit;
    if (!resolved || resolved === "system") {
      resolved = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    if (resolved === "dark") document.documentElement.classList.add("dark");
    document.documentElement.dataset.theme = resolved;
  } catch (error) {
    /* localStorage throws in some privacy modes. A light theme is a fine
       fallback; failing to render is not. */
  }
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-screen bg-background text-foreground">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
