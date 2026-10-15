import { spawn as spawnChildProcess, type SpawnOptionsWithoutStdio } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import type { Readable, Writable } from "node:stream";
import { type BackendHealth, type StorageStatus } from "./ipc-contract";

const PROTOCOL_VERSION = "1.0";
const MAX_PROTOCOL_LINE_BYTES = 65_536;
const MAX_DIAGNOSTIC_LINE_CHARS = 1_000;

export type BackendState = "idle" | "starting" | "ready" | "failed" | "stopped";

export type BackendErrorCode =
  | "BACKEND_UNAVAILABLE"
  | "BACKEND_STARTUP_TIMEOUT"
  | "BACKEND_REQUEST_TIMEOUT"
  | "BACKEND_PROTOCOL_ERROR"
  | "BACKEND_TERMINATED"
  | "BACKEND_OPERATION_FAILED";

export class BackendProcessError extends Error {
  public constructor(
    public readonly code: BackendErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "BackendProcessError";
  }
}

export interface BackendChildProcess {
  readonly stdin: Writable;
  readonly stdout: Readable;
  readonly stderr: Readable;
  kill(signal?: NodeJS.Signals): boolean;
  once(event: "error", listener: (error: Error) => void): this;
  once(
    event: "exit",
    listener: (code: number | null, signal: NodeJS.Signals | null) => void,
  ): this;
}

export interface BackendLaunchConfiguration {
  command: string;
  args: string[];
  options: SpawnOptionsWithoutStdio;
}

type BackendCommand = "health.check" | "storage.status" | "ai.status" | "ai.check_connection" | "ai.set_authorization" | "ai.analysis_preview" | "ai.analyze_document" | "ai.analysis_status" | "ai.analysis_findings" | "ai.question_preview" | "ai.ask_question" | "ai.question_status" | "matter.review" | "matter.billing_preview" | "matter.analysis_batch_preview" | "matter.start_analysis_batch" | "matter.analysis_batch_status" | "matter.question_preview" | "matter.ask_question" | "matter.question_status" | "documents.import" | "documents.list" | "documents.remove" | "documents.content" | "documents.retry_parse";

export type BackendSpawn = (
  command: string,
  args: string[],
  options: SpawnOptionsWithoutStdio,
) => BackendChildProcess;

interface PendingRequest {
  timer: NodeJS.Timeout;
  resolve: (value: Record<string, unknown>) => void;
  reject: (reason: Error) => void;
}

interface BackendResponse {
  protocolVersion: string;
  id: string | null;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: {
    code: string;
    message: string;
  };
}

export interface BackendProcessManagerOptions {
  launch: BackendLaunchConfiguration;
  spawn?: BackendSpawn;
  startupTimeoutMs?: number;
  requestTimeoutMs?: number;
  onDiagnostic?: (message: string) => void;
}

function spawnBackend(
  command: string,
  args: string[],
  options: SpawnOptionsWithoutStdio,
): BackendChildProcess {
  return spawnChildProcess(command, args, {
    ...options,
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  }) as BackendChildProcess;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBackendResponse(value: unknown): value is BackendResponse {
  if (!isRecord(value) || value.protocolVersion !== PROTOCOL_VERSION) {
    return false;
  }

  if (typeof value.id !== "string" && value.id !== null) {
    return false;
  }

  if (typeof value.ok !== "boolean") {
    return false;
  }

  if (value.ok) {
    return isRecord(value.result);
  }

  return (
    isRecord(value.error) &&
    typeof value.error.code === "string" &&
    typeof value.error.message === "string"
  );
}

function parseBackendHealth(value: Record<string, unknown>): BackendHealth {
  if (
    value.status !== "ok" ||
    typeof value.applicationVersion !== "string" ||
    value.applicationVersion.length === 0 ||
    typeof value.pythonVersion !== "string" ||
    value.pythonVersion.length === 0 ||
    !isRecord(value.platform) ||
    typeof value.platform.system !== "string" ||
    value.platform.system.length === 0 ||
    typeof value.platform.release !== "string" ||
    value.platform.release.length === 0 ||
    typeof value.platform.machine !== "string" ||
    value.platform.machine.length === 0
  ) {
    throw new BackendProcessError(
      "BACKEND_PROTOCOL_ERROR",
      "The backend returned an invalid health response.",
    );
  }

  return Object.freeze({
    status: "ok",
    applicationVersion: value.applicationVersion,
    pythonVersion: value.pythonVersion,
    platform: Object.freeze({
      system: value.platform.system,
      release: value.platform.release,
      machine: value.platform.machine,
    }),
  });
}

function parseStorageStatus(value: Record<string, unknown>): StorageStatus {
  if (
    (value.status !== "ready" && value.status !== "error") ||
    (value.databaseStatus !== "connected" && value.databaseStatus !== "unavailable") ||
    (typeof value.schemaVersion !== "number" && value.schemaVersion !== null) ||
    (typeof value.schemaVersion === "number" &&
      (!Number.isInteger(value.schemaVersion) || value.schemaVersion < 0)) ||
    typeof value.directoriesAvailable !== "boolean"
  ) {
    throw new BackendProcessError(
      "BACKEND_PROTOCOL_ERROR",
      "The backend returned an invalid storage response.",
    );
  }

  return Object.freeze({
    status: value.status,
    databaseStatus: value.databaseStatus,
    schemaVersion: value.schemaVersion,
    directoriesAvailable: value.directoriesAvailable,
  });
}

export function resolveDevelopmentBackendLaunch(
  projectRoot: string,
  storageRoot: string,
  platform: NodeJS.Platform = process.platform,
): BackendLaunchConfiguration {
  const backendDirectory = path.join(projectRoot, "backend");
  const interpreter =
    platform === "win32"
      ? path.join(backendDirectory, ".venv", "Scripts", "python.exe")
      : path.join(backendDirectory, ".venv", "bin", "python");

  if (!existsSync(interpreter)) {
    throw new BackendProcessError(
      "BACKEND_UNAVAILABLE",
      "The project-local Python virtual environment was not found.",
    );
  }

  if (
    !path.isAbsolute(storageRoot) ||
    path.parse(storageRoot).root === path.resolve(storageRoot)
  ) {
    throw new BackendProcessError(
      "BACKEND_UNAVAILABLE",
      "The application storage directory is unavailable.",
    );
  }

  return {
    command: interpreter,
    args: ["-m", "group_claims_backend", "--storage-root", storageRoot],
    options: {
      cwd: backendDirectory,
      env: { ...process.env, PYTHONUTF8: "1" },
      shell: false,
      windowsHide: true,
    },
  };
}

export class BackendProcessManager {
  private readonly spawn: BackendSpawn;
  private readonly startupTimeoutMs: number;
  private readonly requestTimeoutMs: number;
  private readonly onDiagnostic: (message: string) => void;
  private readonly pendingRequests = new Map<string, PendingRequest>();
  private child: BackendChildProcess | undefined;
  private stdoutBuffer = Buffer.alloc(0);
  private startupPromise: Promise<void> | undefined;
  private currentState: BackendState = "idle";

  public constructor(private readonly options: BackendProcessManagerOptions) {
    this.spawn = options.spawn ?? spawnBackend;
    // Covers interpreter launch plus the first health check; cold starts on
    // office laptops (antivirus scanning, first-run bytecode compilation) can be slow.
    this.startupTimeoutMs = options.startupTimeoutMs ?? 20_000;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 5_000;
    this.onDiagnostic = options.onDiagnostic ?? (() => undefined);
  }

  public get state(): BackendState {
    return this.currentState;
  }

  public start(): Promise<void> {
    if (this.currentState === "ready") {
      return Promise.resolve();
    }

    if (this.currentState === "starting" && this.startupPromise !== undefined) {
      return this.startupPromise;
    }

    if (this.currentState === "stopped") {
      return Promise.reject(
        new BackendProcessError("BACKEND_UNAVAILABLE", "The backend was stopped."),
      );
    }

    // A failed backend is never restarted automatically; it is only restarted
    // when a caller explicitly invokes start() again (for example, a user retry).
    this.currentState = "starting";
    this.stdoutBuffer = Buffer.alloc(0);
    try {
      const child = this.spawn(
        this.options.launch.command,
        this.options.launch.args,
        this.options.launch.options,
      );
      this.child = child;
      this.attachChildListeners(child);
    } catch {
      const error = new BackendProcessError(
        "BACKEND_UNAVAILABLE",
        "The backend process could not be started.",
      );
      this.fail(error);
      return Promise.reject(error);
    }

    this.startupPromise = this.sendRequest("health.check", {}, this.startupTimeoutMs)
      .then((result) => {
        parseBackendHealth(result);
        this.currentState = "ready";
      })
      .catch((error: unknown) => {
        const failure = this.toBackendError(error);
        this.fail(failure);
        throw failure;
      });

    return this.startupPromise;
  }

  public async getHealth(): Promise<BackendHealth> {
    if (this.currentState === "starting" && this.startupPromise !== undefined) {
      await this.startupPromise;
    }

    if (this.currentState !== "ready") {
      throw new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is not connected.");
    }

    const result = await this.sendRequest("health.check", {}, this.requestTimeoutMs);
    return parseBackendHealth(result);
  }

  public async getStorageStatus(): Promise<StorageStatus> {
    if (this.currentState === "starting" && this.startupPromise !== undefined) {
      await this.startupPromise;
    }

    if (this.currentState !== "ready") {
      throw new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is not connected.");
    }

    const result = await this.sendRequest("storage.status", {}, this.requestTimeoutMs);
    return parseStorageStatus(result);
  }
  public async getAIStatus(): Promise<Record<string, unknown>> { return this.requestDocumentOperation("ai.status", {}); }
  public async checkAIConnection(): Promise<Record<string, unknown>> { return this.requestDocumentOperation("ai.check_connection", {}); }
  public async setAIAuthorization(authorized: boolean): Promise<Record<string, unknown>> { return this.requestDocumentOperation("ai.set_authorization", { authorized, acknowledged: true }); }

  public async requestDocumentOperation(command: Exclude<BackendCommand, "health.check" | "storage.status">, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (this.currentState === "starting" && this.startupPromise !== undefined) await this.startupPromise;
    if (this.currentState !== "ready") throw new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is not connected.");
    return this.sendRequest(command, params, 60_000);
  }

  public stop(): void {
    const child = this.child;
    this.child = undefined;
    this.currentState = "stopped";
    this.startupPromise = undefined;
    this.rejectAllPending(
      new BackendProcessError("BACKEND_TERMINATED", "The backend was stopped."),
    );

    if (child !== undefined) {
      child.stdin.end();
      child.kill("SIGTERM");
    }
  }

  private attachChildListeners(child: BackendChildProcess): void {
    child.stdout.on("data", (chunk: Buffer | string) => {
      this.handleStdout(child, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, "utf8"));
    });
    // Python writes operational logs (never raw request content) to stderr. Forward
    // them line by line to diagnostics only; they are never sent to the renderer.
    let stderrText = "";
    const forwardStderrLine = (line: string): void => {
      const trimmed = line.trimEnd();
      if (trimmed.length === 0) {
        return;
      }
      this.onDiagnostic(
        `python: ${
          trimmed.length > MAX_DIAGNOSTIC_LINE_CHARS
            ? `${trimmed.slice(0, MAX_DIAGNOSTIC_LINE_CHARS)}…`
            : trimmed
        }`,
      );
    };
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      stderrText += chunk;
      const lines = stderrText.split("\n");
      stderrText = lines.pop() ?? "";
      for (const line of lines) {
        forwardStderrLine(line);
      }
      if (stderrText.length > MAX_DIAGNOSTIC_LINE_CHARS) {
        forwardStderrLine(stderrText);
        stderrText = "";
      }
    });
    child.stderr.on("end", () => {
      forwardStderrLine(stderrText);
      stderrText = "";
    });
    child.once("error", () => {
      this.failForChild(
        child,
        new BackendProcessError("BACKEND_UNAVAILABLE", "The backend process encountered an error."),
      );
    });
    child.once("exit", () => {
      this.failForChild(
        child,
        new BackendProcessError("BACKEND_TERMINATED", "The backend process terminated."),
      );
    });
  }

  private handleStdout(child: BackendChildProcess, chunk: Buffer): void {
    if (this.child !== child || this.currentState === "stopped") {
      return;
    }

    this.stdoutBuffer = Buffer.concat([this.stdoutBuffer, chunk]);
    if (this.stdoutBuffer.length > MAX_PROTOCOL_LINE_BYTES && !this.stdoutBuffer.includes(0x0a)) {
      this.failForChild(
        child,
        new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend response exceeded the size limit."),
      );
      return;
    }

    let newlineIndex = this.stdoutBuffer.indexOf(0x0a);
    while (newlineIndex !== -1) {
      const line = this.stdoutBuffer.subarray(0, newlineIndex);
      this.stdoutBuffer = this.stdoutBuffer.subarray(newlineIndex + 1);
      if (line.length > MAX_PROTOCOL_LINE_BYTES) {
        this.failForChild(
          child,
          new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend response exceeded the size limit."),
        );
        return;
      }

      this.handleResponseLine(child, line);
      if (this.child !== child) {
        return;
      }
      newlineIndex = this.stdoutBuffer.indexOf(0x0a);
    }
  }

  private handleResponseLine(child: BackendChildProcess, line: Buffer): void {
    let response: unknown;
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(line);
      response = JSON.parse(text) as unknown;
    } catch {
      this.failForChild(
        child,
        new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned malformed JSON."),
      );
      return;
    }

    if (!isBackendResponse(response) || response.id === null) {
      this.failForChild(
        child,
        new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned an invalid response."),
      );
      return;
    }

    const pending = this.pendingRequests.get(response.id);
    if (pending === undefined) {
      this.failForChild(
        child,
        new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned an unexpected response."),
      );
      return;
    }

    clearTimeout(pending.timer);
    this.pendingRequests.delete(response.id);
    if (response.ok) {
      pending.resolve(response.result ?? {});
      return;
    }

    pending.reject(
      new BackendProcessError("BACKEND_OPERATION_FAILED", response.error?.message ?? "The backend rejected the request."),
    );
  }

  private sendRequest(
    command: BackendCommand,
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<Record<string, unknown>> {
    const child = this.child;
    if (child === undefined) {
      return Promise.reject(
        new BackendProcessError("BACKEND_UNAVAILABLE", "The backend process is unavailable."),
      );
    }

    const requestId = randomUUID();
    const request = JSON.stringify({
      protocolVersion: PROTOCOL_VERSION,
      id: requestId,
      command,
      params,
    });

    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(requestId);
        const error = new BackendProcessError(
          this.currentState === "starting"
            ? "BACKEND_STARTUP_TIMEOUT"
            : "BACKEND_REQUEST_TIMEOUT",
          this.currentState === "starting"
            ? "The backend did not become ready in time."
            : "The backend did not respond in time.",
        );
        reject(error);
        this.failForChild(child, error);
      }, timeoutMs);

      this.pendingRequests.set(requestId, { timer, resolve, reject });
      try {
        child.stdin.write(`${request}\n`, "utf8");
      } catch {
        clearTimeout(timer);
        this.pendingRequests.delete(requestId);
        reject(
          new BackendProcessError("BACKEND_TERMINATED", "The backend process is unavailable."),
        );
      }
    });
  }

  private failForChild(child: BackendChildProcess, error: BackendProcessError): void {
    if (this.child !== child || this.currentState === "stopped") {
      return;
    }
    this.fail(error);
  }

  private fail(error: BackendProcessError): void {
    const child = this.child;
    this.child = undefined;
    this.currentState = "failed";
    this.stdoutBuffer = Buffer.alloc(0);
    this.rejectAllPending(error);
    this.onDiagnostic(`${error.code}: ${error.message}`);

    if (child !== undefined) {
      child.stdin.end();
      child.kill("SIGTERM");
    }
  }

  private rejectAllPending(error: BackendProcessError): void {
    for (const pending of this.pendingRequests.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pendingRequests.clear();
  }

  private toBackendError(error: unknown): BackendProcessError {
    if (error instanceof BackendProcessError) {
      return error;
    }
    return new BackendProcessError("BACKEND_UNAVAILABLE", "The backend process is unavailable.");
  }
}
