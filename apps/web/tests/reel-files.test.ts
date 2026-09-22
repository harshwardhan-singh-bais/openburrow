import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "vitest";

import {
  listReelFiles,
  locateReel,
  readJsonFile,
  readTextFile,
  reelNotFound,
  safeIdOrNull,
} from "@/lib/reel-files";

/**
 * Reading reels off disk, where the security surface is entirely the path.
 *
 * A reel id arrives from a URL, so `..%2f..%2fetc%2fpasswd` is the attack. The
 * defence is not `path.normalize` — it is refusing any id that is not shaped like
 * an id, plus a second `isInside` check at the point of use. Both are tested here
 * against a file that *really exists* outside the reels directory, because a
 * traversal check proved only against a missing file proves nothing at all.
 */

describe("safeIdOrNull", () => {
  test("accepts the shapes the exporter and the CLI actually produce", () => {
    for (const id of [
      "sess_01HQ8Z4K2M9N7P1Q3R5T7V9X1B",
      "2026-09-15-nightly",
      "reel.v2",
      "a",
      "A1",
      "x".repeat(200),
    ]) {
      expect(safeIdOrNull(id), `${id} should be accepted`).toBe(id);
    }
  });

  test("refuses every traversal spelling", () => {
    for (const id of [
      "..",
      ".",
      "../etc",
      "..\\etc",
      "a/../b",
      "a/b",
      "a\\b",
      "/absolute",
      "C:\\absolute",
      "\\\\server\\share",
      "~",
      "%2e%2e",
      "a b",
      "a\nb",
      "a\u0000b",
      "-leading-dash",
      "_leading-underscore",
      ".leading-dot",
      "trailing-slash/",
    ]) {
      expect(safeIdOrNull(id), `${JSON.stringify(id)} should be refused`).toBeNull();
    }
  });

  test("refuses empty and over-long ids", () => {
    expect(safeIdOrNull("")).toBeNull();
    expect(safeIdOrNull("x".repeat(201))).toBeNull();
  });

  test("the boundary is 200 characters, and it is inclusive", () => {
    expect(safeIdOrNull("x".repeat(200))).not.toBeNull();
    expect(safeIdOrNull("x".repeat(201))).toBeNull();
  });
});

describe("reading reels from a real directory", () => {
  let root = "";
  let reelsDir = "";
  let outside = "";

  beforeAll(async () => {
    root = await mkdtemp(path.join(tmpdir(), "ob-reels-"));
    reelsDir = path.join(root, "reels");
    outside = path.join(root, "outside");
    await mkdir(reelsDir, { recursive: true });
    await mkdir(outside, { recursive: true });

    // The directory layout the exporter writes.
    await mkdir(path.join(reelsDir, "sess_dir"));
    await writeFile(path.join(reelsDir, "sess_dir", "manifest.json"), JSON.stringify({ session_id: "s1" }));
    await writeFile(path.join(reelsDir, "sess_dir", "index.html"), "<html></html>");

    // The flat-file layout some callers use instead.
    await writeFile(path.join(reelsDir, "sess_flat.json"), JSON.stringify({ session: { id: "s2" } }));

    // Deliberately outside the reels directory, and readable. This is what a
    // traversal would reach.
    await writeFile(path.join(outside, "secret.json"), JSON.stringify({ secret: true }));

    process.env.OPENBURROW_HOME = root;
  });

  afterAll(async () => {
    delete process.env.OPENBURROW_HOME;
    if (root) await rm(root, { recursive: true, force: true });
  });

  test("resolveReelsDir follows OPENBURROW_HOME", async () => {
    // If this is wrong every other assertion below is testing the wrong tree.
    const found = await locateReel("sess_dir");
    expect(found.directory).toBe(path.join(reelsDir, "sess_dir"));
  });

  test("finds a directory layout", async () => {
    expect(await locateReel("sess_dir")).toEqual({
      directory: path.join(reelsDir, "sess_dir"),
      bundleFile: null,
    });
  });

  test("finds a flat-file layout", async () => {
    expect(await locateReel("sess_flat")).toEqual({
      directory: null,
      bundleFile: path.join(reelsDir, "sess_flat.json"),
    });
  });

  test("a name that exists nowhere is simply absent", async () => {
    expect(await locateReel("sess_missing")).toEqual({ directory: null, bundleFile: null });
  });

  test("a traversal is refused even when the target exists", async () => {
    // `../outside` resolves to a real, readable directory. The only thing
    // standing between the caller and it is `isInside`.
    expect(await locateReel("../outside")).toEqual({ directory: null, bundleFile: null });
    expect(await locateReel("..")).toEqual({ directory: null, bundleFile: null });
    expect(await locateReel("../outside/secret.json")).toEqual({ directory: null, bundleFile: null });
  });

  test("an absolute path is refused", async () => {
    expect(await locateReel(outside)).toEqual({ directory: null, bundleFile: null });
  });

  test("readJsonFile parses, and returns null rather than throwing", async () => {
    expect(await readJsonFile(path.join(reelsDir, "sess_flat.json"))).toEqual({ session: { id: "s2" } });
    expect(await readJsonFile(path.join(reelsDir, "nope.json"))).toBeNull();
    expect(await readJsonFile(path.join(reelsDir, "sess_dir"))).toBeNull();
  });

  test("readTextFile returns null for a missing file", async () => {
    expect(await readTextFile(path.join(reelsDir, "sess_dir", "index.html"))).toBe("<html></html>");
    expect(await readTextFile(path.join(reelsDir, "nope.html"))).toBeNull();
  });

  test("listReelFiles sorts by name and reports sizes", async () => {
    // Sizes are measured from the bytes written rather than hardcoded, so this
    // cannot drift when the fixture changes — and `bytes` is what the viewer's
    // "what is in this bundle" panel shows.
    const html = "<html></html>";
    const manifest = JSON.stringify({ session_id: "s1" });
    await writeFile(path.join(reelsDir, "sess_dir", "index.html"), html);
    await writeFile(path.join(reelsDir, "sess_dir", "manifest.json"), manifest);

    const files = await listReelFiles(path.join(reelsDir, "sess_dir"));
    expect(files.map((file) => file.name)).toEqual(["index.html", "manifest.json"]);
    expect(files[0]?.bytes).toBe(Buffer.byteLength(html));
    expect(files[1]?.bytes).toBe(Buffer.byteLength(manifest));
  });

  test("listReelFiles on a missing directory is empty, not an error", async () => {
    expect(await listReelFiles(path.join(reelsDir, "nope"))).toEqual([]);
  });

  test("reelNotFound is the 404 envelope, and names the directory it searched", async () => {
    const response = reelNotFound("sess_missing");
    expect(response.status).toBe(404);
    const payload = (await response.json()) as { error: { code: string; context: Record<string, unknown> } };
    expect(payload.error.code).toBe("openburrow.web.reel_not_found");
    // Telling the caller *where* it looked is what turns "no reel" into a fixable
    // "wrong OPENBURROW_HOME".
    expect(payload.error.context.reels_dir).toBe(reelsDir);
  });

  test("the file the traversal was aiming at is genuinely readable", async () => {
    // The control for the two refusals above: without this, they could be passing
    // because the target does not exist.
    const secret = await readFile(path.join(outside, "secret.json"), "utf8");
    expect(JSON.parse(secret)).toEqual({ secret: true });
  });
});
