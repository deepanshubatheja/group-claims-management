import assert from "node:assert/strict";
import { EventEmitter, once } from "node:events";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { PassThrough } from "node:stream";
import {
  BackendProcessError,
  BackendProcessManager,
  resolveDevelopmentBackendLaunch,
  resolvePackagedBackendLaunch,
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

test("returns a controlled allowlisted operation failure without terminating the backend", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  await startConnected(manager, fake);

  const request = manager.requestDocumentOperation("ai.analysis_preview", { documentId: "document-1" });
  const backendRequest = await nextRequest(fake);
  fake.emitResponse({
    protocolVersion: "1.0",
    id: backendRequest.id,
    ok: false,
    error: { code: "DOCUMENT_NOT_PARSED", message: "Only successfully parsed documents can be analyzed." },
  });

  await assert.rejects(request, (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_OPERATION_FAILED");
    assert.equal(error.message, "Only successfully parsed documents can be analyzed.");
    return true;
  });
  assert.equal(manager.state, "ready");
  manager.stop();
});

test("returns a controlled AI connection outcome through the allowlisted protocol", async () => {
  const fake = new FakeBackendProcess();
  const manager = createManager(fake);
  await startConnected(manager, fake);

  const check = manager.checkAIConnection();
  const backendRequest = await nextRequest(fake);
  assert.equal(backendRequest.command, "ai.check_connection");
  assert.deepEqual(backendRequest.params, {});
  fake.emitResponse({
    protocolVersion: "1.0",
    id: backendRequest.id,
    ok: true,
    result: { outcome: "provider_error", checkedAt: "2026-10-15T00:00:00+00:00" },
  });

  assert.deepEqual(await check, { outcome: "provider_error", checkedAt: "2026-10-15T00:00:00+00:00" });
  assert.equal(manager.state, "ready");
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

test("restarts a failed backend only when start is called again", async () => {
  const first = new FakeBackendProcess();
  const second = new FakeBackendProcess();
  let spawnCount = 0;
  const manager = createManager(first, {
    spawn: () => (++spawnCount === 1 ? first : second),
  });
  await startConnected(manager, first);

  first.terminateUnexpectedly();
  assert.equal(manager.state, "failed");
  assert.equal(spawnCount, 1);
  await assert.rejects(manager.getHealth(), (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_UNAVAILABLE");
    return true;
  });

  await startConnected(manager, second);
  assert.equal(spawnCount, 2);
  assert.equal(manager.state, "ready");
  manager.stop();
});

test("does not restart a backend after a clean stop", async () => {
  const fake = new FakeBackendProcess();
  let spawnCount = 0;
  const manager = createManager(fake, {
    spawn: () => {
      spawnCount += 1;
      return fake;
    },
  });
  await startConnected(manager, fake);
  manager.stop();

  await assert.rejects(manager.start(), (error: unknown) => {
    assert.ok(error instanceof BackendProcessError);
    assert.equal(error.code, "BACKEND_UNAVAILABLE");
    return true;
  });
  assert.equal(spawnCount, 1);
  assert.equal(manager.state, "stopped");
});

test("forwards backend stderr lines to diagnostics", async () => {
  const fake = new FakeBackendProcess();
  const diagnostics: string[] = [];
  const manager = createManager(fake, {
    onDiagnostic: (message) => diagnostics.push(message),
  });
  await startConnected(manager, fake);

  fake.stderr.write("first line\nsecond ");
  fake.stderr.write("line\n\n");
  fake.stderr.write(`${"x".repeat(1_500)}\n`);
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(diagnostics.slice(0, 2), ["python: first line", "python: second line"]);
  assert.equal(diagnostics.length, 3);
  assert.equal(diagnostics[2], `python: ${"x".repeat(1_000)}…`);
  manager.stop();
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

test("passes a synthetic OpenAI environment value to the Python child without diagnostics", () => {
  const projectRoot = path.resolve(process.cwd(), "..");
  const storageRoot = mkdtempSync(path.join(os.tmpdir(), "group-claims-electron-env-test-"));
  const previous = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "synthetic-test-key";

  try {
    const launch = resolveDevelopmentBackendLaunch(projectRoot, storageRoot);
    assert.equal(launch.options.env?.OPENAI_API_KEY, "synthetic-test-key");
  } finally {
    if (previous === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previous;
    rmSync(storageRoot, { recursive: true, force: true });
  }
});

test("resolves the bundled backend and local OCR without a development Python environment", () => {
  const resourcesRoot = mkdtempSync(path.join(os.tmpdir(), "claimflow-resources-test-"));
  const storageRoot = mkdtempSync(path.join(os.tmpdir(), "claimflow-storage-test-"));
  const executable = path.join(resourcesRoot, "backend", "claimflow-backend");
  const tesseract = path.join(resourcesRoot, "ocr", "bin", "tesseract");
  const tessdata = path.join(resourcesRoot, "ocr", "tessdata");
  mkdirSync(path.dirname(executable), { recursive: true });
  mkdirSync(path.dirname(tesseract), { recursive: true });
  mkdirSync(tessdata, { recursive: true });
  writeFileSync(executable, "#!/bin/sh\nexit 0\n");
  writeFileSync(tesseract, "#!/bin/sh\nexit 0\n");
  chmodSync(executable, 0o700);
  chmodSync(tesseract, 0o700);

  try {
    const launch = resolvePackagedBackendLaunch(resourcesRoot, storageRoot, "darwin");
    assert.equal(launch.command, executable);
    assert.deepEqual(launch.args, ["--storage-root", storageRoot]);
    assert.equal(launch.options.env?.GROUP_CLAIMS_TESSERACT_CMD, tesseract);
    assert.equal(launch.options.env?.TESSDATA_PREFIX, tessdata);
  } finally {
    rmSync(resourcesRoot, { recursive: true, force: true });
    rmSync(storageRoot, { recursive: true, force: true });
  }
});

test("reports only key presence from the real Python backend without contacting OpenAI", async () => {
  const projectRoot = path.resolve(process.cwd(), "..");
  const storageRoot = mkdtempSync(path.join(os.tmpdir(), "group-claims-electron-ai-status-test-"));
  const previousKey = process.env.OPENAI_API_KEY;
  const previousProvider = process.env.GROUP_CLAIMS_AI_PROVIDER;
  const previousModel = process.env.GROUP_CLAIMS_AI_MODEL;
  process.env.OPENAI_API_KEY = "synthetic-test-key";
  process.env.GROUP_CLAIMS_AI_PROVIDER = "openai";
  process.env.GROUP_CLAIMS_AI_MODEL = "gpt-4.1-mini";
  const manager = new BackendProcessManager({
    launch: resolveDevelopmentBackendLaunch(projectRoot, storageRoot),
    startupTimeoutMs: 3_000,
    requestTimeoutMs: 3_000,
  });

  try {
    await manager.start();
    const status = await manager.getAIStatus();
    assert.equal(status.credentialPresent, true);
    assert.equal(status.provider, "openai");
    assert.equal(status.model, "gpt-4.1-mini");
    assert.equal(JSON.stringify(status).includes("synthetic-test-key"), false);
  } finally {
    manager.stop();
    if (previousKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previousKey;
    if (previousProvider === undefined) delete process.env.GROUP_CLAIMS_AI_PROVIDER;
    else process.env.GROUP_CLAIMS_AI_PROVIDER = previousProvider;
    if (previousModel === undefined) delete process.env.GROUP_CLAIMS_AI_MODEL;
    else process.env.GROUP_CLAIMS_AI_MODEL = previousModel;
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
