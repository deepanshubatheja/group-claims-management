export const GET_APP_INFO_CHANNEL = "app:get-info" as const;
export const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
export const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;
export const GET_AI_STATUS_CHANNEL = "app:get-ai-status" as const;
export const CHECK_AI_CONNECTION_CHANNEL = "app:check-ai-connection" as const;
export const SET_AI_AUTHORIZATION_CHANNEL = "app:set-ai-authorization" as const;
export const SAVE_AI_API_KEY_CHANNEL = "app:save-ai-api-key" as const;
export const REMOVE_AI_API_KEY_CHANNEL = "app:remove-ai-api-key" as const;
export const GET_DOCUMENT_ANALYSIS_PREVIEW_CHANNEL = "documents:get-analysis-preview" as const;
export const START_DOCUMENT_ANALYSIS_CHANNEL = "documents:start-analysis" as const;
export const GET_DOCUMENT_ANALYSIS_STATUS_CHANNEL = "documents:get-analysis-status" as const;
export const GET_DOCUMENT_ANALYSIS_FINDINGS_CHANNEL = "documents:get-analysis-findings" as const;
export const PREVIEW_DOCUMENT_QUESTION_CHANNEL = "documents:preview-question" as const;
export const ASK_DOCUMENT_QUESTION_CHANNEL = "documents:ask-question" as const;
export const GET_DOCUMENT_QUESTION_STATUS_CHANNEL = "documents:get-question-status" as const;
export const GET_MATTER_REVIEW_CHANNEL = "matter:get-review" as const;
export const GET_BILLING_PREVIEW_CHANNEL = "matter:get-billing-preview" as const;
export const PREVIEW_MATTER_ANALYSIS_BATCH_CHANNEL = "matter:preview-analysis-batch" as const;
export const START_MATTER_ANALYSIS_BATCH_CHANNEL = "matter:start-analysis-batch" as const;
export const GET_MATTER_ANALYSIS_BATCH_STATUS_CHANNEL = "matter:get-analysis-batch-status" as const;
export const PREVIEW_MATTER_QUESTION_CHANNEL = "matter:preview-question" as const;
export const ASK_MATTER_QUESTION_CHANNEL = "matter:ask-question" as const;
export const GET_MATTER_QUESTION_STATUS_CHANNEL = "matter:get-question-status" as const;
export const SELECT_AND_IMPORT_DOCUMENTS_CHANNEL = "documents:select-and-import" as const;
export const SELECT_FOLDER_FOR_IMPORT_CHANNEL = "documents:select-folder-for-import" as const;
export const CONFIRM_FOLDER_IMPORT_CHANNEL = "documents:confirm-folder-import" as const;
export const CANCEL_FOLDER_IMPORT_CHANNEL = "documents:cancel-folder-import" as const;
export const FOLDER_IMPORT_PROGRESS_CHANNEL = "documents:folder-import-progress" as const;
export const IMPORT_DROPPED_DOCUMENTS_CHANNEL = "documents:import-dropped" as const;
export const REMOVE_DOCUMENT_CHANNEL = "documents:remove" as const;
export const LIST_DOCUMENTS_CHANNEL = "documents:list" as const;
export const GET_DOCUMENT_CONTENT_CHANNEL = "documents:get-content" as const;
export const RETRY_DOCUMENT_PARSE_CHANNEL = "documents:retry-parse" as const;

export interface DocumentSummary { id: string; original_filename: string; file_size_bytes: number; imported_at: string; file_format: string; processing_status: "imported" | "parsed" | "unsupported" | "failed"; processing_error: string | null; ai_review_status: "new" | "reviewed"; }
export interface DocumentOperationFailure { ok: false; error: { code: string; message: string } }
export interface DocumentImportItem { ok: boolean; filename?: string; document?: DocumentSummary; duplicate?: boolean; error?: { code: string; message: string } }
export type DocumentImportResponse = { ok: true; cancelled?: boolean; documents: DocumentImportItem[] } | DocumentOperationFailure;
export type DocumentRemoveResponse = { ok: true; documentId: string } | DocumentOperationFailure;
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
export interface AIConnectionCheck { outcome: "available" | "provider_not_configured" | "api_key_missing" | "authentication_rejected" | "permission_denied" | "rate_limited" | "network_unavailable" | "timeout" | "provider_error"; checkedAt: string; }
export interface AIStatus { enabled: boolean; authorized: boolean; provider: string | null; model: string | null; configured: boolean; credentialPresent: boolean; message: string; lastConnectionCheck: AIConnectionCheck | null; }
export type AIStatusResponse = { ok: true; ai: AIStatus } | BackendHealthFailure;
export type AIConnectionCheckResponse = { ok: true; check: AIConnectionCheck } | BackendHealthFailure;
/** Never includes an API-key value or fingerprint. */
export type AICredentialResponse = { ok: true; credentialPresent: boolean; message: string } | BackendHealthFailure;
export interface AnalysisScope { extractionId: string; chunks: number; sourceItems: number; totalSourceItems: number; excludedSourceItems: number; includedCharacters: number; excludedCharacters: number; truncated: boolean; }
export interface AnalysisCostPreview { estimateMethod: string; estimatedInputTokens: number; maxOutputTokens: number; estimatedCostUsd: string | null; limitUsd: string; withinLimit: boolean; maxRequests: number; maxRequestAttempts: number; }
export interface AnalysisUsage { estimatedCostUsd: string | null; inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }
export interface DocumentAnalysisSummary { text: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; }
export interface DocumentAnalysisJob { jobId: string; documentId: string; extractionId: string; provider: string; model: string | null; promptVersion: string; schemaVersion: string; status: "queued" | "running" | "completed" | "failed" | "blocked"; errorCode: string | null; coverage: Record<string, unknown> | null; summary: DocumentAnalysisSummary; usage: AnalysisUsage; createdAt: string; completedAt: string | null; }
export interface DocumentAnalysisPreview { document: Record<string, unknown>; provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; scope: AnalysisScope; cost: AnalysisCostPreview; latestJob: DocumentAnalysisJob | null; }
export interface DocumentAnalysisFinding { id: string; proposalType: string; fieldName: string; value: unknown; unknown: boolean; confidence: number | null; validationStatus: "validated"; sourceReference: string; evidenceSnippet: string | null; modelVersion: string | null; }
export type DocumentAnalysisPreviewResponse = { ok: true; preview: DocumentAnalysisPreview } | DocumentOperationFailure;
export type DocumentAnalysisStartResponse = { ok: true; job: DocumentAnalysisJob } | DocumentOperationFailure;
export type DocumentAnalysisStatusResponse = { ok: true; job: DocumentAnalysisJob } | DocumentOperationFailure;
export type DocumentAnalysisFindingsResponse = { ok: true; findings: DocumentAnalysisFinding[]; hasMore: boolean } | DocumentOperationFailure;
export interface DocumentQuestionScope { sourceItems: number; totalSourceItems: number; excludedSourceItems: number; truncated: boolean; documentsSelected?: number; documentsWithEvidence?: number; comparisonCoverageComplete?: boolean; }
export interface DocumentQuestionCost { estimatedInputTokens: number; maxOutputTokens: number; estimatedCostUsd: string | null; limitUsd: string | null; withinLimit: boolean; maxRequestAttempts: number; }
export interface DocumentQuestionPreview { provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; scope: DocumentQuestionScope; cost: DocumentQuestionCost; }
export interface DocumentQuestionCitation { sourceReference: string; evidenceSnippet: string; }
export interface DocumentQuestionFollowUp { question: string; answer: string; }
export interface DocumentQuestionStatus { questionId: string; documentId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; errorCode: string | null; model: string | null; scope: DocumentQuestionScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; }
export type DocumentQuestionPreviewResponse = { ok: true; preview: DocumentQuestionPreview } | DocumentOperationFailure;
export type DocumentQuestionStartResponse = { ok: true; question: DocumentQuestionStatus } | DocumentOperationFailure;
export type DocumentQuestionStatusResponse = { ok: true; question: DocumentQuestionStatus } | DocumentOperationFailure;

export interface MatterReviewDocument { id: string; filename: string; format: string; importedAt: string; analysisAvailable: boolean; }
export interface MatterReviewSummary { selectedDocuments: number; analyzedDocuments: number; documentsWithoutCompletedAnalysis: string[]; provisionalFindings: number; findingsTruncated: boolean; }
export interface MatterReviewFinding { id: string; documentId: string; proposalType: string; fieldName: string; value: unknown; unknown: boolean; sourceReference: string; evidenceSnippet: string | null; }
export interface MatterReviewResult { documents: MatterReviewDocument[]; summaries: Array<{ documentId: string; text: string | null; unknown: boolean; citations: DocumentQuestionCitation[] }>; findings: MatterReviewFinding[]; summary: MatterReviewSummary; }
export type MatterReviewResponse = { ok: true; review: MatterReviewResult } | DocumentOperationFailure;
export interface MatterAnalysisBatchPreviewDocument { documentId: string; filename: string; ready: boolean; errorCode: string | null; scope: AnalysisScope | null; cost: AnalysisCostPreview | null; }
export interface MatterAnalysisBatchPreview { documents: MatterAnalysisBatchPreviewDocument[]; globalAuthorization: boolean; configured: boolean; estimatedCostUsd: string; costLimitUsd: string; withinCostLimit: boolean; ready: boolean; maxDocuments: number; maxRequestsPerDocument: number; automaticRetries: number; }
export interface MatterAnalysisBatchDocument { documentId: string; filename: string; status: "pending" | "running" | "completed" | "failed" | "blocked"; analysisJobId: string | null; errorCode: string | null; }
export interface MatterAnalysisBatch { batchId: string; createdAt: string; status: "queued" | "running" | "completed" | "partial"; estimatedCostUsd: string | null; costLimitUsd: string; documents: MatterAnalysisBatchDocument[]; summary: { total: number; completed: number; failed: number; pending: number; }; }
export type MatterAnalysisBatchPreviewResponse = { ok: true; preview: MatterAnalysisBatchPreview } | DocumentOperationFailure;
export type MatterAnalysisBatchResponse = { ok: true; batch: MatterAnalysisBatch } | DocumentOperationFailure;
export interface MatterQuestionCitation { documentId: string; filename: string; sourceReference: string; evidenceSnippet: string; }
/**
 * Previous turn supplied only for an explicitly-authorized follow-up.  It is
 * transient request context, not a stored chat history or a source citation.
 */
export interface MatterQuestionFollowUp { question: string; answer: string; }
export interface MatterQuestionPreview { provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; localOnly: boolean; scope: DocumentQuestionScope; cost: DocumentQuestionCost; }
export interface MatterQuestionStatus { questionId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: MatterQuestionCitation[]; errorCode: string | null; model: string | null; scope: DocumentQuestionScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; }
export type MatterQuestionPreviewResponse = { ok: true; preview: MatterQuestionPreview } | DocumentOperationFailure;
export type MatterQuestionResponse = { ok: true; question: MatterQuestionStatus } | DocumentOperationFailure;
export interface BillingPreviewRow { id: string; label: string; eligibleAmount: string; }
export interface BillingPreviewResult { feePool: string; currency: string; totalEligibleAmount: string; allocatedTotal: string; allocations: Array<BillingPreviewRow & { share: string; allocatedFee: string; currency: string }>; assumptions: string[]; }
export type BillingPreviewResponse = { ok: true; preview: BillingPreviewResult } | DocumentOperationFailure;
