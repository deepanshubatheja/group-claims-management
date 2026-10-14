export const GET_APP_INFO_CHANNEL = "app:get-info" as const;
export const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
export const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;
export const SELECT_AND_IMPORT_DOCUMENTS_CHANNEL = "documents:select-and-import" as const;
export const SELECT_FOLDER_FOR_IMPORT_CHANNEL = "documents:select-folder-for-import" as const;
export const CONFIRM_FOLDER_IMPORT_CHANNEL = "documents:confirm-folder-import" as const;
export const CANCEL_FOLDER_IMPORT_CHANNEL = "documents:cancel-folder-import" as const;
export const FOLDER_IMPORT_PROGRESS_CHANNEL = "documents:folder-import-progress" as const;
export const IMPORT_DROPPED_DOCUMENTS_CHANNEL = "documents:import-dropped" as const;
export const LIST_DOCUMENTS_CHANNEL = "documents:list" as const;
export const GET_DOCUMENT_CONTENT_CHANNEL = "documents:get-content" as const;
export const RETRY_DOCUMENT_PARSE_CHANNEL = "documents:retry-parse" as const;

export interface DocumentSummary { id: string; original_filename: string; file_size_bytes: number; imported_at: string; file_format: string; processing_status: "imported" | "parsed" | "unsupported" | "failed"; processing_error: string | null; }
export interface DocumentOperationFailure { ok: false; error: { code: string; message: string } }
export interface DocumentImportItem { ok: boolean; filename?: string; document?: DocumentSummary; duplicate?: boolean; error?: { code: string; message: string } }
export type DocumentImportResponse = { ok: true; cancelled?: boolean; documents: DocumentImportItem[] } | DocumentOperationFailure;
export interface FolderImportPreview { authorizationId: string; supportedFiles: number; totalBytes: number; skippedFiles: number; skippedLinks: number; limitReached: boolean; }
export type FolderImportPreviewResponse = { ok: true; cancelled?: boolean; preview?: FolderImportPreview } | DocumentOperationFailure;
export interface FolderImportProgress { authorizationId: string; discovered: number; processed: number; imported: number; duplicates: number; failed: number; skipped: number; }
export interface FolderImportResult extends FolderImportProgress { failures: Array<{ filename: string; message: string }>; }
export type FolderImportResponse = { ok: true; result: FolderImportResult } | DocumentOperationFailure;
export type DocumentListResponse = { ok: true; documents: DocumentSummary[] } | DocumentOperationFailure;
export type DocumentContentResponse = { ok: true; document: DocumentSummary; parserVersion?: string; contentType?: "spreadsheet" | "pdf"; extraction?: Record<string, unknown>; worksheets?: Array<Record<string, unknown>>; grid?: Record<string, unknown>; pagination?: Record<string, unknown>; references: Array<Record<string, unknown>>; hasMore: boolean } | DocumentOperationFailure;
export interface DocumentContentOptions { sheetName?: string; rowStart?: number; rowLimit?: number; columnStart?: number; columnLimit?: number; pageNumber?: number; }
export type DocumentRetryResponse = { ok: true; document: DocumentSummary } | DocumentOperationFailure;

export interface AppInfo {
  name: string;
  version: string;
  platform: NodeJS.Platform;
}

export interface BackendHealth {
  status: "ok";
  applicationVersion: string;
  pythonVersion: string;
  platform: {
    system: string;
    release: string;
    machine: string;
  };
}

export interface BackendHealthFailure {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

export interface BackendHealthSuccess {
  ok: true;
  health: BackendHealth;
}

export type BackendHealthResponse = BackendHealthSuccess | BackendHealthFailure;

export interface StorageStatus {
  status: "ready" | "error";
  databaseStatus: "connected" | "unavailable";
  schemaVersion: number | null;
  directoriesAvailable: boolean;
}

export interface StorageStatusFailure {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

export interface StorageStatusSuccess {
  ok: true;
  storage: StorageStatus;
}

export type StorageStatusResponse = StorageStatusSuccess | StorageStatusFailure;
