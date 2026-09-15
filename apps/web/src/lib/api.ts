import type {
  A2ATask,
  BusEvent,
  Lane,
  ReelBundle,
  ReelManifest,
  RelayRoom,
  Session,
} from "@/types/openburrow";

/**
 * The daemon and relay REST client.
 *
 * Two things here are worth reading before changing anything.
 *
 * **The error envelope is the contract.** Every OpenBurrow error carries a stable
 * machine `code`, a human `hint`, and a `context` object — that is true in the
 * daemon, in the relay, and here. `ApiError` preserves all three rather than
 * collapsing them into a message string, because the UI wants to render the hint
 * under the message and branch on the code, and re-deriving either from prose is
 * how a UI ends up string-matching error text.
 *
 * **The base URL is resolved at call time, not import time.** A static export
 * talks to the daemon directly and a server build talks to its own route
 * handlers; resolving once at module scope would bake in whichever was set when
 * the bundle was built.
 */

export interface ApiErrorPayload {
  code: string;
  message: string;
  hint?: string;
  context?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly hint?: string;
  readonly context: Record<string, unknown>;
  readonly status: number;
  /** True when retrying the same request could plausibly succeed. */
  readonly retryable: boolean;

  constructor(payload: ApiErrorPayload, status: number) {
    super(payload.message);
    this.name = "ApiError";
    this.code = payload.code;
    this.hint = payload.hint;
    this.context = payload.context ?? {};
    this.status = status;
    // 5xx and 429 are worth retrying; a 401 or a 403 will say the same thing
    // forever, and a client that retries those is a client that hammers a door
    // that will not open.
    this.retryable = status >= 500 || status === 429 || status === 0;
  }

  /** `[code] message` — the form used in log lines and the error panel header. */
  get label(): string {
    return `[${this.code}] ${this.message}`;
  }
}

/** Thrown when the daemon is unreachable, as opposed to having answered badly. */
export const UNREACHABLE_CODE = "openburrow.web.daemon_unreachable";

function apiBase(): string {
  const configured = process.env.NEXT_PUBLIC_OPENBURROW_API;
  if (configured) return configured.replace(/\/$/, "");
  // Server build: the route handlers under src/app/api proxy the daemon, which
  // keeps the control token out of the browser.
  return "";
}

export function isDirectMode(): boolean {
  return Boolean(process.env.NEXT_PUBLIC_OPENBURROW_API);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Milliseconds. The daemon polls fast; a stalled request should not wedge the UI. */
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * One request, with the error envelope unwrapped.
 *
 * The timeout is implemented with `AbortSignal.timeout` combined with the
 * caller's signal rather than a `setTimeout` that calls `abort()`, so a request
 * that finishes early does not leave a timer holding the event loop open — which
 * in a polling dashboard is a slow leak of pending timers.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const url = `${apiBase()}${path}`;

  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      signal: combined,
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      // The daemon's state is the whole point of the UI; a cached lane list is
      // worse than no lane list.
      cache: "no-store",
    });
  } catch (cause) {
    const aborted = cause instanceof Error && cause.name === "TimeoutError";
    throw new ApiError(
      {
        code: UNREACHABLE_CODE,
        message: aborted
          ? `the daemon did not answer within ${timeoutMs}ms`
          : "could not reach the daemon",
        hint: "Check that it is running (`burrow daemon status`) and that OPENBURROW_DAEMON_URL points at it.",
        context: { url, method, aborted },
      },
      0,
    );
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // A non-JSON body from a daemon is usually a proxy's error page. Passing it
      // through as the message is far more useful than "unexpected token".
      parsed = undefined;
    }
  }

  if (!response.ok) {
    const envelope = extractError(parsed, text, response.status);
    throw new ApiError(envelope, response.status);
  }

  // The daemon wraps successful payloads in `{data: ...}` on some endpoints and
  // returns them bare on others. Unwrapping here keeps that inconsistency in one
  // place instead of in every caller.
  if (parsed && typeof parsed === "object" && "data" in parsed) {
    return (parsed as { data: T }).data;
  }
  return parsed as T;
}

function extractError(parsed: unknown, text: string, status: number): ApiErrorPayload {
  if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    const nested = record.error;
    if (nested && typeof nested === "object") {
      const error = nested as Record<string, unknown>;
      return {
        code: typeof error.code === "string" ? error.code : `http.${status}`,
        message: typeof error.message === "string" ? error.message : httpStatusMessage(status),
        hint: typeof error.hint === "string" ? error.hint : undefined,
        context:
          error.context && typeof error.context === "object"
            ? (error.context as Record<string, unknown>)
            : undefined,
      };
    }
  }
  return {
    code: `http.${status}`,
    message: text.slice(0, 400) || httpStatusMessage(status),
  };
}

function httpStatusMessage(status: number): string {
  return `the daemon returned HTTP ${status}`;
}

// ---------------------------------------------------------------------------
// Daemon endpoints
// ---------------------------------------------------------------------------

export interface DaemonStatus {
  running: boolean;
  version: string;
  pid: number;
  uptime_s: number;
  endpoint: string;
  repo_root: string;
  requests_served: number;
}

export interface DaemonHealth {
  database: { ok: boolean; error?: string; url?: string };
  adapters: Record<string, boolean>;
  bus: { subscribers: number };
  governance: {
    enabled: boolean;
    max_delegation_depth: number;
    authority_inheritance: string;
  };
}

export const daemon = {
  status: (signal?: AbortSignal) => request<DaemonStatus>("/api/daemon/status", { signal }),
  health: (signal?: AbortSignal) => request<DaemonHealth>("/api/daemon/health", { signal }),
};

export const sessions = {
  list: (openOnly = true, signal?: AbortSignal) =>
    request<Session[]>(`/api/sessions?open_only=${openOnly ? "true" : "false"}`, { signal }),

  show: (sessionId: string, signal?: AbortSignal) =>
    request<{ session: Session; running_lanes: Lane[] }>(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
      { signal },
    ),
};

export const lanes = {
  list: (sessionId: string, signal?: AbortSignal) =>
    request<Lane[]>(`/api/sessions/${encodeURIComponent(sessionId)}/lanes`, { signal }),

  start: (sessionId: string, body: { name: string; harness: string; role: string; owner?: string }) =>
    request<Lane>(`/api/sessions/${encodeURIComponent(sessionId)}/lanes`, { method: "POST", body }),

  stop: (sessionId: string, laneId: string, reason = "") =>
    request<{ stopped: boolean; lane_id: string }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/lanes/${encodeURIComponent(laneId)}/stop`,
      { method: "POST", body: { reason } },
    ),

  prompt: (sessionId: string, laneId: string, prompt: string) =>
    request<{ delivered: boolean }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/lanes/${encodeURIComponent(laneId)}/prompt`,
      { method: "POST", body: { prompt } },
    ),
};

export const bus = {
  /**
   * Events after `sinceSeq`.
   *
   * `seq` is the ordering key, not a timestamp and not a ULID: the daemon assigns
   * it monotonically, so "give me everything after 412" is exact. Polling with it
   * is also self-healing — a dropped tick is recovered by the next one, which is
   * why the board polls rather than subscribing.
   */
  tail: (sessionId: string, sinceSeq = 0, limit = 200, signal?: AbortSignal) =>
    request<BusEvent[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/bus?since_seq=${sinceSeq}&limit=${limit}`,
      { signal },
    ),
};

export const tasks = {
  list: (sessionId: string, blockingOnly = false, signal?: AbortSignal) =>
    request<A2ATask[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/tasks?blocking_only=${
        blockingOnly ? "true" : "false"
      }`,
      { signal },
    ),
};

export interface AdapterDescription {
  name: string;
  available: boolean;
  version?: string | null;
  executable?: string | null;
  supports: string[];
}

export const adapters = {
  list: (signal?: AbortSignal) => request<AdapterDescription[]>("/api/adapters", { signal }),
};

// ---------------------------------------------------------------------------
// Relay endpoints
// ---------------------------------------------------------------------------

export const relay = {
  redeem: (invite: string, subject: string, displayName = "") =>
    request<{
      token: string;
      room: RelayRoom;
      member: { id: string; subject: string; display_name: string; role: string };
      expires_in_s: number;
    }>("/api/relay/auth/token", {
      method: "POST",
      body: { invite, subject, display_name: displayName },
    }),

  rooms: (signal?: AbortSignal) =>
    request<{ rooms: RelayRoom[] }>("/api/relay/rooms", { signal }),

  readyz: (signal?: AbortSignal) =>
    request<{
      ok: boolean;
      uptime_s: number;
      database: { ok: boolean; error?: string };
      connections: Record<string, number>;
      rooms: Record<string, number>;
    }>("/api/relay/readyz", { signal }),
};

// ---------------------------------------------------------------------------
// Reels
// ---------------------------------------------------------------------------

/**
 * The reel payload, as assembled by `/api/reels/{id}`.
 *
 * Note what is *not* here: the casts. They are fetched one lane at a time by
 * `reelFile()`, because a ten-lane session's casts are the largest thing this
 * app ever moves and the viewer only ever displays one at a time.
 */
export interface ReelPayload {
  id: string;
  /** Null when the exporter wrote a flat `<id>.json` instead of a directory. */
  directory: string | null;
  bundle: ReelBundle;
  manifest: ReelManifest | null;
  files: Array<{ name: string; bytes: number }>;
  /** True when the exporter's own self-contained `index.html` is present. */
  has_static_viewer: boolean;
  readme: string | null;
}

export const reels = {
  /**
   * The bundle for a session.
   *
   * Returns null on 404 rather than throwing, because "this session has no reel
   * yet" is a normal state the viewer renders an empty state for — not an error.
   * Every other failure still throws, so a broken export is not silently
   * mistaken for a missing one.
   */
  bundle: async (sessionId: string, signal?: AbortSignal): Promise<ReelPayload | null> => {
    try {
      return await request<ReelPayload>(`/api/reels/${encodeURIComponent(sessionId)}`, { signal });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },

  /** One file from a reel. Defaults to `reel.json`. Used lazily for the casts. */
  file: (sessionId: string, file = "reel.json", signal?: AbortSignal) =>
    request<string>(
      `/api/reels/${encodeURIComponent(sessionId)}/file?file=${encodeURIComponent(file)}`,
      { signal, timeoutMs: 30_000 },
    ),

  /** A direct URL, for `<a download>` and the iframe that hosts `index.html`. */
  fileUrl: (sessionId: string, file = "reel.json") =>
    `${apiBase()}/api/reels/${encodeURIComponent(sessionId)}/file?file=${encodeURIComponent(file)}`,
};
