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
type AIConnectionCheck = { outcome: "available" | "provider_not_configured" | "api_key_missing" | "authentication_rejected" | "permission_denied" | "rate_limited" | "network_unavailable" | "timeout" | "provider_error"; checkedAt: string; };
type AIStatus = { enabled: boolean; authorized: boolean; provider: string | null; model: string | null; configured: boolean; credentialPresent: boolean; message: string; lastConnectionCheck: AIConnectionCheck | null; };
type AIStatusResponse = { ok: true; ai: AIStatus } | { ok: false; error: { code: string; message: string } };
type AIConnectionCheckResponse = { ok: true; check: AIConnectionCheck } | { ok: false; error: { code: string; message: string } };
type DocumentAnalysisCost = { estimateMethod: string; estimatedInputTokens: number; maxOutputTokens: number; estimatedCostUsd: string | null; limitUsd: string; withinLimit: boolean; maxRequests: number; maxRequestAttempts: number; };
type DocumentAnalysisUsage = { estimatedCostUsd: string | null; inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; };
type DocumentAnalysisSummary = { text: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; };
type DocumentAnalysisJob = { jobId: string; documentId: string; extractionId: string; provider: string; model: string | null; promptVersion: string; schemaVersion: string; status: "queued" | "running" | "completed" | "failed" | "blocked"; errorCode: string | null; coverage: Record<string, unknown> | null; summary: DocumentAnalysisSummary; usage: DocumentAnalysisUsage; createdAt: string; completedAt: string | null; };
type DocumentAnalysisPreview = { document: Record<string, unknown>; provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; scope: { extractionId: string; chunks: number; sourceItems: number; totalSourceItems: number; excludedSourceItems: number; includedCharacters: number; excludedCharacters: number; truncated: boolean }; cost: DocumentAnalysisCost; latestJob: DocumentAnalysisJob | null; };
type DocumentAnalysisFinding = { id: string; proposalType: string; fieldName: string; value: unknown; unknown: boolean; confidence: number | null; validationStatus: "validated"; sourceReference: string; evidenceSnippet: string | null; modelVersion: string | null; };
type DocumentAnalysisPreviewResponse = { ok: true; preview: DocumentAnalysisPreview } | DocumentFailure;
type DocumentAnalysisStartResponse = { ok: true; job: DocumentAnalysisJob } | DocumentFailure;
type DocumentAnalysisStatusResponse = { ok: true; job: DocumentAnalysisJob } | DocumentFailure;
type DocumentAnalysisFindingsResponse = { ok: true; findings: DocumentAnalysisFinding[]; hasMore: boolean } | DocumentFailure;
type DocumentQuestionScope = { sourceItems: number; totalSourceItems: number; excludedSourceItems: number; truncated: boolean; documentsSelected?: number; documentsWithEvidence?: number; comparisonCoverageComplete?: boolean; };
type DocumentQuestionCost = { estimatedInputTokens: number; maxOutputTokens: number; estimatedCostUsd: string | null; limitUsd: string | null; withinLimit: boolean; maxRequestAttempts: number; };
type DocumentQuestionPreview = { provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; scope: DocumentQuestionScope; cost: DocumentQuestionCost; };
type DocumentQuestionCitation = { sourceReference: string; evidenceSnippet: string; };
type DocumentQuestionStatus = { questionId: string; documentId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; errorCode: string | null; model: string | null; scope: DocumentQuestionScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; };
type DocumentQuestionPreviewResponse = { ok: true; preview: DocumentQuestionPreview } | DocumentFailure;
type DocumentQuestionStartResponse = { ok: true; question: DocumentQuestionStatus } | DocumentFailure;
type DocumentQuestionStatusResponse = { ok: true; question: DocumentQuestionStatus } | DocumentFailure;
type MatterReviewDocument = { id: string; filename: string; format: string; importedAt: string; analysisAvailable: boolean; };
type MatterReviewSummary = { selectedDocuments: number; analyzedDocuments: number; documentsWithoutCompletedAnalysis: string[]; provisionalFindings: number; findingsTruncated: boolean; };
type MatterReviewFinding = { id: string; documentId: string; proposalType: string; fieldName: string; value: unknown; unknown: boolean; sourceReference: string; evidenceSnippet: string | null; };
type MatterReviewResult = { documents: MatterReviewDocument[]; summaries: Array<{ documentId: string; text: string | null; unknown: boolean; citations: DocumentQuestionCitation[] }>; findings: MatterReviewFinding[]; summary: MatterReviewSummary; };
type MatterReviewResponse = { ok: true; review: MatterReviewResult } | DocumentFailure;
type MatterAnalysisBatchPreview = { documents: Array<{ documentId: string; filename: string; ready: boolean; errorCode: string | null; scope: DocumentAnalysisPreview["scope"] | null; cost: DocumentAnalysisCost | null; }>; globalAuthorization: boolean; configured: boolean; estimatedCostUsd: string; costLimitUsd: string; withinCostLimit: boolean; ready: boolean; maxDocuments: number; maxRequestsPerDocument: number; automaticRetries: number; };
type MatterAnalysisBatch = { batchId: string; createdAt: string; status: "queued" | "running" | "completed" | "partial"; estimatedCostUsd: string | null; costLimitUsd: string; documents: Array<{ documentId: string; filename: string; status: "pending" | "running" | "completed" | "failed" | "blocked"; analysisJobId: string | null; errorCode: string | null; }>; summary: { total: number; completed: number; failed: number; pending: number; }; };
type MatterAnalysisBatchPreviewResponse = { ok: true; preview: MatterAnalysisBatchPreview } | DocumentFailure;
type MatterAnalysisBatchResponse = { ok: true; batch: MatterAnalysisBatch } | DocumentFailure;
type MatterQuestionCitation = { documentId: string; filename: string; sourceReference: string; evidenceSnippet: string; };
type MatterQuestionFollowUp = { question: string; answer: string; };
type MatterQuestionPreview = { provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; scope: DocumentQuestionScope; cost: DocumentQuestionCost; };
type MatterQuestionStatus = { questionId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: MatterQuestionCitation[]; errorCode: string | null; model: string | null; scope: DocumentQuestionScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; };
type MatterQuestionPreviewResponse = { ok: true; preview: MatterQuestionPreview } | DocumentFailure;
type MatterQuestionResponse = { ok: true; question: MatterQuestionStatus } | DocumentFailure;
type BillingPreviewRow = { id: string; label: string; eligibleAmount: string; };
type BillingPreviewResult = { feePool: string; currency: string; totalEligibleAmount: string; allocatedTotal: string; allocations: Array<BillingPreviewRow & { share: string; allocatedFee: string; currency: string }>; assumptions: string[]; };
type BillingPreviewResponse = { ok: true; preview: BillingPreviewResult } | DocumentFailure;

interface DocumentSummary { id: string; original_filename: string; file_size_bytes: number; imported_at: string; file_format: string; processing_status: "imported" | "parsed" | "unsupported" | "failed"; processing_error: string | null; ai_review_status: "new" | "reviewed"; }
type DocumentFailure = { ok: false; error: { code: string; message: string } };
type DocumentImportResponse = { ok: true; cancelled?: boolean; documents: Array<{ ok: boolean; filename?: string; document?: DocumentSummary; duplicate?: boolean; error?: { code: string; message: string } }> } | DocumentFailure;
type DocumentRemoveResponse = { ok: true; documentId: string } | DocumentFailure;
type FolderImportPreview = { authorizationId: string; supportedFiles: number; totalBytes: number; skippedFiles: number; skippedLinks: number; limitReached: boolean };
type FolderImportPreviewResponse = { ok: true; cancelled?: boolean; preview?: FolderImportPreview } | DocumentFailure;
type FolderImportProgress = { authorizationId: string; discovered: number; processed: number; imported: number; duplicates: number; failed: number; skipped: number };
type FolderImportResponse = { ok: true; result: FolderImportProgress & { failures: Array<{ filename: string; message: string }> } } | DocumentFailure;
type DocumentListResponse = { ok: true; documents: DocumentSummary[] } | DocumentFailure;
type DocumentContentOptions = { sheetName?: string; rowStart?: number; rowLimit?: number; columnStart?: number; columnLimit?: number; pageNumber?: number };
type DocumentContentResponse = { ok: true; document: DocumentSummary; parserVersion?: string; contentType?: "spreadsheet" | "pdf"; extraction?: Record<string, unknown>; worksheets?: Array<Record<string, unknown>>; grid?: Record<string, unknown>; pagination?: Record<string, unknown>; references: Array<Record<string, unknown>>; hasMore: boolean } | DocumentFailure;
type DocumentRetryResponse = { ok: true; document: DocumentSummary } | DocumentFailure;

interface DesktopAPI {
  getAppInfo(): Promise<AppInfo>;
  getBackendHealth(): Promise<BackendHealthResponse>;
  getStorageStatus(): Promise<StorageStatusResponse>;
  getAIStatus(): Promise<AIStatusResponse>;
  checkAIConnection(): Promise<AIConnectionCheckResponse>;
  setAIAuthorization(authorized: boolean): Promise<AIStatusResponse>;
  getDocumentAnalysisPreview(documentId: string): Promise<DocumentAnalysisPreviewResponse>;
  startDocumentAnalysis(documentId: string): Promise<DocumentAnalysisStartResponse>;
  getDocumentAnalysisStatus(jobId: string): Promise<DocumentAnalysisStatusResponse>;
  getDocumentAnalysisFindings(jobId: string): Promise<DocumentAnalysisFindingsResponse>;
  previewDocumentQuestion(documentId: string, question: string): Promise<DocumentQuestionPreviewResponse>;
  askDocumentQuestion(documentId: string, question: string): Promise<DocumentQuestionStartResponse>;
  getDocumentQuestionStatus(questionId: string): Promise<DocumentQuestionStatusResponse>;
  getMatterReview(documentIds: string[]): Promise<MatterReviewResponse>;
  previewMatterAnalysisBatch(documentIds: string[]): Promise<MatterAnalysisBatchPreviewResponse>;
  startMatterAnalysisBatch(documentIds: string[]): Promise<MatterAnalysisBatchResponse>;
  getMatterAnalysisBatchStatus(batchId: string): Promise<MatterAnalysisBatchResponse>;
  previewMatterQuestion(documentIds: string[], question: string, followUp?: MatterQuestionFollowUp): Promise<MatterQuestionPreviewResponse>;
  askMatterQuestion(documentIds: string[], question: string, followUp?: MatterQuestionFollowUp): Promise<MatterQuestionResponse>;
  getMatterQuestionStatus(questionId: string): Promise<MatterQuestionResponse>;
  getBillingPreview(feePool: string, currency: string, rows: BillingPreviewRow[]): Promise<BillingPreviewResponse>;
  selectAndImportDocuments(): Promise<DocumentImportResponse>;
  selectFolderForImport(): Promise<FolderImportPreviewResponse>;
  confirmFolderImport(authorizationId: string): Promise<FolderImportResponse>;
  cancelFolderImport(authorizationId: string): Promise<void>;
  onFolderImportProgress(listener: (progress: FolderImportProgress) => void): () => void;
  importDroppedDocuments(files: File[]): Promise<FolderImportPreviewResponse>;
  removeDocument(documentId: string): Promise<DocumentRemoveResponse>;
  listDocuments(): Promise<DocumentListResponse>;
  getDocumentContent(documentId: string, options?: DocumentContentOptions): Promise<DocumentContentResponse>;
  retryDocumentParsing(documentId: string): Promise<DocumentRetryResponse>;
}

interface Window {
  desktopAPI: DesktopAPI;
}
