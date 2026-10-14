import assert from "node:assert/strict";
import { EventEmitter, once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { PassThrough } from "node:stream";
import {
  BackendProcessError,
  BackendProcessManager,
  resolveDevelopmentBackendLaunch,
  type BackendChildProcess,
  type BackendSpawn,
} from "../src/backend-process";

class FakeBackendProcess extends EventEmitter implements BackendChildProcess {
  public readonly stdin = new PassThrough();
  public readonly stdout = new PassThrough();
  public readonly stderr = new PassThrough();
  public killed = false;

  public constructor() {
    super();
  }

  public kill(): boolean {
    this.killed = true;
    this.emit("exit", 0, "SIGTERM");
    return true;
  }

  public emitResponse(response: unknown): void {
    this.stdout.write(`${JSON.stringify(response)}\n`);
  }

  public emitMalformedResponse(): void {
    this.stdout.write("not-json\n");
  }

  public terminateUnexpectedly(): void {
    this.emit("exit", 1, null);
  }
}

function healthResult(version = "0.1.0"): Record<string, unknown> {
  return {
    status: "ok",
    applicationVersion: version,
    pythonVersion: "3.13.0",
    platform: { system: "Darwin", release: "24.0.0", machine: "arm64" },
  };
}

async function nextRequest(fake: FakeBackendProcess): Promise<Record<string, unknown>> {
  const [chunk] = await once(fake.stdin, "data");
  return JSON.parse(Buffer.from(chunk as Buffer).toString("utf8")) as Record<string, unknown>;
}

async function startConnected(
  manager: BackendProcessManager,
  fake: FakeBackendProcess,
): Promise<void> {
  const starting = manager.start();
  const request = await nextRequest(fake);
  fake.emitResponse({ protocolVersion: "1.0", id: request.id, ok: true, result: healthResult() });
  await starting;
}

function createManager(
  fake: FakeBackendProcess,
  overrides: Partial<ConstructorParameters<typeof BackendProcessManager>[0]> = {},
): BackendProcessManager {
  const spawn: BackendSpawn = () => fake;
  return new BackendProcessManager({
    launch: { command: "python", args: ["-m", "group_claims_backend"], options: {} },
    spawn,
    startupTimeoutMs: 100,
    requestTimeoutMs: 100,
    ...overrides,
  });
}

test("correlates concurrent request and response ids", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  await startConnected(manager, fake);

  const first = manager.getHealth();
  const firstRequest = await nextRequest(fake);
  const second = manager.getHealth();
  const secondRequest = await nextRequest(fake);

  fake.emitResponse({
    protocolVersion: "1.0",
    id: secondRequest.id,
    ok: true,
    result: healthResult("second"),
  });
  fake.emitResponse({
    protocolVersion: "1.0",
    id: firstRequest.id,
    ok: true,
    result: healthResult("first"),
  });

  assert.equal((await first).applicationVersion, "first");
  assert.equal((await second).applicationVersion, "second");
  manager.stop();
});

test("fails when backend startup times out", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake, { startupTimeoutMs: 10 });

  await assert.rejects(manager.start(), (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_STARTUP_TIMEOUT");
    return true;
  });
  assert.equal(manager.state, "failed");
  assert.equal(fake.killed, true);
});

test("fails a ready request when it times out", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake, { requestTimeoutMs: 10 });
  await startConnected(manager, fake);

  const health = manager.getHealth();
  await nextRequest(fake);
  await assert.rejects(health, (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_REQUEST_TIMEOUT");
    return true;
  });
  manager.stop();
});

test("rejects malformed backend responses and terminates the process", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  const starting = manager.start();
  await nextRequest(fake);
  fake.emitMalformedResponse();

  await assert.rejects(starting, (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_PROTOCOL_ERROR");
    return true;
  });
  assert.equal(manager.state, "failed");
  assert.equal(fake.killed, true);
});

test("rejects pending requests after unexpected backend termination", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  await startConnected(manager, fake);

  const health = manager.getHealth();
  await nextRequest(fake);
  fake.terminateUnexpectedly();

  await assert.rejects(health, (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_TERMINATED");
    return true;
  });
  assert.equal(manager.state, "failed");
});

test("stops the child process and rejects pending work during clean shutdown", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  await startConnected(manager, fake);

  const health = manager.getHealth();
  await nextRequest(fake);
  manager.stop();

  await assert.rejects(health, (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_TERMINATED");
    return true;
  });
  assert.equal(manager.state, "stopped");
  assert.equal(fake.killed, true);
});

test("communicates with the real local Python health-check process", async () => {
  const projectRoot = path.resolve(process.cwd(), "..");
  const storageRoot = mkdtempSync(path.join(os.tmpdir(), "group-claims-electron-test-"));
  const manager = new BackendProcessManager({
    launch: resolveDevelopmentBackendLaunch(projectRoot, storageRoot),
    startupTimeoutMs: 3_000,
    requestTimeoutMs: 3_000,
  });

  try {
    await manager.start();
    const health = await manager.getHealth();
    assert.equal(health.status, "ok");
    assert.match(health.pythonVersion, /^3\./);
  } finally {
    manager.stop();
    rmSync(storageRoot, { recursive: true, force: true });
  }
});

test("retrieves a validated storage status through the allowlisted protocol", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  await startConnected(manager, fake);

  const storage = manager.getStorageStatus();
  const request = await nextRequest(fake);
  assert.equal(request.command, "storage.status");
  fake.emitResponse({
    protocolVersion: "1.0",
    id: request.id,
    ok: true,
    result: {
      status: "ready",
      databaseStatus: "connected",
      schemaVersion: 1,
      directoriesAvailable: true,
    },
  });

  assert.equal((await storage).schemaVersion, 1);
  manager.stop();
});
