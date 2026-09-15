import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

import { resolveReelsDir } from "@/lib/daemon-bridge";
import { fail } from "@/lib/http";

/**
 * Reading reels off disk.
 *
 * A reel is a *directory of files the daemon already wrote* — `reel.json`,
 * `manifest.json`, `index.html`, the `.cast` files, `plan.json`, `audit.json`,
 * `metrics.json`. There is no daemon method for "give me the reel", and adding
 * one would mean streaming a multi-megabyte bundle through a request/response
 * socket protocol that has an 8 MiB frame cap. Reading the files is simpler and
 * has no size ceiling.
 *
 * The security surface here is entirely the path. A reel id arrives from a URL,
 * so `..%2f..%2fetc%2fpasswd` is the attack, and the defence is not
 * `path.normalize` — it is refusing any id that is not shaped like an id.
 */

/**
 * Session ids are `sess_` + a ULID, but the *reel directory* is named by
 * whoever called the exporter, so the check is on shape rather than on prefix:
 * alphanumerics, underscore, hyphen, dot — starting and ending with an
 * alphanumeric. That admits `sess_01HQ…` and `2026-09-15-nightly` and refuses
 * every traversal, every absolute path, and every name that a shell would treat
 * as an option.
 */
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export function safeIdOrNull(id: string): string | null {
  if (!id || id.length > 200) return null;
  if (!SAFE_ID.test(id)) return null;
  // `.` and `..` match the character class, so they are excluded explicitly.
  if (id === "." || id === "..") return null;
  return id;
}

export interface ReelLocation {
  /** The directory holding the bundle, when the exporter wrote one. */
  directory: string | null;
  /** A bare `reel.json` next to the reels directory, when there is no directory. */
  bundleFile: string | null;
}

/**
 * Locate a reel, accepting both layouts the exporter can produce.
 *
 * `export_reel(directory=…)` writes into a directory; some callers pass
 * `<reels>/<id>.json` and write a single file. Supporting both here means the
 * viewer works regardless of which entry point produced the reel, rather than
 * showing "not found" for a reel that plainly exists.
 */
export async function locateReel(id: string): Promise<ReelLocation> {
  const reelsDir = resolveReelsDir();
  const candidateDir = path.join(reelsDir, id);

  // Belt and braces: even with the shape check above, confirm the resolved path
  // is still inside the reels directory. A symlink or a platform quirk that made
  // the regex insufficient would be caught here rather than by a reader.
  if (!isInside(reelsDir, candidateDir)) return { directory: null, bundleFile: null };

  try {
    const info = await stat(candidateDir);
    if (info.isDirectory()) {
      return { directory: candidateDir, bundleFile: null };
    }
  } catch {
    // Not a directory. Fall through to the flat-file layout.
  }

  const flat = path.join(reelsDir, `${id}.json`);
  if (!isInside(reelsDir, flat)) return { directory: null, bundleFile: null };
  try {
    await stat(flat);
    return { directory: null, bundleFile: flat };
  } catch {
    return { directory: null, bundleFile: null };
  }
}

function isInside(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

/** Read a JSON file, or null when it is absent or unparseable. */
export async function readJsonFile<T = unknown>(file: string): Promise<T | null> {
  try {
    const text = await readFile(file, "utf8");
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

export async function readTextFile(file: string): Promise<string | null> {
  try {
    return await readFile(file, "utf8");
  } catch {
    return null;
  }
}

/** Every file in a reel directory, for the "what is in this bundle" panel. */
export async function listReelFiles(directory: string): Promise<Array<{ name: string; bytes: number }>> {
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    const files = await Promise.all(
      entries
        .filter((entry) => entry.isFile())
        .map(async (entry) => {
          try {
            const info = await stat(path.join(directory, entry.name));
            return { name: entry.name, bytes: info.size };
          } catch {
            return { name: entry.name, bytes: 0 };
          }
        }),
    );
    return files.sort((a, b) => a.name.localeCompare(b.name));
  } catch {
    return [];
  }
}

/** The standard "no reel here" answer, used by both the viewer and the download route. */
export function reelNotFound(id: string): Response {
  return fail(
    {
      code: "openburrow.web.reel_not_found",
      message: `no reel for ${id}`,
      hint: "Export one with `burrow reel export`, then reload. The viewer reads what the daemon already wrote to disk.",
      context: { reel_id: id, reels_dir: resolveReelsDir() },
    },
    404,
  );
}
