import { callDaemon } from "@/lib/daemon-bridge";
import { handle } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * The daemon's self-report: database, adapters, bus, governance.
 *
 * Deliberately separate from `/api/daemon/status`. Status answers "is it there";
 * health answers "is it well", and the two fail independently — a daemon with an
 * unreachable database is very much running, and collapsing the two would make
 * the dashboard either hide a working daemon or claim a broken one is fine.
 *
 * The daemon returns `database.ok: false` with an `error` string rather than
 * throwing, and that is preserved here: the panel renders a red database row
 * *inside* a healthy page, which is the correct picture.
 */
export async function GET(): Promise<Response> {
  return handle(() => callDaemon<Record<string, unknown>>("daemon.health"));
}
