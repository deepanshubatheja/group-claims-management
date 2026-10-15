import { app, BrowserWindow, dialog, ipcMain, session } from "electron";
import { access, lstat } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  BackendProcessError,
  BackendProcessManager,
  resolveDevelopmentBackendLaunch,
} from "./backend-process";
import {
  GET_APP_INFO_CHANNEL,
  GET_BACKEND_HEALTH_CHANNEL,
  GET_STORAGE_STATUS_CHANNEL,
  GET_AI_STATUS_CHANNEL, CHECK_AI_CONNECTION_CHANNEL, SET_AI_AUTHORIZATION_CHANNEL,
  GET_DOCUMENT_ANALYSIS_PREVIEW_CHANNEL, START_DOCUMENT_ANALYSIS_CHANNEL,
  GET_DOCUMENT_ANALYSIS_STATUS_CHANNEL, GET_DOCUMENT_ANALYSIS_FINDINGS_CHANNEL,
  PREVIEW_DOCUMENT_QUESTION_CHANNEL, ASK_DOCUMENT_QUESTION_CHANNEL, GET_DOCUMENT_QUESTION_STATUS_CHANNEL,
  GET_MATTER_REVIEW_CHANNEL, GET_BILLING_PREVIEW_CHANNEL,
  PREVIEW_MATTER_ANALYSIS_BATCH_CHANNEL, START_MATTER_ANALYSIS_BATCH_CHANNEL, GET_MATTER_ANALYSIS_BATCH_STATUS_CHANNEL,
  PREVIEW_MATTER_QUESTION_CHANNEL, ASK_MATTER_QUESTION_CHANNEL, GET_MATTER_QUESTION_STATUS_CHANNEL,
  SELECT_AND_IMPORT_DOCUMENTS_CHANNEL, SELECT_FOLDER_FOR_IMPORT_CHANNEL, CONFIRM_FOLDER_IMPORT_CHANNEL, CANCEL_FOLDER_IMPORT_CHANNEL, FOLDER_IMPORT_PROGRESS_CHANNEL, IMPORT_DROPPED_DOCUMENTS_CHANNEL, REMOVE_DOCUMENT_CHANNEL, LIST_DOCUMENTS_CHANNEL, GET_DOCUMENT_CONTENT_CHANNEL, RETRY_DOCUMENT_PARSE_CHANNEL,
  type AppInfo,
  type BackendHealthResponse,
  type StorageStatusResponse,
  type AIStatusResponse, type AIConnectionCheckResponse, type AIConnectionCheck,
  type DocumentAnalysisPreviewResponse, type DocumentAnalysisStartResponse,
  type DocumentAnalysisStatusResponse, type DocumentAnalysisFindingsResponse,
  type DocumentAnalysisPreview, type DocumentAnalysisJob, type DocumentAnalysisFinding,
  type DocumentQuestionPreviewResponse, type DocumentQuestionStartResponse, type DocumentQuestionStatusResponse,
  type DocumentQuestionPreview, type DocumentQuestionStatus,
  type MatterReviewResponse, type MatterAnalysisBatchPreviewResponse, type MatterAnalysisBatchResponse,
  type MatterQuestionPreviewResponse, type MatterQuestionResponse, type MatterQuestionFollowUp,
  type BillingPreviewResponse, type MatterReviewResult, type MatterAnalysisBatchPreview, type MatterAnalysisBatch, type MatterQuestionPreview, type MatterQuestionStatus, type BillingPreviewResult,
  type DocumentImportResponse, type DocumentRemoveResponse,
} from "./ipc-contract";
import { discoverDroppedImportableFiles, discoverImportableFiles, type AuthorizedFolderFile, type FolderDiscovery } from "./folder-import";

const MAX_IMPORT_FILE_BYTES = 50 * 1024 * 1024;
const supportedExtensions = new Set([".xlsx", ".xls", ".csv", ".pdf"]);

const developmentServerUrl = process.env.ELECTRON_RENDERER_URL;
const rendererHtmlPath = path.join(__dirname, "../../frontend/dist/index.html");
const rendererHtmlUrl = pathToFileURL(rendererHtmlPath).toString();
const projectRoot = path.join(__dirname, "../..");
let backendProcess: BackendProcessManager | undefined;
const folderAuthorizations = new Map<string, { files: AuthorizedFolderFile[]; skipped: number; expiresAt: number }>();

function isAllowedRendererUrl(url: string): boolean {
  try {
    if (developmentServerUrl !== undefined) {
      return new URL(url).origin === new URL(developmentServerUrl).origin;
    }

    return url === rendererHtmlUrl;
  } catch {
    return false;
  }
}

function createAppInfo(): AppInfo {
  const appInfo: AppInfo = {
    name: app.getName(),
    version: app.getVersion(),
    platform: process.platform,
  };

  if (
    appInfo.name.length === 0 ||
    appInfo.version.length === 0 ||
    !["darwin", "win32", "linux"].includes(appInfo.platform)
  ) {
    throw new Error("Unable to provide validated application information.");
  }

  return Object.freeze(appInfo);
}

function createContentSecurityPolicy(): string {
  // Vite's React Fast Refresh preamble is an inline module in development.
  // Production bundles do not need this exception.
  const scriptSource =
    developmentServerUrl !== undefined
      ? "script-src 'self' 'unsafe-inline'"
      : "script-src 'self'";
  const styleSource =
    developmentServerUrl !== undefined
      ? "style-src 'self' 'unsafe-inline'"
      : "style-src 'self'";
  const directives = [
    "default-src 'self'",
    scriptSource,
    styleSource,
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-src 'none'",
  ];

  if (developmentServerUrl !== undefined) {
    const server = new URL(developmentServerUrl);
    const websocketProtocol = server.protocol === "https:" ? "wss:" : "ws:";
    const websocketUrl = `${websocketProtocol}//${server.host}`;
    directives[5] = `connect-src 'self' ${server.origin} ${websocketUrl}`;
  }

  return directives.join("; ");
}

function configureSessionSecurity(): void {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });

  session.defaultSession.setPermissionCheckHandler(() => false);

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.resourceType !== "mainFrame") {
      callback({ cancel: false });
      return;
    }

    callback({
      cancel: false,
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [createContentSecurityPolicy()],
      },
    });
  });
}

function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    show: false,
    backgroundColor: "#f6f8fb",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
    },
  });

  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedRendererUrl(url)) {
      event.preventDefault();
    }
  });

  window.once("ready-to-show", () => window.show());

  if (developmentServerUrl !== undefined) {
    void window.loadURL(developmentServerUrl);
  } else {
    void window.loadFile(rendererHtmlPath);
  }

  return window;
}

function createBackendHealthFailure(
  error: unknown,
): { ok: false; error: { code: string; message: string } } {
  if (error instanceof BackendProcessError) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
      },
    };
  }

  return {
    ok: false,
    error: {
      code: "BACKEND_UNAVAILABLE",
      message: "The backend is unavailable.",
    },
  };
}

function documentFailure(error: unknown): { ok: false; error: { code: string; message: string } } {
  return createBackendHealthFailure(error);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAnalysisSummary(value: unknown): boolean {
  return isRecord(value) && (typeof value.text === "string" || value.text === null)
    && typeof value.unknown === "boolean" && Array.isArray(value.citations)
    && value.citations.every((citation) => isRecord(citation)
      && typeof citation.sourceReference === "string" && typeof citation.evidenceSnippet === "string");
}

function isAnalysisJob(value: unknown): value is DocumentAnalysisJob {
  return isRecord(value) && typeof value.jobId === "string" && typeof value.documentId === "string"
    && typeof value.extractionId === "string" && typeof value.provider === "string"
    && (typeof value.model === "string" || value.model === null)
    && typeof value.promptVersion === "string" && typeof value.schemaVersion === "string"
    && ["queued", "running", "completed", "failed", "blocked"].includes(String(value.status))
    && (typeof value.errorCode === "string" || value.errorCode === null)
    && (isRecord(value.coverage) || value.coverage === null)
    && isAnalysisSummary(value.summary)
    && isRecord(value.usage)
    && (typeof value.usage.estimatedCostUsd === "string" || value.usage.estimatedCostUsd === null)
    && (typeof value.usage.inputTokens === "number" || value.usage.inputTokens === null)
    && (typeof value.usage.outputTokens === "number" || value.usage.outputTokens === null)
    && (typeof value.usage.totalTokens === "number" || value.usage.totalTokens === null)
    && (typeof value.usage.actualCostEstimateUsd === "string" || value.usage.actualCostEstimateUsd === null)
    && typeof value.createdAt === "string" && (typeof value.completedAt === "string" || value.completedAt === null);
}

function isAnalysisPreview(value: unknown): value is DocumentAnalysisPreview {
  return isRecord(value) && isRecord(value.document) && (typeof value.provider === "string" || value.provider === null)
    && (typeof value.model === "string" || value.model === null) && typeof value.globalAuthorization === "boolean"
    && typeof value.configured === "boolean" && isRecord(value.scope) && typeof value.scope.extractionId === "string"
    && typeof value.scope.chunks === "number" && typeof value.scope.sourceItems === "number"
    && typeof value.scope.totalSourceItems === "number" && typeof value.scope.excludedSourceItems === "number"
    && typeof value.scope.includedCharacters === "number" && typeof value.scope.excludedCharacters === "number"
    && typeof value.scope.truncated === "boolean" && isRecord(value.cost)
    && typeof value.cost.estimateMethod === "string" && typeof value.cost.estimatedInputTokens === "number"
    && typeof value.cost.maxOutputTokens === "number"
    && (typeof value.cost.estimatedCostUsd === "string" || value.cost.estimatedCostUsd === null)
    && typeof value.cost.limitUsd === "string" && typeof value.cost.withinLimit === "boolean"
    && typeof value.cost.maxRequests === "number" && typeof value.cost.maxRequestAttempts === "number"
    && (value.latestJob === null || isAnalysisJob(value.latestJob));
}

function isAnalysisFinding(value: unknown): value is DocumentAnalysisFinding {
  return isRecord(value) && typeof value.id === "string" && typeof value.proposalType === "string"
    && typeof value.fieldName === "string" && typeof value.unknown === "boolean"
    && (typeof value.confidence === "number" || value.confidence === null)
    && value.validationStatus === "validated" && typeof value.sourceReference === "string"
    && (typeof value.evidenceSnippet === "string" || value.evidenceSnippet === null)
    && (typeof value.modelVersion === "string" || value.modelVersion === null);
}

function isQuestionScope(value: unknown): boolean {
  return isRecord(value) && typeof value.sourceItems === "number" && typeof value.totalSourceItems === "number"
    && typeof value.excludedSourceItems === "number" && typeof value.truncated === "boolean";
}

function isQuestionCost(value: unknown): boolean {
  return isRecord(value) && typeof value.estimatedInputTokens === "number" && typeof value.maxOutputTokens === "number"
    && (typeof value.estimatedCostUsd === "string" || value.estimatedCostUsd === null)
    && (typeof value.limitUsd === "string" || value.limitUsd === null)
    && typeof value.withinLimit === "boolean" && typeof value.maxRequestAttempts === "number";
}

function isQuestionPreview(value: unknown): value is DocumentQuestionPreview {
  return isRecord(value) && (typeof value.provider === "string" || value.provider === null)
    && (typeof value.model === "string" || value.model === null) && typeof value.globalAuthorization === "boolean"
    && typeof value.configured === "boolean" && isQuestionScope(value.scope) && isQuestionCost(value.cost);
}

function isQuestionStatus(value: unknown): value is DocumentQuestionStatus {
  return isRecord(value) && typeof value.questionId === "string" && typeof value.documentId === "string"
    && ["queued", "running", "completed", "failed"].includes(String(value.status))
    && (typeof value.answer === "string" || value.answer === null) && typeof value.unknown === "boolean"
    && Array.isArray(value.citations) && value.citations.every((citation) => isRecord(citation)
      && typeof citation.sourceReference === "string" && typeof citation.evidenceSnippet === "string")
    && (typeof value.errorCode === "string" || value.errorCode === null)
    && (typeof value.model === "string" || value.model === null) && isQuestionScope(value.scope)
    && isRecord(value.usage)
    && (typeof value.usage.inputTokens === "number" || value.usage.inputTokens === null)
    && (typeof value.usage.outputTokens === "number" || value.usage.outputTokens === null)
    && (typeof value.usage.totalTokens === "number" || value.usage.totalTokens === null)
    && (typeof value.usage.actualCostEstimateUsd === "string" || value.usage.actualCostEstimateUsd === null);
}

function isMatterReview(value: unknown): value is MatterReviewResult {
  return isRecord(value) && Array.isArray(value.documents) && Array.isArray(value.summaries)
    && Array.isArray(value.findings) && isRecord(value.summary)
    && value.documents.every((document) => isRecord(document) && typeof document.id === "string"
      && typeof document.filename === "string" && typeof document.format === "string"
      && typeof document.importedAt === "string" && typeof document.analysisAvailable === "boolean")
    && value.findings.every((finding) => isRecord(finding) && typeof finding.id === "string"
      && typeof finding.documentId === "string" && typeof finding.proposalType === "string"
      && typeof finding.fieldName === "string" && typeof finding.unknown === "boolean"
      && typeof finding.sourceReference === "string"
      && (typeof finding.evidenceSnippet === "string" || finding.evidenceSnippet === null));
}

function isBillingPreview(value: unknown): value is BillingPreviewResult {
  return isRecord(value) && typeof value.feePool === "string" && typeof value.currency === "string"
    && typeof value.totalEligibleAmount === "string" && typeof value.allocatedTotal === "string"
    && Array.isArray(value.assumptions) && value.assumptions.every((item) => typeof item === "string")
    && Array.isArray(value.allocations) && value.allocations.every((row) => isRecord(row)
      && typeof row.id === "string" && typeof row.label === "string"
      && typeof row.eligibleAmount === "string" && typeof row.share === "string"
      && typeof row.allocatedFee === "string" && typeof row.currency === "string");
}

function isMatterAnalysisBatchPreview(value: unknown): value is MatterAnalysisBatchPreview {
  return isRecord(value) && Array.isArray(value.documents) && typeof value.globalAuthorization === "boolean"
    && typeof value.configured === "boolean" && typeof value.estimatedCostUsd === "string"
    && typeof value.costLimitUsd === "string" && typeof value.withinCostLimit === "boolean"
    && typeof value.ready === "boolean" && typeof value.maxDocuments === "number"
    && typeof value.maxRequestsPerDocument === "number" && typeof value.automaticRetries === "number"
    && value.documents.every((document) => isRecord(document) && typeof document.documentId === "string"
      && typeof document.filename === "string" && typeof document.ready === "boolean"
      && (typeof document.errorCode === "string" || document.errorCode === null)
      && (isRecord(document.scope) || document.scope === null)
      && (isRecord(document.cost) || document.cost === null));
}

function isMatterAnalysisBatch(value: unknown): value is MatterAnalysisBatch {
  return isRecord(value) && typeof value.batchId === "string" && typeof value.createdAt === "string"
    && ["queued", "running", "completed", "partial"].includes(String(value.status))
    && (typeof value.estimatedCostUsd === "string" || value.estimatedCostUsd === null)
    && typeof value.costLimitUsd === "string" && isRecord(value.summary)
    && typeof value.summary.total === "number" && typeof value.summary.completed === "number"
    && typeof value.summary.failed === "number" && typeof value.summary.pending === "number"
    && Array.isArray(value.documents) && value.documents.every((document) => isRecord(document)
      && typeof document.documentId === "string" && typeof document.filename === "string"
      && ["pending", "running", "completed", "failed", "blocked"].includes(String(document.status))
      && (typeof document.analysisJobId === "string" || document.analysisJobId === null)
      && (typeof document.errorCode === "string" || document.errorCode === null));
}

function isMatterQuestionPreview(value: unknown): value is MatterQuestionPreview {
  return isRecord(value) && (typeof value.provider === "string" || value.provider === null)
    && (typeof value.model === "string" || value.model === null)
    && typeof value.globalAuthorization === "boolean" && typeof value.configured === "boolean"
    && isQuestionScope(value.scope) && isQuestionCost(value.cost);
}

function isMatterQuestionStatus(value: unknown): value is MatterQuestionStatus {
  return isRecord(value) && typeof value.questionId === "string"
    && ["queued", "running", "completed", "failed"].includes(String(value.status))
    && (typeof value.answer === "string" || value.answer === null) && typeof value.unknown === "boolean"
    && Array.isArray(value.citations) && value.citations.every((citation) => isRecord(citation)
      && typeof citation.documentId === "string" && typeof citation.filename === "string"
      && typeof citation.sourceReference === "string" && typeof citation.evidenceSnippet === "string")
    && (typeof value.errorCode === "string" || value.errorCode === null)
    && (typeof value.model === "string" || value.model === null)
    && isQuestionScope(value.scope) && isRecord(value.usage)
    && (typeof value.usage.inputTokens === "number" || value.usage.inputTokens === null)
    && (typeof value.usage.outputTokens === "number" || value.usage.outputTokens === null)
    && (typeof value.usage.totalTokens === "number" || value.usage.totalTokens === null)
    && (typeof value.usage.actualCostEstimateUsd === "string" || value.usage.actualCostEstimateUsd === null);
}

function isAIConnectionCheck(value: unknown): value is AIConnectionCheck {
  return isRecord(value)
    && ["available", "provider_not_configured", "api_key_missing", "authentication_rejected", "permission_denied", "rate_limited", "network_unavailable", "timeout", "provider_error"].includes(String(value.outcome))
    && typeof value.checkedAt === "string";
}

async function validateAuthorizedImportPaths(value: unknown): Promise<string[]> {
  if (!Array.isArray(value) || value.length < 1 || value.length > 20 || !value.every((item) => typeof item === "string" && path.isAbsolute(item))) throw new BackendProcessError("BACKEND_UNAVAILABLE", "Invalid document selection.");
  const paths = value as string[];
  for (const selectedPath of paths) {
    const extension = path.extname(selectedPath).toLowerCase();
    const details = await lstat(selectedPath);
    if (!supportedExtensions.has(extension) || !details.isFile() || details.isSymbolicLink() || details.size > MAX_IMPORT_FILE_BYTES) throw new BackendProcessError("BACKEND_UNAVAILABLE", "A selected file is unsupported or unsafe.");
    await access(selectedPath, fsConstants.R_OK);
  }
  return paths;
}

function startDevelopmentBackend(storageRoot: string): void {
  if (app.isPackaged) {
    // Packaging a standalone backend executable is a later release milestone.
    return;
  }

  try {
    backendProcess = new BackendProcessManager({
      launch: resolveDevelopmentBackendLaunch(projectRoot, storageRoot),
      onDiagnostic: (message) => console.error(`Backend process: ${message}`),
    });
    void backendProcess.start().catch(() => undefined);
  } catch (error: unknown) {
    console.error("Backend process could not be configured.");
  }
}

// Called on explicit renderer health requests (initial load or user retry): restarts a
// failed backend once per request, and retries configuration if the venv was missing.
async function ensureDevelopmentBackend(): Promise<BackendProcessManager | undefined> {
  if (backendProcess === undefined) {
    startDevelopmentBackend(app.getPath("userData"));
  }

  if (backendProcess?.state === "failed") {
    await backendProcess.start();
  }

  return backendProcess;
}

app.setName("Group Claims Management");

app.whenReady().then(() => {
  configureSessionSecurity();
  startDevelopmentBackend(app.getPath("userData"));

  ipcMain.handle(GET_APP_INFO_CHANNEL, (event, ...arguments_: unknown[]) => {
    const senderFrame = event.senderFrame;

    if (
      arguments_.length !== 0 ||
      senderFrame === null ||
      !isAllowedRendererUrl(senderFrame.url)
    ) {
      throw new Error("Invalid application information request.");
    }

    return createAppInfo();
  });

  ipcMain.handle(
    GET_BACKEND_HEALTH_CHANNEL,
    async (event, ...arguments_: unknown[]): Promise<BackendHealthResponse> => {
      const senderFrame = event.senderFrame;
      if (
        arguments_.length !== 0 ||
        senderFrame === null ||
        !isAllowedRendererUrl(senderFrame.url)
      ) {
        throw new Error("Invalid backend health request.");
      }

      try {
        const backend = await ensureDevelopmentBackend();
        if (backend === undefined) {
          return createBackendHealthFailure(
            new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."),
          );
        }

        return { ok: true, health: await backend.getHealth() };
      } catch (error: unknown) {
        return createBackendHealthFailure(error);
      }
    },
  );
  ipcMain.handle(GET_AI_STATUS_CHANNEL, async (event, ...arguments_: unknown[]): Promise<AIStatusResponse> => {
    if (arguments_.length !== 0 || event.senderFrame === null || !isAllowedRendererUrl(event.senderFrame.url)) throw new Error("Invalid AI status request.");
    try { const backend = await ensureDevelopmentBackend(); if (!backend) return createBackendHealthFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable.")); return { ok: true, ai: await backend.getAIStatus() as AIStatusResponse extends { ok: true; ai: infer T } ? T : never }; } catch (error) { return createBackendHealthFailure(error); }
  });
  ipcMain.handle(CHECK_AI_CONNECTION_CHANNEL, async (event, ...arguments_: unknown[]): Promise<AIConnectionCheckResponse> => {
    if (arguments_.length !== 0 || event.senderFrame === null || !isAllowedRendererUrl(event.senderFrame.url)) throw new Error("Invalid AI connection check request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (!backend) return createBackendHealthFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const check = await backend.checkAIConnection();
      if (!isAIConnectionCheck(check)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid AI connection information.");
      return { ok: true, check };
    } catch (error) { return createBackendHealthFailure(error); }
  });
  ipcMain.handle(SET_AI_AUTHORIZATION_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<AIStatusResponse> => {
    if (arguments_.length !== 0 || event.senderFrame === null || !isAllowedRendererUrl(event.senderFrame.url) || typeof payload !== "object" || payload === null || typeof (payload as { authorized?: unknown }).authorized !== "boolean") throw new Error("Invalid AI authorization request.");
    try { const backend = await ensureDevelopmentBackend(); if (!backend) return createBackendHealthFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable.")); return { ok: true, ai: await backend.setAIAuthorization((payload as { authorized: boolean }).authorized) as AIStatusResponse extends { ok: true; ai: infer T } ? T : never }; } catch (error) { return createBackendHealthFailure(error); }
  });

  const analysisDocumentId = (payload: unknown): string | undefined => {
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return undefined;
    const documentId = (payload as { documentId?: unknown }).documentId;
    return typeof documentId === "string" && documentId.length > 0 && documentId.length <= 128 ? documentId : undefined;
  };
  const analysisJobId = (payload: unknown): string | undefined => {
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return undefined;
    const jobId = (payload as { jobId?: unknown }).jobId;
    return typeof jobId === "string" && /^[a-f0-9-]{36}$/i.test(jobId) ? jobId : undefined;
  };
  const validAnalysisSender = (event: Electron.IpcMainInvokeEvent, arguments_: unknown[]): boolean =>
    arguments_.length === 0 && event.senderFrame !== null && isAllowedRendererUrl(event.senderFrame.url);
  ipcMain.handle(GET_DOCUMENT_ANALYSIS_PREVIEW_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentAnalysisPreviewResponse> => {
    const documentId = analysisDocumentId(payload);
    if (!validAnalysisSender(event, arguments_) || !documentId) throw new Error("Invalid document analysis preview request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const preview = await backend.requestDocumentOperation("ai.analysis_preview", { documentId });
      if (!isAnalysisPreview(preview)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid analysis information.");
      return { ok: true, preview };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(START_DOCUMENT_ANALYSIS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentAnalysisStartResponse> => {
    const documentId = analysisDocumentId(payload);
    if (!validAnalysisSender(event, arguments_) || !documentId || (payload as { acknowledged?: unknown }).acknowledged !== true) throw new Error("Invalid document analysis request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const job = await backend.requestDocumentOperation("ai.analyze_document", { documentId, acknowledged: true });
      if (!isAnalysisJob(job)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned an invalid analysis job.");
      return { ok: true, job };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_DOCUMENT_ANALYSIS_STATUS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentAnalysisStatusResponse> => {
    const jobId = analysisJobId(payload);
    if (!validAnalysisSender(event, arguments_) || !jobId) throw new Error("Invalid analysis status request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const job = await backend.requestDocumentOperation("ai.analysis_status", { jobId });
      if (!isAnalysisJob(job)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned an invalid analysis job.");
      return { ok: true, job };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_DOCUMENT_ANALYSIS_FINDINGS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentAnalysisFindingsResponse> => {
    const jobId = analysisJobId(payload);
    if (!validAnalysisSender(event, arguments_) || !jobId) throw new Error("Invalid analysis findings request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const result = await backend.requestDocumentOperation("ai.analysis_findings", { jobId, limit: 25 });
      if (!Array.isArray(result.findings) || !result.findings.every(isAnalysisFinding) || typeof result.hasMore !== "boolean") {
        throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid analysis findings.");
      }
      return { ok: true, findings: result.findings, hasMore: result.hasMore };
    } catch (error) { return documentFailure(error); }
  });
  const documentQuestion = (payload: unknown, acknowledged: boolean): { documentId: string; question: string } | undefined => {
    if (!isRecord(payload)) return undefined;
    const expected = acknowledged ? ["documentId", "question", "acknowledged"] : ["documentId", "question"];
    if (Object.keys(payload).length !== expected.length || Object.keys(payload).some((key) => !expected.includes(key))) return undefined;
    const documentId = analysisDocumentId(payload);
    const question = payload.question;
    if (!documentId || typeof question !== "string" || !question.trim() || question.length > 600) return undefined;
    if (acknowledged && payload.acknowledged !== true) return undefined;
    return { documentId, question: question.trim() };
  };
  const questionId = (payload: unknown): string | undefined => {
    if (!isRecord(payload) || Object.keys(payload).length !== 1) return undefined;
    const value = payload.questionId;
    return typeof value === "string" && /^[a-f0-9-]{36}$/i.test(value) ? value : undefined;
  };
  ipcMain.handle(PREVIEW_DOCUMENT_QUESTION_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentQuestionPreviewResponse> => {
    const request = documentQuestion(payload, false);
    if (!validAnalysisSender(event, arguments_) || !request) throw new Error("Invalid document question preview request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const preview = await backend.requestDocumentOperation("ai.question_preview", request);
      if (!isQuestionPreview(preview)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid question information.");
      return { ok: true, preview };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(ASK_DOCUMENT_QUESTION_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentQuestionStartResponse> => {
    const request = documentQuestion(payload, true);
    if (!validAnalysisSender(event, arguments_) || !request) throw new Error("Invalid document question request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const question = await backend.requestDocumentOperation("ai.ask_question", { ...request, acknowledged: true });
      if (!isQuestionStatus(question)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid question status.");
      return { ok: true, question };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_DOCUMENT_QUESTION_STATUS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentQuestionStatusResponse> => {
    const id = questionId(payload);
    if (!validAnalysisSender(event, arguments_) || !id) throw new Error("Invalid document question status request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const question = await backend.requestDocumentOperation("ai.question_status", { questionId: id });
      if (!isQuestionStatus(question)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid question status.");
      return { ok: true, question };
    } catch (error) { return documentFailure(error); }
  });

  const matterDocumentIds = (payload: unknown): string[] | undefined => {
    if (!isRecord(payload) || Object.keys(payload).length !== 1 || !Array.isArray(payload.documentIds)) return undefined;
    const ids = payload.documentIds;
    if (ids.length < 1 || ids.length > 20 || !ids.every((id) => typeof id === "string" && /^[a-f0-9-]{36}$/i.test(id)) || new Set(ids).size !== ids.length) return undefined;
    return [...ids];
  };
  const batchMatterDocumentIds = (payload: unknown, acknowledgement: boolean): string[] | undefined => {
    if (!isRecord(payload) || !Array.isArray(payload.documentIds)) return undefined;
    const expectedKeys = acknowledgement ? 2 : 1;
    if (Object.keys(payload).length !== expectedKeys || (acknowledgement && payload.acknowledged !== true)) return undefined;
    const ids = payload.documentIds;
    if (ids.length < 1 || ids.length > 20 || !ids.every((id) => typeof id === "string" && /^[a-f0-9-]{36}$/i.test(id)) || new Set(ids).size !== ids.length) return undefined;
    return [...ids];
  };
  const matterQuestionRequest = (payload: unknown, acknowledgement: boolean): { documentIds: string[]; question: string; followUp?: MatterQuestionFollowUp } | undefined => {
    if (!isRecord(payload) || !Array.isArray(payload.documentIds) || typeof payload.question !== "string" || !payload.question.trim() || payload.question.length > 600) return undefined;
    const allowedKeys = acknowledgement ? new Set(["documentIds", "question", "acknowledged", "followUp"]) : new Set(["documentIds", "question", "followUp"]);
    if (Object.keys(payload).some((key) => !allowedKeys.has(key))) return undefined;
    if (acknowledgement && payload.acknowledged !== true) return undefined;
    const ids = payload.documentIds;
    if (ids.length < 1 || ids.length > 20 || !ids.every((id) => typeof id === "string" && /^[a-f0-9-]{36}$/i.test(id)) || new Set(ids).size !== ids.length) return undefined;
    const documentIds = [...ids];
    const followUp = payload.followUp;
    if (followUp !== undefined && (!isRecord(followUp) || Object.keys(followUp).length !== 2 || typeof followUp.question !== "string" || typeof followUp.answer !== "string" || !followUp.question.trim() || followUp.question.length > 600 || !followUp.answer.trim() || followUp.answer.length > 1200)) return undefined;
    const validatedFollowUp = followUp === undefined ? undefined : {
      question: (followUp.question as string).trim(), answer: (followUp.answer as string).trim(),
    };
    return { documentIds, question: payload.question.trim(), ...(validatedFollowUp ? { followUp: validatedFollowUp } : {}) };
  };
  const billingPreviewRequest = (payload: unknown): { feePool: string; currency: string; rows: Array<{ id: string; label: string; eligibleAmount: string }> } | undefined => {
    if (!isRecord(payload) || Object.keys(payload).length !== 3 || typeof payload.feePool !== "string" || typeof payload.currency !== "string" || !Array.isArray(payload.rows)) return undefined;
    if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(payload.feePool) || !/^[A-Z]{3}$/.test(payload.currency) || payload.rows.length < 1 || payload.rows.length > 50) return undefined;
    const rows = payload.rows;
    if (!rows.every((row) => isRecord(row) && Object.keys(row).length === 3 && typeof row.id === "string" && typeof row.label === "string" && typeof row.eligibleAmount === "string")) return undefined;
    return { feePool: payload.feePool, currency: payload.currency, rows: rows as Array<{ id: string; label: string; eligibleAmount: string }> };
  };
  ipcMain.handle(GET_MATTER_REVIEW_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterReviewResponse> => {
    const documentIds = matterDocumentIds(payload);
    if (!validAnalysisSender(event, arguments_) || !documentIds) throw new Error("Invalid matter review request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const review = await backend.requestDocumentOperation("matter.review", { documentIds });
      if (!isMatterReview(review)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid matter review data.");
      return { ok: true, review };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(PREVIEW_MATTER_ANALYSIS_BATCH_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterAnalysisBatchPreviewResponse> => {
    const documentIds = batchMatterDocumentIds(payload, false);
    if (!validAnalysisSender(event, arguments_) || !documentIds) throw new Error("Invalid batch analysis preview request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const preview = await backend.requestDocumentOperation("matter.analysis_batch_preview", { documentIds });
      if (!isMatterAnalysisBatchPreview(preview)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid batch analysis data.");
      return { ok: true, preview };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(START_MATTER_ANALYSIS_BATCH_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterAnalysisBatchResponse> => {
    const documentIds = batchMatterDocumentIds(payload, true);
    if (!validAnalysisSender(event, arguments_) || !documentIds) throw new Error("Invalid batch analysis request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const batch = await backend.requestDocumentOperation("matter.start_analysis_batch", { documentIds, acknowledged: true });
      if (!isMatterAnalysisBatch(batch)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid batch analysis data.");
      return { ok: true, batch };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_MATTER_ANALYSIS_BATCH_STATUS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterAnalysisBatchResponse> => {
    const batchId = isRecord(payload) && Object.keys(payload).length === 1 && typeof payload.batchId === "string" && /^[a-f0-9-]{36}$/i.test(payload.batchId) ? payload.batchId : undefined;
    if (!validAnalysisSender(event, arguments_) || batchId === undefined) throw new Error("Invalid batch analysis status request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const batch = await backend.requestDocumentOperation("matter.analysis_batch_status", { batchId });
      if (!isMatterAnalysisBatch(batch)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid batch analysis status.");
      return { ok: true, batch };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(PREVIEW_MATTER_QUESTION_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterQuestionPreviewResponse> => {
    const request = matterQuestionRequest(payload, false);
    if (!validAnalysisSender(event, arguments_) || !request) throw new Error("Invalid consolidated question preview request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const preview = await backend.requestDocumentOperation("matter.question_preview", request);
      if (!isMatterQuestionPreview(preview)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid consolidated question data.");
      return { ok: true, preview };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(ASK_MATTER_QUESTION_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterQuestionResponse> => {
    const request = matterQuestionRequest(payload, true);
    if (!validAnalysisSender(event, arguments_) || !request) throw new Error("Invalid consolidated question request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const question = await backend.requestDocumentOperation("matter.ask_question", { ...request, acknowledged: true });
      if (!isMatterQuestionStatus(question)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid consolidated question status.");
      return { ok: true, question };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_MATTER_QUESTION_STATUS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<MatterQuestionResponse> => {
    const questionId = isRecord(payload) && Object.keys(payload).length === 1 && typeof payload.questionId === "string" && /^[a-f0-9-]{36}$/i.test(payload.questionId) ? payload.questionId : undefined;
    if (!validAnalysisSender(event, arguments_) || questionId === undefined) throw new Error("Invalid consolidated question status request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const question = await backend.requestDocumentOperation("matter.question_status", { questionId });
      if (!isMatterQuestionStatus(question)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid consolidated question status.");
      return { ok: true, question };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_BILLING_PREVIEW_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<BillingPreviewResponse> => {
    const request = billingPreviewRequest(payload);
    if (!validAnalysisSender(event, arguments_) || !request) throw new Error("Invalid billing preview request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const preview = await backend.requestDocumentOperation("matter.billing_preview", request);
      if (!isBillingPreview(preview)) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned invalid billing preview data.");
      return { ok: true, preview };
    } catch (error) { return documentFailure(error); }
  });

  const validateSender = (event: Electron.IpcMainInvokeEvent, arguments_: unknown[]): void => {
    if (event.senderFrame === null || !isAllowedRendererUrl(event.senderFrame.url)) throw new Error("Invalid document request.");
    if (arguments_.length > 1) throw new Error("Invalid document request.");
  };
  const importPaths = async (paths: unknown): Promise<DocumentImportResponse> => {
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      return { ok: true as const, ...(await backend.requestDocumentOperation("documents.import", { paths: await validateAuthorizedImportPaths(paths) })) } as DocumentImportResponse;
    } catch (error) { return documentFailure(error); }
  };
  const authorizeFolderDiscovery = (discovery: FolderDiscovery) => {
    const authorizationId = randomUUID();
    folderAuthorizations.set(authorizationId, {
      files: discovery.files,
      skipped: discovery.skippedFiles + discovery.skippedLinks,
      expiresAt: Date.now() + 5 * 60_000,
    });
    return {
      ok: true as const,
      preview: {
        authorizationId,
        supportedFiles: discovery.files.length,
        totalBytes: discovery.totalBytes,
        skippedFiles: discovery.skippedFiles,
        skippedLinks: discovery.skippedLinks,
        limitReached: discovery.limitReached,
      },
    };
  };
  ipcMain.handle(SELECT_AND_IMPORT_DOCUMENTS_CHANNEL, async (event, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    if (arguments_.length !== 0) throw new Error("Invalid document selection request.");
    const dialogOptions = { properties: ["openFile", "multiSelections"] as Array<"openFile" | "multiSelections">, filters: [{ name: "Supported documents", extensions: ["xlsx", "xls", "csv", "pdf"] }] };
    const owner = BrowserWindow.fromWebContents(event.sender);
    const selected = owner === null ? await dialog.showOpenDialog(dialogOptions) : await dialog.showOpenDialog(owner, dialogOptions);
    return selected.canceled ? { ok: true, cancelled: true, documents: [] } : importPaths(selected.filePaths);
  });
  ipcMain.handle(SELECT_FOLDER_FOR_IMPORT_CHANNEL, async (event, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    if (arguments_.length !== 0) throw new Error("Invalid folder selection request.");
    const owner = BrowserWindow.fromWebContents(event.sender);
    const selected = owner === null ? await dialog.showOpenDialog({ properties: ["openDirectory"] }) : await dialog.showOpenDialog(owner, { properties: ["openDirectory"] });
    if (selected.canceled) return { ok: true, cancelled: true };
    try {
      const discovery = await discoverImportableFiles(selected.filePaths[0] ?? "");
      return authorizeFolderDiscovery(discovery);
    } catch { return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The selected folder could not be read safely.")); }
  });
  const folderAuthorizationId = (payload: unknown): string | undefined => typeof payload === "object" && payload !== null && typeof (payload as { authorizationId?: unknown }).authorizationId === "string" ? (payload as { authorizationId: string }).authorizationId : undefined;
  ipcMain.handle(CANCEL_FOLDER_IMPORT_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]) => { validateSender(event, arguments_); const id = folderAuthorizationId(payload); if (id) folderAuthorizations.delete(id); return { ok: true }; });
  ipcMain.handle(CONFIRM_FOLDER_IMPORT_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    const authorizationId = folderAuthorizationId(payload);
    const authorized = authorizationId ? folderAuthorizations.get(authorizationId) : undefined;
    if (!authorizationId || !authorized || authorized.expiresAt < Date.now()) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The folder selection expired. Select the folder again."));
    folderAuthorizations.delete(authorizationId);
    const progress = { authorizationId, discovered: authorized.files.length, processed: 0, imported: 0, duplicates: 0, failed: 0, skipped: authorized.skipped };
    const failures: Array<{ filename: string; message: string }> = [];
    const emit = () => event.sender.send(FOLDER_IMPORT_PROGRESS_CHANNEL, progress);
    emit();
    for (const file of authorized.files) {
      const response = await importPaths([file.path]);
      progress.processed += 1;
      const item = response.ok && "documents" in response ? response.documents[0] : undefined;
      if (item?.ok) { if (item.duplicate) progress.duplicates += 1; else progress.imported += 1; }
      else { progress.failed += 1; if (failures.length < 20) failures.push({ filename: file.relativePath, message: item?.error?.message ?? (!response.ok ? response.error.message : "Import failed.") }); }
      emit();
    }
    return { ok: true, result: { ...progress, failures } };
  });
  ipcMain.handle(IMPORT_DROPPED_DOCUMENTS_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) throw new Error("Invalid dropped documents request.");
    const paths = (payload as { paths?: unknown }).paths;
    if (!Array.isArray(paths) || paths.length < 1 || paths.length > 20 || !paths.every((item) => typeof item === "string" && path.isAbsolute(item))) {
      throw new Error("Invalid dropped documents request.");
    }
    try {
      return authorizeFolderDiscovery(await discoverDroppedImportableFiles(paths));
    } catch {
      return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The dropped items could not be read safely."));
    }
  });
  ipcMain.handle(REMOVE_DOCUMENT_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]): Promise<DocumentRemoveResponse> => {
    validateSender(event, arguments_);
    const documentId = isRecord(payload) && Object.keys(payload).length === 1 && typeof payload.documentId === "string" && /^[a-f0-9-]{36}$/i.test(payload.documentId)
      ? payload.documentId
      : undefined;
    if (documentId === undefined) throw new Error("Invalid document removal request.");
    try {
      const backend = await ensureDevelopmentBackend();
      if (backend === undefined) return documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."));
      const removed = await backend.requestDocumentOperation("documents.remove", { documentId });
      if (!isRecord(removed) || removed.documentId !== documentId) throw new BackendProcessError("BACKEND_PROTOCOL_ERROR", "The backend returned an invalid removal result.");
      return { ok: true, documentId };
    } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(LIST_DOCUMENTS_CHANNEL, async (event, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    try { const backend = await ensureDevelopmentBackend(); return backend === undefined ? documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable.")) : { ok: true as const, ...(await backend.requestDocumentOperation("documents.list", {})) }; } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(GET_DOCUMENT_CONTENT_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    const request = typeof payload === "object" && payload !== null && !Array.isArray(payload) ? payload as { documentId?: unknown; sheetName?: unknown; rowStart?: unknown; rowLimit?: unknown; columnStart?: unknown; columnLimit?: unknown; pageNumber?: unknown } : {};
    const documentId = request.documentId;
    const sheetName = request.sheetName;
    const rowStart = request.rowStart === undefined ? 1 : request.rowStart;
    const rowLimit = request.rowLimit === undefined ? 10 : request.rowLimit;
    const columnStart = request.columnStart === undefined ? 1 : request.columnStart;
    const columnLimit = request.columnLimit === undefined ? 10 : request.columnLimit;
    const pageNumber = request.pageNumber;
    if (typeof documentId !== "string" || documentId.length > 128 || (sheetName !== undefined && (typeof sheetName !== "string" || sheetName.length === 0 || sheetName.length > 255)) || typeof rowStart !== "number" || !Number.isInteger(rowStart) || rowStart < 1 || rowStart > 1_000_000 || typeof rowLimit !== "number" || !Number.isInteger(rowLimit) || rowLimit < 1 || rowLimit > 10 || typeof columnStart !== "number" || !Number.isInteger(columnStart) || columnStart < 1 || columnStart > 16_384 || typeof columnLimit !== "number" || !Number.isInteger(columnLimit) || columnLimit < 1 || columnLimit > 10 || (pageNumber !== undefined && (typeof pageNumber !== "number" || !Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > 100_000))) throw new Error("Invalid document content request.");
    try { const backend = await ensureDevelopmentBackend(); return backend === undefined ? documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable.")) : { ok: true as const, ...(await backend.requestDocumentOperation("documents.content", { documentId, limit: 1, sheetName, rowStart, rowLimit, columnStart, columnLimit, pageNumber })) }; } catch (error) { return documentFailure(error); }
  });
  ipcMain.handle(RETRY_DOCUMENT_PARSE_CHANNEL, async (event, payload: unknown, ...arguments_: unknown[]) => {
    validateSender(event, arguments_);
    const documentId = typeof payload === "object" && payload !== null ? (payload as { documentId?: unknown }).documentId : undefined;
    if (typeof documentId !== "string" || documentId.length > 128) throw new Error("Invalid retry request.");
    try { const backend = await ensureDevelopmentBackend(); return backend === undefined ? documentFailure(new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable.")) : { ok: true as const, ...(await backend.requestDocumentOperation("documents.retry_parse", { documentId })) }; } catch (error) { return documentFailure(error); }
  });

  ipcMain.handle(
    GET_STORAGE_STATUS_CHANNEL,
    async (event, ...arguments_: unknown[]): Promise<StorageStatusResponse> => {
      const senderFrame = event.senderFrame;
      if (
        arguments_.length !== 0 ||
        senderFrame === null ||
        !isAllowedRendererUrl(senderFrame.url)
      ) {
        throw new Error("Invalid storage status request.");
      }

      if (backendProcess === undefined) {
        return createBackendHealthFailure(
          new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."),
        );
      }

      try {
        return { ok: true, storage: await backendProcess.getStorageStatus() };
      } catch (error: unknown) {
        return createBackendHealthFailure(error);
      }
    },
  );

  createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  backendProcess?.stop();
});
