/** Integration tests: starts a real cellblock server on :4300 (Docker required) and drives it. */
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Cellblock, CellblockError, Sandbox } from "../src/index.ts";

const PORT = process.env.CELLBLOCK_PORT ?? "4300";
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = fileURLToPath(new URL("../../../", import.meta.url)); // sdk/ts/test -> project root
const PY = join(ROOT, ".venv/bin/python"); // the project venv: no PATH dependency on uv
const INSTANCE = `ts${Date.now().toString(36)}`;
const env = { ...process.env, CELLBLOCK_HOME: mkdtempSync(join(tmpdir(), "cb-ts-")), CELLBLOCK_INSTANCE: INSTANCE, CELLBLOCK_POOL_SIZE: "0", CELLBLOCK_PORT: PORT };

let server: ChildProcess | undefined;
let client: Cellblock;

async function waitHealthy(): Promise<void> {
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(`${BASE}/healthz`)).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("server did not start");
}

before(async () => {
  const key = execFileSync(PY, ["-m", "cellblock.cli", "key", "create", "ts-test"], { cwd: ROOT, env, encoding: "utf8" }).trim().split("\n").pop()!;
  server = spawn(PY, ["-m", "cellblock.cli", "serve", "--port", PORT], { cwd: ROOT, env, stdio: "ignore" });
  client = new Cellblock({ baseUrl: BASE, apiKey: key });
  await waitHealthy();
});

after(() => {
  server?.kill("SIGTERM");
  const ids = execFileSync("docker", ["ps", "-aq", "--filter", `label=cellblock.instance=${INSTANCE}`], { encoding: "utf8" }).split(/\s+/).filter(Boolean);
  if (ids.length) execFileSync("docker", ["rm", "-f", ...ids]);
  rmSync(env.CELLBLOCK_HOME, { recursive: true, force: true });
});

test("runCode returns stdout, exit code, and deletes cleanly", async () => {
  const sb = await Sandbox.create({}, client);
  const result = await sb.runCode("print(6 * 7)");
  assert.equal(result.stdout, "42\n");
  assert.equal(result.exitCode, 0);
  await sb.delete();
  await assert.rejects(Sandbox.connect(sb.id, client), (err: unknown) => err instanceof CellblockError && err.status === 404);
});

test("streamed stdout arrives in order and before the process exits", async () => {
  const sb = await Sandbox.create({}, client);
  const chunks: string[] = [];
  const start = Date.now();
  let firstAt = 0;
  const result = await sb.exec("for i in 1 2 3; do echo line$i; sleep 0.3; done", {
    onStdout: (c) => {
      if (!firstAt) firstAt = Date.now() - start;
      chunks.push(c);
    },
  });
  const total = Date.now() - start;
  assert.equal(chunks.join(""), "line1\nline2\nline3\n");
  assert.equal(result.stdout, chunks.join(""));
  assert.ok(firstAt < total - 500, "first chunk should arrive while the command still runs");
  await sb.delete();
});

test("timeout reports timed_out with exit 124", async () => {
  const sb = await Sandbox.create({}, client);
  const result = await sb.exec("sleep 60", { timeout: 2 });
  assert.equal(result.timedOut, true);
  assert.equal(result.exitCode, 124);
  await sb.delete();
});

test("files round-trip, including binary and traversal rejection", async () => {
  const sb = await Sandbox.create({}, client);
  await sb.files.write("notes/a.txt", "hello");
  assert.equal(await sb.files.readText("notes/a.txt"), "hello");
  const blob = new Uint8Array([0, 255, 1, 254]);
  await sb.files.write("bin/x.dat", blob);
  assert.deepEqual(await sb.files.read("bin/x.dat"), blob);
  assert.deepEqual((await sb.files.list("notes")).map((e) => e.name), ["a.txt"]);
  await assert.rejects(sb.files.read("../../etc/passwd"), (err: unknown) => err instanceof CellblockError && err.status === 400);
  await sb.delete();
});

test("a bad key is rejected with 401", async () => {
  const bad = new Cellblock({ baseUrl: BASE, apiKey: "cb_wrong" });
  await assert.rejects(Sandbox.create({}, bad), (err: unknown) => err instanceof CellblockError && err.status === 401);
});
