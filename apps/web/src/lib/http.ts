import { NextResponse } from "next/server";

import { DaemonBridgeError, type BridgeErrorPayload } from "@/lib/daemon-bridge";

/**
 * Response helpers for the route handlers.
 *
 * Every handler in `src/app/api/` answers in one of exactly two shapes:
 *
 *   success → `{ "data": ... }`
 *   failure → `{ "error": { code, message, hint?, context } }`
 *
 * That is the same envelope the daemon and the relay use, and `lib/api.ts`
 * already unwraps both. The point of routing everything through these two
 * functions is that no handler can invent a third shape — which is how a client
 * ends up with a special case for "the one endpoint that returns a bare array".
 */

export function ok<T>(data: T, init?: ResponseInit): NextResponse {
  return NextResponse.json(
    { data },
    {
      ...init,
      headers: {
        // The daemon's state is the whole product. A cached lane list is worse
        // than no lane list, because it looks live.
        "Cache-Control": "no-store, max-age=0",
        ...(init?.headers ?? {}),
      },
    },
  );
}

export function fail(payload: BridgeErrorPayload, status: number): NextResponse {
  return NextResponse.json({ error: payload }, { status, headers: { "Cache-Control": "no-store" } });
}

/** Render a bridge error, or a generic 500 for anything that is not one. */
export function failFrom(error: unknown): NextResponse {
  if (error instanceof DaemonBridgeError) {
    return fail(error.toResponseBody().error, error.status);
  }
  return fail(
    {
      code: "openburrow.web.internal",
      message: error instanceof Error ? error.message : "the route handler failed",
      context: {},
    },
    500,
  );
}

/**
 * Run a handler, rendering any thrown bridge error into the envelope.
 *
 * Deliberately takes a thunk rather than being middleware: the handlers that
 * call it are three lines long, and a middleware would hide the fact that the
 * failure path is a real path with a real status code.
 */
export async function handle<T>(fn: () => Promise<T>): Promise<NextResponse> {
  try {
    return ok(await fn());
  } catch (error) {
    return failFrom(error);
  }
}

/**
 * Read a JSON body, treating a malformed one as a 400 in *our* envelope.
 *
 * A bare `await request.json()` throws a `SyntaxError` that surfaces as a 500
 * with a stack trace, which tells the caller nothing about what they got wrong.
 */
export async function readJsonBody(
  request: Request,
): Promise<{ ok: true; value: Record<string, unknown> } | { ok: false; response: NextResponse }> {
  const text = await request.text();
  if (!text.trim()) return { ok: true, value: {} };

  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {
        ok: false,
        response: fail(
          {
            code: "openburrow.web.bad_body",
            message: "the request body must be a JSON object",
            context: { received: Array.isArray(parsed) ? "array" : typeof parsed },
          },
          400,
        ),
      };
    }
    return { ok: true, value: parsed as Record<string, unknown> };
  } catch (cause) {
    return {
      ok: false,
      response: fail(
        {
          code: "openburrow.web.bad_json",
          message: "the request body is not valid JSON",
          hint: "Check the Content-Type header and the quoting.",
          context: { detail: cause instanceof Error ? cause.message : String(cause) },
        },
        400,
      ),
    };
  }
}

/** A required query parameter, or a 400 explaining which one is missing. */
export function requiredParam(
  params: URLSearchParams,
  name: string,
): { ok: true; value: string } | { ok: false; response: NextResponse } {
  const value = params.get(name)?.trim();
  if (!value) {
    return {
      ok: false,
      response: fail(
        {
          code: "openburrow.web.missing_parameter",
          message: `the ${name} parameter is required`,
          context: { parameter: name, received: [...params.keys()] },
        },
        400,
      ),
    };
  }
  return { ok: true, value };
}

/**
 * A bounded integer query parameter.
 *
 * Bounded on purpose: `limit` goes straight to a SQL `LIMIT`, and an unbounded
 * one is a request that asks the daemon to serialise an entire session's bus
 * history into one HTTP response.
 */
export function intParam(
  params: URLSearchParams,
  name: string,
  fallback: number,
  { min = 0, max = 1000 }: { min?: number; max?: number } = {},
): { ok: true; value: number } | { ok: false; response: NextResponse } {
  const raw = params.get(name);
  if (raw === null || raw.trim() === "") return { ok: true, value: fallback };

  const parsed = Number(raw);
  if (!Number.isInteger(parsed)) {
    return {
      ok: false,
      response: fail(
        {
          code: "openburrow.web.bad_parameter",
          message: `${name} must be an integer`,
          context: { parameter: name, received: raw },
        },
        400,
      ),
    };
  }
  if (parsed < min || parsed > max) {
    return {
      ok: false,
      response: fail(
        {
          code: "openburrow.web.parameter_out_of_range",
          message: `${name} must be between ${min} and ${max}`,
          context: { parameter: name, received: parsed, min, max },
        },
        400,
      ),
    };
  }
  return { ok: true, value: parsed };
}

/** A boolean query parameter that accepts the two spellings people actually type. */
export function boolParam(params: URLSearchParams, name: string, fallback: boolean): boolean {
  const raw = params.get(name)?.trim().toLowerCase();
  if (raw === undefined || raw === "") return fallback;
  if (raw === "true" || raw === "1" || raw === "yes") return true;
  if (raw === "false" || raw === "0" || raw === "no") return false;
  return fallback;
}
