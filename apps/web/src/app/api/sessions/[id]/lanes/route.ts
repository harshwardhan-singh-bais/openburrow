import { callDaemon } from "@/lib/daemon-bridge";
import { fail, handle, readJsonBody } from "@/lib/http";

export const dynamic = "force-dynamic";

/** Live lanes in a session. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  return handle(() => callDaemon<unknown[]>("lane.status", { session: id }));
}

/**
 * Start a lane.
 *
 * `harness` and `role` are validated by the daemon, not here, and that is
 * deliberate: the daemon owns the adapter registry and the role vocabulary, so
 * duplicating the list in TypeScript would create a second source of truth that
 * silently drifts the moment an adapter is added. The handler checks only that
 * the fields are present and are strings — enough to produce a useful 400
 * instead of a daemon-side type error, and no more.
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  const body = await readJsonBody(request);
  if (!body.ok) return body.response;

  const name = typeof body.value.name === "string" ? body.value.name.trim() : "";
  const harness = typeof body.value.harness === "string" ? body.value.harness.trim() : "";

  if (!name) {
    return fail(
      {
        code: "openburrow.web.missing_field",
        message: "a lane needs a name",
        hint: "Names appear in the board, the bus feed and the reel, so they are required rather than generated.",
        context: { field: "name" },
      },
      400,
    );
  }
  if (!harness) {
    return fail(
      {
        code: "openburrow.web.missing_field",
        message: "a lane needs a harness",
        hint: "Ask `/api/adapters` for the available ones.",
        context: { field: "harness", available_via: "/api/adapters" },
      },
      400,
    );
  }

  return handle(() =>
    callDaemon<Record<string, unknown>>("lane.start", {
      session: id,
      name,
      harness,
      role: typeof body.value.role === "string" ? body.value.role : "implementer",
      owner: typeof body.value.owner === "string" ? body.value.owner : "",
      claims: Array.isArray(body.value.claims) ? body.value.claims : [],
    }),
  );
}
