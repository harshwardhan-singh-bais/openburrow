import { createHash } from "node:crypto";
import type * as NodeFs from "node:fs";
import { connect, type Socket } from "node:net";
import { homedir, platform } from "node:os";
import path from "node:path";

/**
 * The daemon's control plane, spoken from Node.
 *
 * This module exists because the daemon does **not** speak HTTP. Its control
 * plane is newline-delimited JSON-RPC over a Unix domain socket on POSIX and a
 * named pipe on Windows — chosen that way on purpose, because filesystem
 * permissions are the access control and a socket at mode `0600` has no port to
 * scan and no firewall rule to get wrong.
 *
 * The consequence for the frontend is direct: the browser cannot reach the
 * daemon, and it should not be able to. These route handlers are the bridge.
 * They run on the server, they hold the only copy of the daemon's endpoint, and
 * the browser sees an ordinary JSON API. That is also why
 * `OPENBURROW_DAEMON_SOCKET` is deliberately *not* prefixed `NEXT_PUBLIC_` — a
 * control-plane endpoint in the bundle is a control-plane endpoint someone will
 * eventually call from a laptop on the same network.
 *
 * Two rules are inherited from the CLI, and they are worth restating because
 * breaking either produces a bridge that lies:
 *
 * 1. **A call either returns the daemon's result or throws.** There is no
 *    "return a plausible default" path. A dashboard that shows an empty lane
 *    list when the daemon is down is a dashboard that reports success.
 * 2. **One connection per call.** The daemon's protocol is request/response;
 *    the CLI opens, sends, reads, and exits. Pooling here would be an
 *    optimisation with a real failure mode — a pooled socket that dies between
 *    requests turns into an intermittent error the user cannot reproduce.
 */

/** Matches `MAX_FRAME_BYTES` in `openburrow/daemon/ipc.py`. */
const MAX_FRAME_BYTES = 8 * 1024 * 1024;

/** A dashboard request that takes longer than this is a request worth failing. */
const DEFAULT_TIMEOUT_MS = 20_000;

/**
 * The error envelope, identical in shape to the daemon's and the relay's.
 *
 * The frontend parses one error format, everywhere, so `ApiError` in
 * `lib/api.ts` does not need a second branch for bridge-originated failures.
 */
export interface BridgeErrorPayload {
  code: string;
  message: string;
  hint?: string;
  context?: Record<string, unknown>;
}

export class DaemonBridgeError extends Error {
  readonly code: string;
  readonly hint?: string;
  readonly context: Record<string, unknown>;
  /** The HTTP status the route handler should answer with. */
  readonly status: number;

  constructor(payload: BridgeErrorPayload, status: number) {
    super(payload.message);
    this.name = "DaemonBridgeError";
    this.code = payload.code;
    this.hint = payload.hint;
    this.context = payload.context ?? {};
    this.status = status;
  }

  toResponseBody(): { error: BridgeErrorPayload } {
    return {
      error: {
        code: this.code,
        message: this.message,
        ...(this.hint ? { hint: this.hint } : {}),
        context: this.context,
      },
    };
  }
}

// ---------------------------------------------------------------------------
// Endpoint resolution — a deliberate mirror of `openburrow.core.paths`
// ---------------------------------------------------------------------------

const isWindows = platform() === "win32";

/**
 * Expand a leading `~` the way Python's `Path.expanduser()` does.
 *
 * `openburrow.core.paths` calls `expanduser()` on `OPENBURROW_REPO_ROOT`,
 * `OPENBURROW_HOME`, `OPENBURROW_GLOBAL_HOME` and — on POSIX only — the
 * `OPENBURROW_DAEMON_SOCKET` override. Node has no equivalent, so without this
 * an `OPENBURROW_HOME=~/obhome` resolved to `<repo>/~/obhome` here and to
 * `<home>/obhome` in the CLI: the dashboard went looking for the daemon inside a
 * directory literally named `~`, and reported only that it was unreachable.
 *
 * Only a bare `~` and a leading `~/` or `~\` are expanded, which is what
 * `expanduser` does. The `~user` form is deliberately not attempted: Python
 * resolves it through `pwd`, which has no Node counterpart, so a `~user` path
 * resolves differently here. That is recorded rather than left to be found, and
 * `scripts/check_endpoint_parity.py` pins it.
 */
function expandUser(candidate: string): string {
  if (candidate === "~") return homedir();
  if (candidate.startsWith("~/") || candidate.startsWith("~\\")) {
    return path.join(homedir(), candidate.slice(2));
  }
  return candidate;
}

/**
 * Which repo the dashboard is looking at.
 *
 * `OPENBURROW_REPO_ROOT` wins because the web app is very often started from
 * somewhere that is not the repo — a systemd unit, a container, `next dev` from
 * a parent directory. Guessing from `process.cwd()` without an override is how
 * a dashboard silently shows you a *different* repo's sessions.
 */
export function resolveRepoRoot(): string {
  const configured = process.env.OPENBURROW_REPO_ROOT?.trim();
  if (configured) return path.resolve(expandUser(configured));

  // Mirrors `find_repo_root`: prefer an initialised repo, fall back to any VCS
  // marker, so an uninitialised checkout still resolves rather than erroring.
  //
  // The `turbopackIgnore` annotations below are load-bearing, not noise. This
  // loop deliberately probes paths *outside* the app directory, walking up
  // towards the filesystem root, and Turbopack's static analysis cannot tell a
  // bounded probe from an arbitrary one. Without the annotation it concludes the
  // whole project may be read at runtime and traces every source file (including
  // `public/`) into the server bundle — which is both a size problem and a
  // deployment failure waiting to happen. Opting out is correct here precisely
  // because the walk is the feature: scoping it to a subfolder would mean not
  // finding the repo root.
  const markers = ["openburrow.yaml", "openburrow.yml", ".openburrow.yaml", ".git"];
  let current = process.cwd();
  for (;;) {
    for (const marker of markers) {
      if (marker === ".git") {
        // A `.git` file (not directory) is a worktree pointer and still counts.
        if (existsSyncSafe(path.join(/*turbopackIgnore: true*/ current, marker))) return current;
      } else if (existsSyncSafe(path.join(/*turbopackIgnore: true*/ current, marker))) {
        return current;
      }
    }
    const parent = path.dirname(current);
    if (parent === current) return process.cwd();
    current = parent;
  }
}

/**
 * The repo component of the Windows pipe name.
 *
 * `sha256(repo_root.toLowerCase())[:12]`, matching `BurrowPaths.pipe_name`.
 * Python hashes `str(Path)`, which on Windows is the backslash-separated,
 * drive-lettered path that `path.resolve` also produces, so the two agree
 * without further normalisation.
 *
 * The case operation has to match Python's exactly, and Python used to
 * `casefold` here. It no longer does. Measured over every Unicode codepoint,
 * `casefold` disagrees with `toLowerCase` at 352 of them — JavaScript has no
 * casefold at all — while `lower` disagrees at 55, every one of which is a
 * codepoint CPython's bundled tables do not yet know. `U+00DF` was among the
 * 352, so a repo under a `straße`-style path hashed to one pipe name in Python
 * and another here: the daemon listened on one and the dashboard dialled the
 * other, and the only symptom was "daemon unreachable".
 *
 * `lower` is also better at the job this digest exists for. `STRAßE` and
 * `straße` have to hash the same or one directory gets two daemons; `lower`
 * gives that on both sides, while `casefold` turns both into `strasse` here and
 * leaves them as `straße` there.
 *
 * For pure ASCII — every path this project has been run against — the two
 * operations are identical, so no existing pipe name moved.
 */
function repoPipeDigest(): string {
  return createHash("sha256")
    .update(resolveRepoRoot().toLowerCase(), "utf8")
    .digest("hex")
    .slice(0, 12);
}

/**
 * The socket or pipe the daemon is listening on.
 *
 * This has to agree with `BurrowPaths.ipc_endpoint` exactly, because a
 * disagreement is invisible: the dashboard reports "daemon unreachable" while
 * the CLI talks to the same daemon without complaint, and nothing in either
 * output names the pipe. There is no error to follow.
 *
 * It did not agree. The Windows branch returned `\\.\pipe\openburrow-${user}`,
 * while the Python side appends a 12-hex-character digest of the repo root. So
 * on Windows the dashboard dialled a pipe that no daemon had ever created — on
 * every machine, every time. The digest exists in Python for a real reason (two
 * repos must be able to run two daemons, and Windows refuses a second server on
 * a bound pipe name); the dashboard simply never implemented its half.
 *
 * The previous comment here claimed byte-for-byte compatibility. That claim is
 * why nobody looked: it answered the question in advance, and it was false.
 *
 * A second divergence lived in the same function and was found the same way —
 * by running both implementations against the same environment and diffing the
 * strings. `OPENBURROW_HOME` was passed to `path.resolve` without expanding a
 * leading `~`, so `~/obhome` became `<repo>/~/obhome` here against
 * `<home>/obhome` in Python. Same symptom, same silence. See `expandUser`.
 */
export function resolveDaemonEndpoint(): string {
  const override = process.env.OPENBURROW_DAEMON_SOCKET?.trim();
  if (override) {
    // Mirrors an asymmetry in `BurrowPaths` rather than smoothing it over: on
    // POSIX `socket_path` runs the override through `expanduser()`, while on
    // Windows `ipc_endpoint` returns it verbatim. Expanding on both sides would
    // read better and would be wrong — a Windows operator who set
    // `OPENBURROW_DAEMON_SOCKET=~/x` would get a dashboard that expanded the `~`
    // and a CLI that did not, which is the original bug reintroduced.
    // `path.normalize` on the POSIX side, because Python returns
    // `str(Path(override))` there and `Path` normalises separators: an override
    // of `C:/sockets/burrow.sock` comes back as `C:\sockets\burrow.sock`, so
    // returning it raw left the two components naming one socket by two
    // different strings. The Windows branch stays verbatim, matching
    // `ipc_endpoint`, which returns the override untouched.
    return isWindows ? override : path.normalize(expandUser(override));
  }

  if (isWindows) {
    const user = process.env.USERNAME || process.env.USER || "default";
    return `\\\\.\\pipe\\openburrow-${user}-${repoPipeDigest()}`;
  }

  return path.join(resolveRuntimeDir(), "burrow.sock");
}

export function resolveGlobalDir(): string {
  const override = process.env.OPENBURROW_GLOBAL_HOME?.trim();
  if (override) return path.resolve(expandUser(override));
  return path.join(homedir(), ".openburrow");
}

/** The reel directory, for serving exports the daemon wrote. */
export function resolveReelsDir(): string {
  return path.join(resolveRuntimeDir(), "reels");
}

/**
 * The repo's runtime directory — `<repo>/.openburrow/` unless `OPENBURROW_HOME`
 * overrides it.
 *
 * One copy, deliberately. This derivation was previously written out twice, once
 * inline in `resolveDaemonEndpoint` and once here, which is the same shape as
 * the bug that broke the pipe name: two copies of one derivation with nothing
 * comparing them. Had they drifted, the dashboard would have served replays out
 * of a directory the daemon never wrote to, and the symptom would have been an
 * empty list rather than an error.
 *
 * Mirrors `BurrowPaths.for_repo`, where a relative override is taken *relative
 * to the repo root* rather than to the process's working directory — which
 * matters because the web app is routinely started from somewhere else.
 */
function resolveRuntimeDir(): string {
  const override = process.env.OPENBURROW_HOME?.trim();
  if (override) return path.resolve(resolveRepoRoot(), expandUser(override));
  return path.join(resolveRepoRoot(), ".openburrow");
}

function existsSyncSafe(candidate: string): boolean {
  try {
    // Lazy so this module can be imported in an edge-runtime build without
    // pulling `node:fs` into the module graph eagerly.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = require("node:fs") as typeof NodeFs;
    return fs.existsSync(candidate);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// The call
// ---------------------------------------------------------------------------

interface IpcEnvelope {
  id?: string;
  result?: unknown;
  error?: { code?: string; message?: string; hint?: string; context?: unknown } | null;
  more?: boolean;
}

export interface CallOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
}

/**
 * Send one request and return its result.
 *
 * Streaming methods (`bus.stream`) are deliberately not supported here. The
 * daemon signals a stream with `more: true` frames and a terminating frame, and
 * a route handler that consumed it would have to hold a connection open for the
 * life of a request — which a serverless or edge deployment will not do. The
 * board polls `bus.tail` with a `since_seq` instead, which is self-healing: a
 * dropped tick is recovered by the next one, and there is no long-lived socket
 * to leak. `burrow watch` keeps the streaming path, because it is a terminal
 * that can afford to hold a connection.
 */
export async function callDaemon<T = unknown>(
  method: string,
  params: Record<string, unknown> = {},
  options: CallOptions = {},
): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal } = options;
  const endpoint = resolveDaemonEndpoint();

  const envelope = await sendEnvelope(endpoint, { id: "web", method, params }, timeoutMs, signal);

  if (envelope.error) {
    const code = typeof envelope.error.code === "string" ? envelope.error.code : "daemon_error";
    throw new DaemonBridgeError(
      {
        code,
        message:
          typeof envelope.error.message === "string"
            ? envelope.error.message
            : "the daemon refused the request",
        hint: typeof envelope.error.hint === "string" ? envelope.error.hint : undefined,
        context:
          envelope.error.context && typeof envelope.error.context === "object"
            ? (envelope.error.context as Record<string, unknown>)
            : { method },
      },
      statusForDaemonCode(code),
    );
  }

  return envelope.result as T;
}

/**
 * Map a daemon error code onto an HTTP status.
 *
 * The mapping lives here rather than at each call site so that a new daemon
 * error code has exactly one place to be classified. The default is 502 rather
 * than 500: an unmapped daemon error is the daemon's problem, and saying
 * "bad gateway" points at the right component instead of implying the web app
 * is broken.
 */
function statusForDaemonCode(code: string): number {
  switch (code) {
    case "unknown_method":
      return 501;
    case "bad_json":
    case "frame_too_large":
      return 400;
    case "session_not_found":
    case "lane_not_found":
    case "task_not_found":
      return 404;
    case "daemon_not_running":
      return 503;
    case "internal_error":
      return 500;
    default:
      return 502;
  }
}

async function sendEnvelope(
  endpoint: string,
  request: { id: string; method: string; params: Record<string, unknown> },
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<IpcEnvelope> {
  return new Promise<IpcEnvelope>((resolve, reject) => {
    let settled = false;
    let buffer = "";
    let socket: Socket;

    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      // `destroy` rather than `end`: the daemon answers one request per
      // connection, so a half-close here would leave the daemon waiting on a
      // reader that will never send again.
      socket.destroy();
      fn();
    };

    const timer = setTimeout(() => {
      finish(() =>
        reject(
          new DaemonBridgeError(
            {
              code: "openburrow.web.daemon_timeout",
              message: `the daemon did not answer ${request.method} within ${timeoutMs}ms`,
              hint: "It may be busy. Check `burrow daemon status` and the daemon log.",
              context: { method: request.method, timeout_ms: timeoutMs },
            },
            504,
          ),
        ),
      );
    }, timeoutMs);

    const onAbort = () => {
      finish(() => reject(new DOMException("aborted", "AbortError")));
    };
    signal?.addEventListener("abort", onAbort, { once: true });

    try {
      // `net.connect` handles both transports: a path that looks like a pipe
      // goes to the named-pipe implementation, anything else to a unix socket.
      socket = connect(endpoint);
    } catch (cause) {
      finish(() => reject(unreachable(cause, endpoint, request.method)));
      return;
    }

    socket.setNoDelay(true);

    socket.on("connect", () => {
      const line = `${JSON.stringify(request)}\n`;
      socket.write(line);
    });

    socket.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      if (buffer.length > MAX_FRAME_BYTES) {
        finish(() =>
          reject(
            new DaemonBridgeError(
              {
                code: "openburrow.web.response_too_large",
                message: `the daemon's response to ${request.method} exceeded 8 MiB`,
                hint: "Narrow the request — a smaller `limit`, or a `since_seq` closer to now.",
                context: { method: request.method, bytes: buffer.length },
              },
              502,
            ),
          ),
        );
        return;
      }

      const newline = buffer.indexOf("\n");
      if (newline < 0) return;

      const line = buffer.slice(0, newline);
      let parsed: IpcEnvelope;
      try {
        parsed = JSON.parse(line) as IpcEnvelope;
      } catch (cause) {
        finish(() =>
          reject(
            new DaemonBridgeError(
              {
                code: "openburrow.web.bad_daemon_frame",
                message: "the daemon sent a response that is not JSON",
                hint: "This is a bug in the daemon or a protocol version mismatch, not a bad request.",
                context: { method: request.method, sample: line.slice(0, 200) },
              },
              502,
            ),
          ),
        );
        void cause;
        return;
      }

      // A streaming reply arrives as `more: true` frames. This bridge does not
      // support streams, so rather than silently returning the first chunk and
      // pretending the request finished, it says so.
      if (parsed.more) {
        finish(() =>
          reject(
            new DaemonBridgeError(
              {
                code: "openburrow.web.stream_not_supported",
                message: `${request.method} streams and the HTTP bridge does not`,
                hint: "Use the polling equivalent (`bus.tail` with a `since_seq`) or `burrow watch`.",
                context: { method: request.method },
              },
              501,
            ),
          ),
        );
        return;
      }

      finish(() => resolve(parsed));
    });

    socket.on("error", (cause: Error) => {
      finish(() => reject(unreachable(cause, endpoint, request.method)));
    });

    socket.on("close", () => {
      // Closed before a complete line arrived: the daemon died mid-answer, or
      // something else owns the socket file. Reporting this as a success with an
      // empty result is the failure mode this whole module exists to avoid.
      finish(() =>
        reject(
          new DaemonBridgeError(
            {
              code: "openburrow.web.daemon_closed",
              message: "the daemon closed the connection without answering",
              hint: "Check `burrow daemon status` — it may have shut down or crashed mid-request.",
              context: { method: request.method, endpoint },
            },
            503,
          ),
        ),
      );
    });
  });
}

function unreachable(cause: unknown, endpoint: string, method: string): DaemonBridgeError {
  const code = (cause as NodeJS.ErrnoException)?.code;
  const detail = cause instanceof Error ? cause.message : String(cause);

  // The distinction matters to the reader: ENOENT means "wrong path, or nothing
  // ever started", ECONNREFUSED means "a socket file is there but nobody is
  // listening" — which is exactly what a crashed daemon leaves behind.
  const hint =
    code === "ENOENT"
      ? "No daemon socket at that path. Start one with `burrow daemon start`, or set OPENBURROW_DAEMON_SOCKET."
      : code === "ECONNREFUSED"
        ? "A socket exists but nothing is listening — a daemon likely crashed. Try `burrow daemon restart`."
        : code === "EACCES"
          ? "The socket is not readable by this process. The daemon restricts it to its owner (mode 0600), so run the web app as the same user."
          : "Check that the daemon is running and that OPENBURROW_DAEMON_SOCKET points at it.";

  return new DaemonBridgeError(
    {
      code: "openburrow.web.daemon_unreachable",
      message: "could not reach the burrow daemon",
      hint,
      context: { method, endpoint, errno: code ?? null, detail },
    },
    503,
  );
}

// ---------------------------------------------------------------------------
// Route-handler helper
// ---------------------------------------------------------------------------

/**
 * Wrap a route handler so a bridge failure becomes the standard error envelope.
 *
 * Every route handler would otherwise need the same try/catch, and the one that
 * forgets is the one that returns a 500 with a stack trace in the body.
 */
export function bridgeHandler<T>(
  fn: () => Promise<T>,
): Promise<{ ok: true; value: T } | { ok: false; error: DaemonBridgeError }> {
  return fn().then(
    (value) => ({ ok: true as const, value }),
    (error: unknown) => ({
      ok: false as const,
      error:
        error instanceof DaemonBridgeError
          ? error
          : new DaemonBridgeError(
              {
                code: "openburrow.web.bridge_failed",
                message: error instanceof Error ? error.message : "the bridge failed",
                context: {},
              },
              500,
            ),
    }),
  );
}

/** True when the daemon's endpoint is configured at all — used by the health panel. */
export function describeEndpoint(): { endpoint: string; transport: string; repo_root: string } {
  return {
    endpoint: resolveDaemonEndpoint(),
    transport: isWindows ? "named-pipe" : "unix-socket",
    repo_root: resolveRepoRoot(),
  };
}
