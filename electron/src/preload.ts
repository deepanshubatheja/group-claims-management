import { contextBridge, ipcRenderer } from "electron";
import type { AppInfo, BackendHealthResponse, StorageStatusResponse, DocumentContentResponse, DocumentImportResponse, DocumentListResponse, DocumentRetryResponse, DocumentContentOptions } from "./ipc-contract";

// Sandboxed preloads cannot load arbitrary local CommonJS modules at runtime.
// Keep this one allowlisted channel local while sharing the response type.
const GET_APP_INFO_CHANNEL = "app:get-info" as const;
const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;
const SELECT_AND_IMPORT_DOCUMENTS_CHANNEL = "documents:select-and-import" as const;
const IMPORT_DROPPED_DOCUMENTS_CHANNEL = "documents:import-dropped" as const;
const LIST_DOCUMENTS_CHANNEL = "documents:list" as const;
const GET_DOCUMENT_CONTENT_CHANNEL = "documents:get-content" as const;
const RETRY_DOCUMENT_PARSE_CHANNEL = "documents:retry-parse" as const;

function isDocumentResponse(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return candidate.ok === true || (candidate.ok === false && typeof candidate.error === "object" && candidate.error !== null);
}

function isDocumentContentResponse(value: unknown): value is DocumentContentResponse {
  if (!isDocumentResponse(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (candidate.ok === false) return true;
  if (!Array.isArray(candidate.references) || typeof candidate.document !== "object" || candidate.document === null) {
    return false;
  }
  if (candidate.contentType !== undefined && candidate.contentType !== "spreadsheet" && candidate.contentType !== "pdf") {
    return false;
  }
  if (candidate.contentType === "spreadsheet" && (typeof candidate.grid !== "object" || candidate.grid === null || !Array.isArray(candidate.worksheets))) {
    return false;
  }
  if (candidate.contentType === "pdf" && (typeof candidate.pagination !== "object" || candidate.pagination === null)) {
    return false;
  }
  return true;
}

function isAppInfo(value: unknown): value is AppInfo {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;

  return (
    typeof candidate.name === "string" &&
    candidate.name.length > 0 &&
    typeof candidate.version === "string" &&
    candidate.version.length > 0 &&
    (candidate.platform === "darwin" ||
      candidate.platform === "win32" ||
      candidate.platform === "linux")
  );
}

function isBackendHealthResponse(value: unknown): value is BackendHealthResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  if (candidate.ok === false) {
    return (
      typeof candidate.error === "object" &&
      candidate.error !== null &&
      typeof (candidate.error as Record<string, unknown>).code === "string" &&
      typeof (candidate.error as Record<string, unknown>).message === "string"
    );
  }

  if (candidate.ok !== true || typeof candidate.health !== "object" || candidate.health === null) {
    return false;
  }

  const health = candidate.health as Record<string, unknown>;
  const platform = health.platform;
  return (
    health.status === "ok" &&
    typeof health.applicationVersion === "string" &&
    health.applicationVersion.length > 0 &&
    typeof health.pythonVersion === "string" &&
    health.pythonVersion.length > 0 &&
    typeof platform === "object" &&
    platform !== null &&
    typeof (platform as Record<string, unknown>).system === "string" &&
    typeof (platform as Record<string, unknown>).release === "string" &&
    typeof (platform as Record<string, unknown>).machine === "string"
  );
}

function isStorageStatusResponse(value: unknown): value is StorageStatusResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  if (candidate.ok === false) {
    return (
      typeof candidate.error === "object" &&
      candidate.error !== null &&
      typeof (candidate.error as Record<string, unknown>).code === "string" &&
      typeof (candidate.error as Record<string, unknown>).message === "string"
    );
  }

  if (candidate.ok !== true || typeof candidate.storage !== "object" || candidate.storage === null) {
    return false;
  }

  const storage = candidate.storage as Record<string, unknown>;
  return (
    (storage.status === "ready" || storage.status === "error") &&
    (storage.databaseStatus === "connected" || storage.databaseStatus === "unavailable") &&
    (storage.schemaVersion === null ||
      (typeof storage.schemaVersion === "number" &&
        Number.isInteger(storage.schemaVersion) &&
        storage.schemaVersion >= 0)) &&
    typeof storage.directoriesAvailable === "boolean"
  );
}

const desktopApi = Object.freeze({
  async getAppInfo(): Promise<AppInfo> {
    const response: unknown = await ipcRenderer.invoke(GET_APP_INFO_CHANNEL);

    if (!isAppInfo(response)) {
      throw new Error("The desktop application returned invalid application information.");
    }

    return Object.freeze({ ...response });
  },
  async getBackendHealth(): Promise<BackendHealthResponse> {
    const response: unknown = await ipcRenderer.invoke(GET_BACKEND_HEALTH_CHANNEL);

    if (!isBackendHealthResponse(response)) {
      throw new Error("The desktop application returned invalid backend health information.");
    }

    return Object.freeze(response);
  },
  async getStorageStatus(): Promise<StorageStatusResponse> {
    const response: unknown = await ipcRenderer.invoke(GET_STORAGE_STATUS_CHANNEL);

    if (!isStorageStatusResponse(response)) {
      throw new Error("The desktop application returned invalid storage status information.");
    }

    return Object.freeze(response);
  },
  async selectAndImportDocuments(): Promise<DocumentImportResponse> {
    const response: unknown = await ipcRenderer.invoke(SELECT_AND_IMPORT_DOCUMENTS_CHANNEL);
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid document response.");
    return Object.freeze(response) as DocumentImportResponse;
  },
  async importDroppedDocuments(files: File[]): Promise<DocumentImportResponse> {
    // A DOM File from an Electron drop has an OS path only inside this privileged preload.
    // The path is immediately sent to the allowlisted main-process channel and never returned.
    const paths = files.map((file) => (file as File & { path?: string }).path).filter((path): path is string => typeof path === "string" && path.length > 0);
    if (paths.length !== files.length || paths.length === 0 || paths.length > 20) throw new Error("Drop valid local files only.");
    const response: unknown = await ipcRenderer.invoke(IMPORT_DROPPED_DOCUMENTS_CHANNEL, { paths });
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid document response.");
    return Object.freeze(response) as DocumentImportResponse;
  },
  async listDocuments(): Promise<DocumentListResponse> {
    const response: unknown = await ipcRenderer.invoke(LIST_DOCUMENTS_CHANNEL);
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid document response.");
    return Object.freeze(response) as DocumentListResponse;
  },
  async getDocumentContent(documentId: string, options: DocumentContentOptions = {}): Promise<DocumentContentResponse> {
    const rowStart = options.rowStart ?? 1;
    const rowLimit = options.rowLimit ?? 10;
    const columnStart = options.columnStart ?? 1;
    const columnLimit = options.columnLimit ?? 10;
    if (!Number.isInteger(rowStart) || rowStart < 1 || rowStart > 1_000_000 || !Number.isInteger(rowLimit) || rowLimit < 1 || rowLimit > 10 || !Number.isInteger(columnStart) || columnStart < 1 || columnStart > 16_384 || !Number.isInteger(columnLimit) || columnLimit < 1 || columnLimit > 10 || (options.pageNumber !== undefined && (!Number.isInteger(options.pageNumber) || options.pageNumber < 1 || options.pageNumber > 100_000)) || (options.sheetName !== undefined && (!options.sheetName || options.sheetName.length > 255))) throw new Error("Invalid document preview request.");
    const response: unknown = await ipcRenderer.invoke(GET_DOCUMENT_CONTENT_CHANNEL, { documentId, sheetName: options.sheetName, rowStart, rowLimit, columnStart, columnLimit, pageNumber: options.pageNumber });
    if (!isDocumentContentResponse(response)) throw new Error("The desktop application returned an invalid document preview response.");
    return Object.freeze(response) as DocumentContentResponse;
  },
  async retryDocumentParsing(documentId: string): Promise<DocumentRetryResponse> {
    const response: unknown = await ipcRenderer.invoke(RETRY_DOCUMENT_PARSE_CHANNEL, { documentId });
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid document response.");
    return Object.freeze(response) as DocumentRetryResponse;
  },
});

contextBridge.exposeInMainWorld("desktopAPI", desktopApi);
