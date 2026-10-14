/// <reference types="vite/client" />

interface AppInfo {
  name: string;
  version: string;
  platform: string;
}

interface BackendHealth {
  status: "ok";
  applicationVersion: string;
  pythonVersion: string;
  platform: {
    system: string;
    release: string;
    machine: string;
  };
}

type BackendHealthResponse =
  | { ok: true; health: BackendHealth }
  | { ok: false; error: { code: string; message: string } };

interface StorageStatus {
  status: "ready" | "error";
  databaseStatus: "connected" | "unavailable";
  schemaVersion: number | null;
  directoriesAvailable: boolean;
}

type StorageStatusResponse =
  | { ok: true; storage: StorageStatus }
  | { ok: false; error: { code: string; message: string } };

interface DocumentSummary { id: string; original_filename: string; file_size_bytes: number; imported_at: string; file_format: string; processing_status: "imported" | "parsed" | "unsupported" | "failed"; processing_error: string | null; }
type DocumentFailure = { ok: false; error: { code: string; message: string } };
type DocumentImportResponse = { ok: true; cancelled?: boolean; documents: Array<{ ok: boolean; filename?: string; document?: DocumentSummary; duplicate?: boolean; error?: { code: string; message: string } }> } | DocumentFailure;
type DocumentListResponse = { ok: true; documents: DocumentSummary[] } | DocumentFailure;
type DocumentContentOptions = { sheetName?: string; rowStart?: number; rowLimit?: number; columnStart?: number; columnLimit?: number; pageNumber?: number };
type DocumentContentResponse = { ok: true; document: DocumentSummary; parserVersion?: string; contentType?: "spreadsheet" | "pdf"; extraction?: Record<string, unknown>; worksheets?: Array<Record<string, unknown>>; grid?: Record<string, unknown>; pagination?: Record<string, unknown>; references: Array<Record<string, unknown>>; hasMore: boolean } | DocumentFailure;
type DocumentRetryResponse = { ok: true; document: DocumentSummary } | DocumentFailure;

interface DesktopAPI {
  getAppInfo(): Promise<AppInfo>;
  getBackendHealth(): Promise<BackendHealthResponse>;
  getStorageStatus(): Promise<StorageStatusResponse>;
  selectAndImportDocuments(): Promise<DocumentImportResponse>;
  importDroppedDocuments(files: File[]): Promise<DocumentImportResponse>;
  listDocuments(): Promise<DocumentListResponse>;
  getDocumentContent(documentId: string, options?: DocumentContentOptions): Promise<DocumentContentResponse>;
  retryDocumentParsing(documentId: string): Promise<DocumentRetryResponse>;
}

interface Window {
  desktopAPI: DesktopAPI;
}
