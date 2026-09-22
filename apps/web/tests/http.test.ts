import { describe, expect, test } from "vitest";

import { DaemonBridgeError } from "@/lib/daemon-bridge";
import { boolParam, fail, failFrom, handle, intParam, ok, readJsonBody, requiredParam } from "@/lib/http";

/**
 * The response envelope, which is the frontend's whole error contract.
 *
 * `lib/http.ts` claims that every handler in `src/app/api/` answers in one of
 * exactly two shapes, and that routing them through `ok`/`fail` is what stops a
 * handler inventing a third. That claim is only true if the two functions are
 * actually used and actually agree, so these tests pin the shapes and the
 * parameter helpers' refusal behaviour — a helper that silently accepts a bad
 * `limit` is how an unbounded `LIMIT` reaches SQLite.
 */

async function body(response: Response): Promise<Record<string, unknown>> {
  return (await response.json()) as Record<string, unknown>;
}

describe("ok", () => {
  test("wraps the payload in a data envelope", async () => {
    const response = ok({ lanes: [] });
    expect(response.status).toBe(200);
    expect(await body(response)).toEqual({ data: { lanes: [] } });
  });

  test("forbids caching, because a stale lane list looks live", async () => {
    expect(ok({}).headers.get("Cache-Control")).toBe("no-store, max-age=0");
  });

  test("lets a caller override the status without losing the header", async () => {
    const response = ok({ id: "x" }, { status: 201 });
    expect(response.status).toBe(201);
    expect(response.headers.get("Cache-Control")).toBe("no-store, max-age=0");
  });

  test("a caller-supplied header wins over the default", async () => {
    const response = ok({}, { headers: { "X-Probe": "1" } });
    expect(response.headers.get("X-Probe")).toBe("1");
    expect(response.headers.get("Cache-Control")).toBe("no-store, max-age=0");
  });
});

describe("fail", () => {
  test("wraps the payload in an error envelope", async () => {
    const response = fail({ code: "x.y", message: "nope", context: {} }, 409);
    expect(response.status).toBe(409);
    expect(await body(response)).toEqual({ error: { code: "x.y", message: "nope", context: {} } });
  });

  test("forbids caching too", () => {
    expect(fail({ code: "x", message: "y" }, 400).headers.get("Cache-Control")).toBe("no-store");
  });
});

describe("failFrom", () => {
  test("a bridge error keeps its own code, hint and status", async () => {
    const error = new DaemonBridgeError(
      { code: "openburrow.daemon.unreachable", message: "no socket", hint: "start the daemon" },
      503,
    );
    const response = failFrom(error);
    expect(response.status).toBe(503);
    expect(await body(response)).toEqual({
      error: {
        code: "openburrow.daemon.unreachable",
        message: "no socket",
        hint: "start the daemon",
        context: {},
      },
    });
  });

  test("a bridge error with no hint does not emit an empty one", async () => {
    // `hint: undefined` survives JSON.stringify as an absent key, but
    // `hint: ""` would render as an empty hint line in the UI.
    const response = failFrom(new DaemonBridgeError({ code: "a.b", message: "m" }, 502));
    expect(await body(response)).toEqual({ error: { code: "a.b", message: "m", context: {} } });
  });

  test("an ordinary Error becomes a 500 naming itself", async () => {
    const response = failFrom(new Error("disk on fire"));
    expect(response.status).toBe(500);
    expect(await body(response)).toEqual({
      error: { code: "openburrow.web.internal", message: "disk on fire", context: {} },
    });
  });

  test("a thrown non-Error still produces the envelope", async () => {
    // `throw "oops"` is legal and happens; a handler that crashed rendering its
    // own crash tells the caller nothing.
    const response = failFrom("oops");
    expect(response.status).toBe(500);
    expect((await body(response)).error).toEqual({
      code: "openburrow.web.internal",
      message: "the route handler failed",
      context: {},
    });
  });
});

describe("handle", () => {
  test("wraps a resolved value in the success envelope", async () => {
    const response = await handle(async () => ({ lanes: 3 }));
    expect(response.status).toBe(200);
    expect(await body(response)).toEqual({ data: { lanes: 3 } });
  });

  test("renders a thrown bridge error into the failure envelope", async () => {
    const response = await handle(async () => {
      throw new DaemonBridgeError({ code: "openburrow.daemon.timeout", message: "slow" }, 504);
    });
    expect(response.status).toBe(504);
    expect(await body(response)).toEqual({
      error: { code: "openburrow.daemon.timeout", message: "slow", context: {} },
    });
  });

  test("never lets a rejection escape as an unhandled 500", async () => {
    const response = await handle(async () => {
      throw new Error("boom");
    });
    expect(response.status).toBe(500);
  });
});

describe("readJsonBody", () => {
  const post = (payload: string, contentType = "application/json") =>
    new Request("http://localhost/api/x", {
      method: "POST",
      headers: { "Content-Type": contentType },
      body: payload,
    });

  test("parses an object", async () => {
    const result = await readJsonBody(post('{"prompt":"go"}'));
    expect(result).toEqual({ ok: true, value: { prompt: "go" } });
  });

  test("an empty body is an empty object, not an error", async () => {
    // A `POST` with no body is a caller saying "use the defaults"; refusing it
    // would make every optional-field endpoint require `{}`.
    expect(await readJsonBody(post(""))).toEqual({ ok: true, value: {} });
    expect(await readJsonBody(post("   \n  "))).toEqual({ ok: true, value: {} });
  });

  test("malformed JSON is a 400 that says what to check", async () => {
    const result = await readJsonBody(post('{"prompt":'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.response.status).toBe(400);
    const payload = (await body(result.response)).error as Record<string, unknown>;
    expect(payload.code).toBe("openburrow.web.bad_json");
    expect(payload.hint).toContain("Content-Type");
  });

  test("a non-object body is refused rather than coerced", async () => {
    for (const [payload, received] of [
      ["[1,2,3]", "array"],
      ["null", "object"],
      ['"a string"', "string"],
      ["42", "number"],
    ] as Array<[string, string]>) {
      const result = await readJsonBody(post(payload));
      expect(result.ok, `${payload} should be refused`).toBe(false);
      if (result.ok) throw new Error("unreachable");
      const error = (await body(result.response)).error as Record<string, unknown>;
      expect(error.code).toBe("openburrow.web.bad_body");
      expect((error.context as Record<string, unknown>).received).toBe(received);
    }
  });
});

describe("requiredParam", () => {
  test("returns a present value, trimmed", () => {
    const result = requiredParam(new URLSearchParams("id=sess_1"), "id");
    expect(result).toEqual({ ok: true, value: "sess_1" });
  });

  test("treats whitespace as absent", () => {
    const result = requiredParam(new URLSearchParams("id=%20%20"), "id");
    expect(result.ok).toBe(false);
  });

  test("names the parameter and lists what did arrive", async () => {
    const result = requiredParam(new URLSearchParams("limit=5"), "id");
    if (result.ok) throw new Error("unreachable");
    expect(result.response.status).toBe(400);
    const error = (await body(result.response)).error as Record<string, unknown>;
    expect(error.code).toBe("openburrow.web.missing_parameter");
    expect(error.message).toContain("id");
    // Listing what arrived is what turns "missing id" into "you sent limit".
    expect((error.context as Record<string, unknown>).received).toEqual(["limit"]);
  });
});

describe("intParam", () => {
  const parse = (query: string, name = "limit", fallback = 50, bounds = {}) =>
    intParam(new URLSearchParams(query), name, fallback, bounds);

  test("absent and empty both mean the fallback", () => {
    expect(parse("")).toEqual({ ok: true, value: 50 });
    expect(parse("limit=")).toEqual({ ok: true, value: 50 });
  });

  test("parses a plain integer", () => {
    expect(parse("limit=7")).toEqual({ ok: true, value: 7 });
  });

  test("refuses a non-integer rather than truncating it", () => {
    // `1.5` is the one that matters: truncating it to 1 would answer a request
    // for one-and-a-half rows with one, and report success.
    for (const raw of ["abc", "1.5", "1,5", "NaN", "Infinity", "1_000", ""]) {
      const result = parse(`limit=${raw}`);
      if (raw === "") continue; // covered above: empty is the fallback
      expect(result.ok, `${raw} should be refused`).toBe(false);
    }
  });

  test("accepts any integer literal Number() knows, and bounds it anyway", () => {
    // Measured, not assumed. The parser is `Number()`, so `1e3` is 1000 and
    // `0x10` is 16 — surprising spellings for a query parameter, and every one
    // of them is a real integer. Tightening the grammar would be a change of
    // behaviour with no security value: what protects the SQL `LIMIT` this feeds
    // is the range check, which applies to all of them equally.
    expect(parse("limit=1e3")).toEqual({ ok: true, value: 1000 });
    expect(parse("limit=0x10")).toEqual({ ok: true, value: 16 });
    expect(parse("limit=%205%20")).toEqual({ ok: true, value: 5 });
    expect(parse("limit=+5")).toEqual({ ok: true, value: 5 });
    // …and the bounds still catch the large ones, whichever way they are spelled.
    expect(parse("limit=1e9", "limit", 50, { min: 0, max: 1000 }).ok).toBe(false);
    expect(parse("limit=0xFFFF", "limit", 50, { min: 0, max: 1000 }).ok).toBe(false);
  });

  test("enforces the bounds, because the value reaches a SQL LIMIT", () => {
    const low = parse("limit=-1", "limit", 50, { min: 0, max: 1000 });
    const high = parse("limit=5000", "limit", 50, { min: 0, max: 1000 });
    expect(low.ok).toBe(false);
    expect(high.ok).toBe(false);
  });

  test("the boundary values are inside the range", () => {
    expect(parse("limit=0", "limit", 50, { min: 0, max: 1000 })).toEqual({ ok: true, value: 0 });
    expect(parse("limit=1000", "limit", 50, { min: 0, max: 1000 })).toEqual({ ok: true, value: 1000 });
  });

  test("the out-of-range refusal says what the range was", async () => {
    const result = parse("limit=9999", "limit", 50, { min: 1, max: 10 });
    if (result.ok) throw new Error("unreachable");
    const error = (await body(result.response)).error as Record<string, unknown>;
    expect(error.code).toBe("openburrow.web.parameter_out_of_range");
    expect(error.context).toEqual({ parameter: "limit", received: 9999, min: 1, max: 10 });
  });
});

describe("boolParam", () => {
  test("accepts the spellings people type", () => {
    for (const raw of ["true", "1", "yes", "TRUE", " Yes "]) {
      expect(boolParam(new URLSearchParams(`x=${encodeURIComponent(raw)}`), "x", false)).toBe(true);
    }
    for (const raw of ["false", "0", "no", "FALSE", " No "]) {
      expect(boolParam(new URLSearchParams(`x=${encodeURIComponent(raw)}`), "x", true)).toBe(false);
    }
  });

  test("falls back on absence and on nonsense", () => {
    expect(boolParam(new URLSearchParams(""), "x", true)).toBe(true);
    expect(boolParam(new URLSearchParams("x="), "x", true)).toBe(true);
    expect(boolParam(new URLSearchParams("x=maybe"), "x", false)).toBe(false);
  });
});
