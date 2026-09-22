/**
 * A `module.register()` load hook that forces `daemon-bridge.ts` down its POSIX
 * branch, so that branch can be exercised on a Windows host.
 *
 * Why this exists
 * ---------------
 * `resolveDaemonEndpoint` picks its transport from a module-scope constant:
 *
 *     const isWindows = platform() === "win32";
 *
 * That is read once, at import time, and it means the POSIX branch — the branch
 * Docker and Linux actually run, and therefore the *primary* deployment — had
 * never executed on the machine this was developed on. A branch that has never
 * run is not tested, whatever the file looks like.
 *
 * This hook rewrites that one line **in the loaded source**, never on disk, so
 * production code stays honest and the branch becomes observable. It is the same
 * technique `scripts/falsify_stage13.py` uses to reconstruct removed behaviour,
 * and it is deliberately confined to a test harness.
 *
 * Only URLs carrying `?posix=1` are rewritten, so the natural (Windows) module
 * and the forced one can be imported into the same process and compared against
 * each other. The rewrite is asserted: if the anchor ever stops matching — a
 * formatter reflowing the line, a rename — this throws rather than quietly
 * returning the unmodified source, because a silent miss would test the Windows
 * branch twice and report agreement between two identical things.
 *
 * What it does *not* do
 * ---------------------
 * It does not make the process POSIX. `node:path` still joins with backslashes
 * and `node:os.homedir()` still returns the Windows home. That is the point:
 * both the Python and the TypeScript side then compute their POSIX-branch logic
 * over the same Windows path primitives, so the comparison is like-for-like
 * about *structure* — which directory the socket is looked for in — without
 * pretending to be a Linux box.
 */

const ANCHOR = 'const isWindows = platform() === "win32";';
const REPLACEMENT = "const isWindows = false; // forced by _web_posix_hook.mjs";

/** Only this module, and only when the importer asked for the POSIX variant. */
function isTarget(url) {
  return url.includes("daemon-bridge.ts") && url.includes("posix=1");
}

export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);

  if (!isTarget(url)) return result;

  if (typeof result.source !== "string" && !(result.source instanceof Uint8Array)) {
    throw new Error(`_web_posix_hook: no source to rewrite for ${url}`);
  }

  const source = result.source.toString();
  const occurrences = source.split(ANCHOR).length - 1;

  if (occurrences !== 1) {
    throw new Error(
      `_web_posix_hook: expected exactly 1 occurrence of ${JSON.stringify(ANCHOR)} ` +
        `in ${url}, found ${occurrences}. The anchor has drifted; the POSIX branch ` +
        `was NOT exercised. Update the anchor rather than ignoring this.`,
    );
  }

  return { ...result, source: source.replace(ANCHOR, REPLACEMENT) };
}
