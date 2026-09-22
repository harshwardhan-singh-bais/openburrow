import { afterEach, describe, expect, test, vi } from "vitest";

import { relay } from "@/lib/api";

/**
 * The relay client's credential.
 *
 * The relay authenticates with `Authorization: Bearer <room token>` and answers
 * 401 without it. The page stores that token in `localStorage`, and for a while
 * it stored it and never sent it: `request()` had no way to add a header, so the
 * room list was permanently empty and the only symptom was an error the page
 * rendered as "could not list rooms". Nothing in the type system noticed, because
 * a request with no header is still a well-formed request.
 *
 * These tests are about the header and nothing else, so `fetch` is stubbed at the
 * boundary rather than a route handler being driven. What matters is the shape of
 * the request that leaves the browser.
 */

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ rooms: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  return calls;
}

/**
 * The one request that was made, or a failure naming the count.
 *
 * Not `calls[0]`: `noUncheckedIndexedAccess` is on, and the point of asserting
 * the length here is that a test which silently inspects `undefined` would pass
 * while proving nothing about the request.
 */
function only(calls: Call[]): Call {
  expect(calls).toHaveLength(1);
  const [call] = calls;
  if (!call) throw new Error(`expected one request, got ${calls.length}`);
  return call;
}

function headersOf(call: Call): Record<string, string> {
  return (call.init.headers ?? {}) as Record<string, string>;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("relay.rooms", () => {
  test("sends the room token as a bearer credential", async () => {
    const calls = stubFetch();

    await relay.rooms("ob_room_token");
    const call = only(calls);

    expect(call.url).toBe("/api/relay/rooms");
    expect(headersOf(call).Authorization).toBe("Bearer ob_room_token");
  });

  test("sends no Authorization header when the token is empty", async () => {
    const calls = stubFetch();

    await relay.rooms("");
    const call = only(calls);

    // An empty `Bearer ` is not the same request as no header at all. The relay's
    // `extract_bearer` would parse it and reject it as malformed, so the caller
    // would see a 401 that reads like a bad credential instead of a missing one —
    // and a missing credential is the one a user can actually fix.
    expect(headersOf(call).Authorization).toBeUndefined();
  });
});
