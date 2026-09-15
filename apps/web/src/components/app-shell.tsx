"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

/**
 * The chrome around every page.
 *
 * A client component only because the active nav item depends on the current
 * path. The alternative — making each page declare its own nav state — is how a
 * nav bar ends up highlighting two items at once on a nested route.
 */

const NAV = [
  { href: "/", label: "Board", exact: true },
  { href: "/sessions", label: "Sessions", exact: false },
  { href: "/reels", label: "Reels", exact: false },
  { href: "/relay", label: "Relay", exact: false },
  { href: "/system", label: "System", exact: false },
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 border-b border-border bg-background/85 backdrop-blur supports-[backdrop-filter]:bg-background/70">
        <div className="mx-auto flex h-14 w-full max-w-[1400px] items-center gap-4 px-4 sm:px-6">
          <Link href="/" className="flex shrink-0 items-center gap-2">
            {/* An inline mark rather than an image: no request, no layout shift,
                and it inherits the foreground colour in both themes. */}
            <svg viewBox="0 0 20 20" aria-hidden="true" className="size-5 text-state-working">
              <path
                d="M3 4.5h14M3 10h9M3 15.5h5"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                fill="none"
              />
            </svg>
            <span className="text-sm font-semibold tracking-tight">OpenBurrow</span>
          </Link>

          <nav aria-label="Main" className="flex items-center gap-1 overflow-x-auto">
            {NAV.map((item) => {
              const active = item.exact
                ? pathname === item.href
                : pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "rounded-md px-2.5 py-1.5 text-sm whitespace-nowrap transition-colors",
                    active
                      ? "bg-secondary text-secondary-foreground"
                      : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6">{children}</main>

      <footer className="border-t border-border">
        <div className="mx-auto flex w-full max-w-[1400px] flex-wrap items-center gap-x-4 gap-y-1 px-4 py-4 text-xs text-muted-foreground sm:px-6">
          <span>
            The bus log is the record. Everything on this page is a view of it, and views can be
            behind.
          </span>
          <span className="ml-auto font-mono">burrow --version</span>
        </div>
      </footer>
    </div>
  );
}
