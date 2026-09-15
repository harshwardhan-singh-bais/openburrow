import { NextResponse } from "next/server";

import { fail } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * A transparent proxy to the relay.
 *
 * The relay is the one component in OpenBurrow that already speaks HTTP, so
 * there is nothing to bridge — only something to *route around*. This catch-all
 * exists for three reasons, and none of them is "hide the relay":
 *
 * 1. **The relay's origin stays server-side.** `OPENBURROW_RELAY_URL` is not a
 *    `NEXT_PUBLIC_` variable, so the browser never learns where the relay is.
 *    That matters because the relay is routinely on a private network and the
 *    web app is routinely not.
 * 2. **One origin in the browser.** No CORS preflight, no second cookie jar, no
 *    mixed-content problem when the web app is served over TLS and the relay is
 *    not.
 * 3. **The relay's own error envelope passes through untouched.** It already
 *    uses `{error: {code, message, hint, context}}` — the same envelope as the
 *    daemon — so `ApiError` in the client parses it without a branch.
 *
 * What this proxy deliberately does **not** do:
 *
 * - It does not hold a relay token. The relay's tokens are per-member and
 *   per-room; a server-side "service token" would be a credential that acts as
 *   every member at once, which is precisely the ambient authority the whole
 *   governance layer exists to prevent. The client sends its own
 *   `Authorization` header and this forwards it.
 * - It does not retry. A retried `POST /rooms/{room}/events` would double-append
 *   from the caller's point of view even though the relay dedupes by
 *   `(origin_repo, origin_seq)` — the dedupe is a safety net, not a licence to
 *   retry.
 * - It does not touch WebSocket upgrades. `/stream` and `/doc` are dialled
 *   directly by the browser using `NEXT_PUBLIC_OPENBURROW_RELAY_WS`, because a
 *   Next route handler cannot proxy a long-lived bidirectional socket.
 */

/** Only the methods the relay actually serves. A proxy that forwards anything is an open relay. */
const ALLOWED_METHODS = new Set(["GET", "POST", "DELETE"]);

function relayBase(): string | null {
  // Deliberately NOT `OPENBURROW_RELAY_URL`. That name is the *client* setting
  // the CLI uses to dial a relay, and its value is a WebSocket URL with a path
  // (`ws://host:8787/ws`). This proxy needs an HTTP origin (`http://host:8787`).
  // Sharing the name would mean one of the two is silently wrong, and the
  // failure would surface as "could not reach the relay" with nothing to
  // explain why — so the web app gets its own variable.
  const configured = process.env.OPENBURROW_WEB_RELAY_URL?.trim();
  if (!configured) return null;
  return configured.replace(/\/$/, "");
}

/** Path segments, joined back into a path. Also rejects traversal. */
function joinPath(segments: readonly string[]): string | null {
  for (const segment of segments) {
    // A `..` or an empty segment here would let a caller walk out of the
    // intended prefix. `URLSearchParams`-style encoding does not help, so it is
    // rejected outright rather than normalised away.
    if (!segment || segment === "." || segment === ".." || segment.includes("/")) return null;
  }
  return `/${segments.join("/")}`;
}

async function forward(
  request: Request,
  segments: readonly string[],
): Promise<Response> {
  const base = relayBase();
  if (!base) {
    return fail(
      {
        code: "openburrow.web.relay_not_configured",
        message: "no relay is configured for this deployment",
        hint: "Set OPENBURROW_RELAY_URL to the relay's origin to enable the relay surfaces. It is deliberately server-side only.",
        context: { variable: "OPENBURROW_RELAY_URL" },
      },
      503,
    );
  }

  if (!ALLOWED_METHODS.has(request.method)) {
    return fail(
      {
        code: "openburrow.web.method_not_allowed",
        message: `the relay proxy does not forward ${request.method}`,
        context: { method: request.method, allowed: [...ALLOWED_METHODS] },
      },
      405,
    );
  }

  const path = joinPath(segments);
  if (path === null) {
    return fail(
      {
        code: "openburrow.web.bad_path",
        message: "the relay path contains a segment that is not allowed",
        context: { segments: [...segments] },
      },
      400,
    );
  }

  const incoming = new URL(request.url);
  const target = `${base}${path}${incoming.search}`;

  // Only the headers the relay needs are forwarded. Passing the browser's whole
  // header set through would leak the web app's own `Cookie` and `Host` to a
  // different service, and would let a caller forge `X-Forwarded-For` on a
  // request the relay is going to rate-limit by client key.
  const headers = new Headers({ Accept: "application/json" });
  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("Authorization", authorization);

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let body: string | undefined;
  if (hasBody) {
    body = await request.text();
    if (body) headers.set("Content-Type", "application/json");
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      ...(body ? { body } : {}),
      cache: "no-store",
      // The relay is the component allowed to be slow; it may be across a WAN.
      signal: AbortSignal.timeout(30_000),
    });
  } catch (cause) {
    const timedOut = cause instanceof Error && cause.name === "TimeoutError";
    return fail(
      {
        code: "openburrow.web.relay_unreachable",
        message: timedOut ? "the relay did not answer within 30s" : "could not reach the relay",
        hint: "Check that the relay is running and that OPENBURROW_RELAY_URL points at its origin.",
        context: { target, detail: cause instanceof Error ? cause.message : String(cause) },
      },
      503,
    );
  }

  // Status and body pass through unchanged. The relay's envelope is already the
  // one the client parses, so re-wrapping it here would be a second format to
  // keep in sync for no gain.
  const text = await upstream.text();
  const responseHeaders = new Headers({ "Cache-Control": "no-store" });
  const retryAfter = upstream.headers.get("retry-after");
  if (retryAfter) responseHeaders.set("Retry-After", retryAfter);

  return new NextResponse(text || null, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export async function GET(
  request: Request,
  { params }: { params: Promise<{ path?: string[] }> },
): Promise<Response> {
  const { path } = await params;
  return forward(request, path ?? []);
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ path?: string[] }> },
): Promise<Response> {
  const { path } = await params;
  return forward(request, path ?? []);
}

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ path?: string[] }> },
): Promise<Response> {
  const { path } = await params;
  return forward(request, path ?? []);
}
