import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge Tailwind classes, resolving conflicts by last-wins.
 *
 * `clsx` alone concatenates, so `cn("p-2", "p-4")` would emit both and the
 * cascade would decide — which works until the two classes have equal
 * specificity and the order in the generated stylesheet, not the order in the
 * call, wins. `twMerge` makes the call order authoritative, which is what a
 * caller passing an override expects.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Whether we are rendering on the server.
 *
 * Needed because `window` is absent during prerender and a component that reads
 * it at module scope breaks the static build rather than the runtime — a failure
 * that only shows up in `next build`, which is the worst place to find it.
 */
export const isServer = typeof window === "undefined";

/**
 * Copy text to the clipboard, falling back to a hidden textarea.
 *
 * The async Clipboard API needs a secure context. The daemon's UI is routinely
 * served over plain HTTP on a LAN address, where `navigator.clipboard` is
 * undefined — so the fallback is not theoretical, it is the common case for the
 * deployment this is built for.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!isServer && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the legacy path.
    }
  }
  if (isServer) return false;
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
