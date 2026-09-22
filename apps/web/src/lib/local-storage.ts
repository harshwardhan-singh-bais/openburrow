"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * `localStorage` as a React external store.
 *
 * The two obvious ways to read a stored value both fail, and each fails for a
 * different reason:
 *
 * - **In the component body.** `window` does not exist on the server, and the
 *   server cannot know what the browser holds, so the markup it produces differs
 *   from the first client render. That is a hydration mismatch.
 * - **In a mount effect, calling `setState`.** That renders once with a value
 *   known to be wrong and then renders again immediately. React 19's
 *   `react-hooks/set-state-in-effect` rejects it, and the rule is not being
 *   pedantic: the second render is synchronous, so every page load pays for it.
 *
 * `useSyncExternalStore` is the API for this shape. It takes a *server* snapshot
 * — `null` here — so the hydration render matches the server's, then re-reads on
 * the client and re-renders once if the two differ. That one re-render is
 * unavoidable: the browser knows something the server could not.
 *
 * Writes notify subscribers directly, because the `storage` event only fires in
 * *other* documents — without that, `setValue` would not update the component
 * that called it. The `storage` listener covers the other direction, so two tabs
 * showing the same setting stay in step.
 *
 * The snapshot is a string, so `getSnapshot` needs no cache: React compares
 * snapshots with `Object.is`, and two equal strings are `Object.is`-equal. A
 * snapshot that built a fresh object per call would re-render forever, which is
 * the usual way this hook is got wrong.
 */

type Listener = () => void;

const listeners = new Map<string, Set<Listener>>();
let listeningToStorage = false;

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Access itself throws in Safari's private mode rather than returning null.
    // A page that cannot read its own settings should render the defaults, not
    // fail to render at all.
    return null;
  }
}

function emit(key: string): void {
  for (const listener of listeners.get(key) ?? []) listener();
}

/**
 * One `storage` listener for the whole module rather than one per subscriber:
 * the event carries the key, so it can be routed, and a page with five stored
 * values should not add five window listeners.
 */
function listenToStorage(): void {
  if (listeningToStorage || typeof window === "undefined") return;
  listeningToStorage = true;
  window.addEventListener("storage", (event) => {
    // `localStorage.clear()` — the event carries no key, so every key changed.
    if (event.key === null) {
      for (const key of listeners.keys()) emit(key);
      return;
    }
    if (event.storageArea && event.storageArea !== window.localStorage) return;
    emit(event.key);
  });
}

function subscribe(key: string, listener: Listener): () => void {
  listenToStorage();
  let group = listeners.get(key);
  if (!group) {
    group = new Set();
    listeners.set(key, group);
  }
  group.add(listener);
  return () => {
    group.delete(listener);
    if (group.size === 0) listeners.delete(key);
  };
}

/**
 * Module-level so the identity is stable across renders.
 * `useSyncExternalStore` re-subscribes whenever the function it is given changes.
 */
const getServerSnapshot = (): null => null;

export function useLocalStorage(key: string): [string | null, (value: string | null) => void] {
  const subscribeToKey = useCallback((listener: Listener) => subscribe(key, listener), [key]);
  const getSnapshot = useCallback(() => read(key), [key]);

  const value = useSyncExternalStore(subscribeToKey, getSnapshot, getServerSnapshot);

  const setValue = useCallback(
    (next: string | null) => {
      try {
        if (next === null) window.localStorage.removeItem(key);
        else window.localStorage.setItem(key, next);
      } catch {
        // A full or blocked store. The subscribers still fire, so the UI stays
        // consistent with itself for the rest of the session — it just will not
        // survive a reload, and there is nothing useful to say to the user about
        // that which they could act on.
      }
      emit(key);
    },
    [key],
  );

  return [value, setValue];
}
