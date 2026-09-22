import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "vitest";

import { GET as getReel } from "@/app/api/reels/[id]/route";
import { GET as listReels } from "@/app/api/reels/route";

/**
 * The reel routes, driven against a real directory tree.
 *
 * These are the endpoints the viewer calls first, and they are the only routes in
 * the app that do their work without a daemon — a reel is a directory of files the
 * daemon already finished writing. So they can be tested end to end here, which is
 * worth doing because the failure modes are all about *which* thing went wrong:
 * no reel, an unreadable reel, and a half-written export are three different
 * answers and collapsing them costs someone a re-export they did not need.
 */

const manifest = {
  session_id: "sess_1",
  session_name: "nightly",
  exported_at: "2026-01-02T00:00:00Z",
  lane_count: 2,
  event_count: 40,
  governance_event_count: 3,
  duration_seconds: 120,
};

const bundle = {
  session: { id: "sess_1", name: "nightly", duration_s: 120, started_at: "2026-01-01T00:00:00Z" },
  lanes: [{ id: "l1", events: [[0, "a"]] }],
  timeline: [{ id: "e1", t: 0, type: "lane.started", lane_id: "l1" }],
  coverage: [],
};

let root = "";
let reelsDir = "";

beforeAll(async () => {
  root = await mkdtemp(path.join(tmpdir(), "ob-api-reels-"));
  reelsDir = path.join(root, "reels");
  await mkdir(reelsDir, { recursive: true });

  // A complete export.
  const full = path.join(reelsDir, "sess_full");
  await mkdir(full);
  await writeFile(path.join(full, "reel.json"), JSON.stringify(bundle));
  await writeFile(path.join(full, "manifest.json"), JSON.stringify(manifest));
  await writeFile(path.join(full, "index.html"), "<html>static viewer</html>");
  await writeFile(path.join(full, "README.txt"), "how to read this reel");

  // An export that never finished: a bundle, no manifest.
  const partial = path.join(reelsDir, "sess_partial");
  await mkdir(partial);
  await writeFile(path.join(partial, "reel.json"), JSON.stringify(bundle));

  // A directory the viewer could never fetch, because the name is not an id.
  const junk = path.join(reelsDir, "not an id!");
  await mkdir(junk);
  await writeFile(path.join(junk, "reel.json"), JSON.stringify(bundle));

  // A directory with no bundle at all.
  await mkdir(path.join(reelsDir, "sess_empty"));

  // The flat-file layout.
  await writeFile(path.join(reelsDir, "sess_flat.json"), JSON.stringify(bundle));

  process.env.OPENBURROW_HOME = root;
});

afterAll(async () => {
  delete process.env.OPENBURROW_HOME;
  if (root) await rm(root, { recursive: true, force: true });
});

async function data(response: Response): Promise<Record<string, unknown>> {
  const payload = (await response.json()) as { data: Record<string, unknown> };
  return payload.data;
}

async function errorOf(response: Response): Promise<{ code: string; context: Record<string, unknown> }> {
  const payload = (await response.json()) as { error: { code: string; context: Record<string, unknown> } };
  return payload.error;
}

describe("GET /api/reels", () => {
  test("lists only the entries that are shaped like reels", async () => {
    const response = await listReels();
    expect(response.status).toBe(200);
    const body = await data(response);
    const ids = (body.reels as Array<{ id: string }>).map((reel) => reel.id).sort();
    // `not an id!` is a real directory holding a real bundle, and it is absent
    // because the viewer could never fetch it — listing it would be a dead link.
    // `sess_flat.json` is absent for a different reason: it is a *file*, and the
    // list enumerates directories. The flat-file layout is served by the detail
    // route, which looks for both shapes.
    expect(ids).toEqual(["sess_empty", "sess_full", "sess_partial"]);
    expect(body.directory).toBe(reelsDir);
  });

  test("reads the manifest for the fields the list needs", async () => {
    const body = await data(await listReels());
    const full = (body.reels as Array<Record<string, unknown>>).find((reel) => reel.id === "sess_full");
    expect(full).toMatchObject({
      session_id: "sess_1",
      session_name: "nightly",
      duration_s: 120,
      lane_count: 2,
      event_count: 40,
      governance_event_count: 3,
      exported_at: "2026-01-02T00:00:00Z",
      has_static_viewer: true,
      files: 4,
    });
    expect(full?.bytes).toBeGreaterThan(0);
  });

  test("an interrupted export is listed with nulls, not hidden", async () => {
    // Hiding it would make a failed export look like a reel that was never
    // attempted, which is a different thing to go and fix.
    const body = await data(await listReels());
    const partial = (body.reels as Array<Record<string, unknown>>).find((reel) => reel.id === "sess_partial");
    expect(partial).toMatchObject({
      session_id: "sess_1", // fell back to the bundle
      session_name: "nightly",
      duration_s: 120,
      lane_count: 1,
      event_count: 1,
      governance_event_count: null, // manifest-only, and there is no manifest
      exported_at: null,
      has_static_viewer: false,
      files: 1,
    });
  });

  test("a directory with neither file is still listed, with zeros", async () => {
    const body = await data(await listReels());
    const empty = (body.reels as Array<Record<string, unknown>>).find((reel) => reel.id === "sess_empty");
    expect(empty).toMatchObject({ session_id: null, lane_count: null, files: 0, bytes: 0 });
  });

  test("the flat-file layout is not a directory, so it is not listed", async () => {
    // `sess_flat.json` is a file, and the list enumerates directories only.
    const body = await data(await listReels());
    const ids = (body.reels as Array<{ id: string }>).map((reel) => reel.id);
    expect(ids).not.toContain("sess_flat.json");
  });

  test("sorts deterministically, with the undated entries first", async () => {
    // Measured, and worth stating plainly because it is not what "newest first"
    // sounds like. The comparator is `(b.exported_at ?? b.id).localeCompare(…)`,
    // so an undated entry is compared as an *id* against another entry's
    // *timestamp* — and `"2026-01-02T00:00:00Z"` sorts below `"sess_partial"`
    // because `"2"` is less than `"s"`. The dated entry therefore comes last.
    //
    // The order is total and stable, which is what the list needs, and putting an
    // interrupted export at the top is arguably the right place for it. Pinned
    // here so that a change to the comparator is a deliberate act rather than a
    // silent reshuffle of a list people click on.
    const body = await data(await listReels());
    const ids = (body.reels as Array<{ id: string }>).map((reel) => reel.id);
    expect(ids).toEqual(["sess_partial", "sess_empty", "sess_full"]);
  });

  test("a missing reels directory is an empty list, not an error", async () => {
    process.env.OPENBURROW_HOME = path.join(root, "nowhere");
    try {
      const response = await listReels();
      expect(response.status).toBe(200);
      expect(await data(response)).toEqual({
        reels: [],
        directory: path.join(root, "nowhere", "reels"),
      });
    } finally {
      process.env.OPENBURROW_HOME = root;
    }
  });
});

describe("GET /api/reels/[id]", () => {
  const call = (id: string) => getReel(new Request("http://localhost/api/reels/x"), { params: Promise.resolve({ id }) });

  test("returns the bundle, manifest, inventory and readme together", async () => {
    const response = await call("sess_full");
    expect(response.status).toBe(200);
    const body = await data(response);
    expect(body.id).toBe("sess_full");
    expect(body.directory).toBe(path.join(reelsDir, "sess_full"));
    expect((body.bundle as { session: { id: string } }).session.id).toBe("sess_1");
    expect((body.manifest as { lane_count: number }).lane_count).toBe(2);
    expect(body.has_static_viewer).toBe(true);
    expect(body.readme).toBe("how to read this reel");
    expect((body.files as unknown[]).length).toBe(4);
  });

  test("a reel id that is not an id is refused before the filesystem is touched", async () => {
    for (const id of ["..", "../outside", "a/b", "not an id!", "-x"]) {
      const response = await call(id);
      expect(response.status, `${id} should be a 400`).toBe(400);
      expect((await errorOf(response)).code).toBe("openburrow.web.bad_reel_id");
    }
  });

  test("a reel that does not exist is a 404 naming where it looked", async () => {
    const response = await call("sess_missing");
    expect(response.status).toBe(404);
    const error = await errorOf(response);
    expect(error.code).toBe("openburrow.web.reel_not_found");
    expect(error.context.reels_dir).toBe(reelsDir);
  });

  test("a directory with no readable bundle is 502, not 404", async () => {
    // The distinction is the point: 404 says "there is no reel here" and 502 says
    // "there is one and it is broken". Collapsing them makes an interrupted export
    // look like a reel that was never exported.
    const response = await call("sess_empty");
    expect(response.status).toBe(502);
    const error = await errorOf(response);
    expect(error.code).toBe("openburrow.web.reel_unreadable");
    // The route builds this with a template literal and a forward slash rather
    // than `path.join`, so on Windows the message carries mixed separators. Node
    // and Windows both accept that, and it is what the code says, so the test
    // pins it as-is rather than asserting a tidier string the route never emits.
    expect(error.context.expected).toBe(`${path.join(reelsDir, "sess_empty")}/reel.json`);
  });

  test("the flat-file layout works, with no manifest and no inventory", async () => {
    const response = await call("sess_flat");
    expect(response.status).toBe(200);
    const body = await data(response);
    expect(body.directory).toBeNull();
    expect(body.manifest).toBeNull();
    expect(body.files).toEqual([]);
    expect(body.has_static_viewer).toBe(false);
    expect((body.bundle as { session: { id: string } }).session.id).toBe("sess_1");
  });

  test("a traversal cannot reach a reel outside the directory", async () => {
    // `sess_full` exists and is reachable; `../sess_full` must not be, even though
    // it names the same directory after normalisation.
    const response = await call("../sess_full");
    expect(response.status).toBe(400);
  });
});
