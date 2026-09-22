/**
 * The Node half of `scripts/check_endpoint_parity.py`.
 *
 * Reads one JSON request on stdin, writes one JSON response on stdout, and
 * keeps stderr free for warnings so the caller can parse stdout directly.
 *
 * Request
 * -------
 *   {
 *     "repo": "<absolute repo root, used when a scenario does not set one>",
 *     "scenarios": [
 *       { "name": "…", "env": { "OPENBURROW_HOME": "…", … }, "posix": false }
 *     ]
 *   }
 *
 * An env value of `null` means "unset". `posix: true` selects the module instance
 * whose `isWindows` was forced to false by `_web_posix_hook.mjs`.
 *
 * Response
 * --------
 *   { "observations": [
 *       { "name": "…", "repo_root": …, "endpoint": …, "reels": …, "global": … }
 *   ] }
 *
 * A call that throws is recorded as `{"threw": "<message>"}` rather than
 * aborting the run: the Python side may legitimately raise where TypeScript
 * returns, and that difference is a finding, not a crash.
 */

import { register } from "node:module";
import { readFileSync } from "node:fs";

const BRIDGE = new URL("../apps/web/src/lib/daemon-bridge.ts", import.meta.url).href;
const HOOK = new URL("./_web_posix_hook.mjs", import.meta.url).href;

/** Every variable either side reads. Reset per scenario so nothing leaks. */
const MANAGED_ENV = [
  "OPENBURROW_REPO_ROOT",
  "OPENBURROW_HOME",
  "OPENBURROW_GLOBAL_HOME",
  "OPENBURROW_DAEMON_SOCKET",
];

const request = JSON.parse(readFileSync(0, "utf8"));

// Registered before the POSIX import, and keyed on the `?posix=1` query, so the
// natural module is imported untouched. See `_web_posix_hook.mjs`.
register(HOOK, import.meta.url);

const natural = await import(BRIDGE);
const posix = await import(`${BRIDGE}?posix=1`);

/** Guard against the hook silently not applying — see the hook's docstring. */
function assertPosixBranchIsForced() {
  process.env.OPENBURROW_DAEMON_SOCKET = "";
  delete process.env.OPENBURROW_DAEMON_SOCKET;
  process.env.OPENBURROW_REPO_ROOT = request.repo;

  const fromNatural = natural.describeEndpoint().transport;
  const fromPosix = posix.describeEndpoint().transport;

  if (fromNatural !== "named-pipe" || fromPosix !== "unix-socket") {
    throw new Error(
      `_web_endpoint_probe: the POSIX branch was not forced ` +
        `(natural=${fromNatural}, posix=${fromPosix}). Comparing these two would ` +
        `compare the Windows branch against itself.`,
    );
  }
}

assertPosixBranchIsForced();

function observe(fn) {
  try {
    return fn();
  } catch (error) {
    return { threw: error instanceof Error ? error.message : String(error) };
  }
}

const observations = request.scenarios.map((scenario) => {
  for (const key of MANAGED_ENV) delete process.env[key];
  process.env.OPENBURROW_REPO_ROOT = scenario.repo ?? request.repo;

  for (const [key, value] of Object.entries(scenario.env ?? {})) {
    if (value === null) delete process.env[key];
    else process.env[key] = value;
  }

  const mod = scenario.posix ? posix : natural;

  return {
    name: scenario.name,
    repo_root: observe(() => mod.resolveRepoRoot()),
    endpoint: observe(() => mod.resolveDaemonEndpoint()),
    reels: observe(() => mod.resolveReelsDir()),
    global: observe(() => mod.resolveGlobalDir()),
  };
});

process.stdout.write(`${JSON.stringify({ observations })}\n`);
