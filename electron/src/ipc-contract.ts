export const GET_APP_INFO_CHANNEL = "app:get-info" as const;
/**
 * Matter Review accepts up to this many Unicode code points after outer
 * whitespace is removed. This is deliberately separate from the private
 * NDJSON byte limit and from bounded AI prompt/evidence budgets.
 */
export const MAX_MATTER_QUESTION_CHARACTERS = 5_000;

/** Match Python's ``len(str)`` for astral-plane Unicode characters. */
export function unicodeCodePointCount(value: string): number {
  return Array.from(value).length;
}

export const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
export const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;
export const GET_AI_STATUS_CHANNEL = "app:get-ai-status" as const;
export const SET_AI_PROVIDER_CHANNEL = "app:set-ai-provider" as const;
export const CHECK_AI_CONNECTION_CHANNEL = "app:check-ai-connection" as const;
export const SET_AI_AUTHORIZATION_CHANNEL = "app:set-ai-authorization" as const;
export const SAVE_AI_API_KEY_CHANNEL = "app:save-ai-api-key" as const;
export const REMOVE_AI_API_KEY_CHANNEL = "app:remove-ai-api-key" as const;
export const CONNECT_CODEX_CHANNEL = "app:connect-codex" as const;
export const DISCONNECT_CODEX_CHANNEL = "app:disconnect-codex" as const;
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
export const GET_RECONCILIATION_HOLDINGS_AS_OF_CHANNEL = "reconciliation:get-holdings-as-of" as const;
export const GET_RECONCILIATION_TIMELINE_CHANNEL = "reconciliation:get-timeline" as const;
export const GET_RECONCILIATION_TRANSFERS_CHANNEL = "reconciliation:get-transfers" as const;
export const GET_RECONCILIATION_ASSIGNMENTS_CHANNEL = "reconciliation:get-assignments" as const;
export const GET_RECONCILIATION_DISCREPANCIES_CHANNEL = "reconciliation:get-discrepancies" as const;
export const GET_RECONCILIATION_RESULT_CHANNEL = "reconciliation:get-result" as const;
export const GET_RECONCILIATION_CANDIDATES_CHANNEL = "reconciliation:get-candidates" as const;
export const CONFIRM_RECONCILIATION_CANDIDATE_CHANNEL = "reconciliation:confirm-candidate" as const;
export const REJECT_RECONCILIATION_CANDIDATE_CHANNEL = "reconciliation:reject-candidate" as const;
export const APPLY_RECONCILIATION_POLICY_CHANNEL = "reconciliation:apply-policy" as const;
export const GET_IDENTITY_QUESTIONS_CHANNEL = "reconciliation:get-identity-questions" as const;
export const RESOLVE_IDENTITY_QUESTION_CHANNEL = "reconciliation:resolve-identity-question" as const;
export const REREAD_DOCUMENTS_CHANNEL = "reconciliation:reread-documents" as const;
export const GET_BILLING_RULE_CANDIDATES_CHANNEL = "billing:get-rule-candidates" as const;
export const APPROVE_BILLING_RULE_CHANNEL = "billing:approve-rule" as const;
export const SAVE_BILLING_CALCULATION_CHANNEL = "billing:save-calculation" as const;
export const GET_BILLING_RULE_CHANNEL = "billing:get-rule" as const;
export const GET_BILLING_CALCULATION_PREVIEW_CHANNEL = "billing:get-calculation-preview" as const;
export const GET_BILLING_CALCULATION_RUNS_CHANNEL = "billing:get-calculation-runs" as const;
export const GET_BILLING_CALCULATION_RUN_CHANNEL = "billing:get-calculation-run" as const;
export const COMPARE_BILLING_CALCULATION_RUNS_CHANNEL = "billing:compare-calculation-runs" as const;
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
/** Local, read-only filters accepted by the reconciliation timeline APIs. */
export interface ReconciliationFilters { fromDate?: string; toDate?: string; investor?: string; subfund?: string; account?: string; isin?: string; }
export interface ReconciliationAsOfFilters { asOfDate: string; investor?: string; subfund?: string; account?: string; isin?: string; }
/** Structured locally-derived records; no SQLite handles or raw document bytes cross IPC. */
export interface ReconciliationReadResult { [key: string]: unknown; }
export type ReconciliationReadResponse = { ok: true; result: ReconciliationReadResult } | DocumentOperationFailure;
export type ReconciliationWriteResponse = { ok: true; result: ReconciliationReadResult } | DocumentOperationFailure;
/** Read-only persisted billing data; calculation writes stay in the Python core. */
export interface BillingCalculationRequest { billingAgreementId: string; asOfDate?: string; }
export interface BillingCalculationReadResult { [key: string]: unknown; }
export type BillingCalculationReadResponse = { ok: true; result: BillingCalculationReadResult } | DocumentOperationFailure;
export type BillingCalculationWriteResponse = { ok: true; result: BillingCalculationReadResult } | DocumentOperationFailure;

export interface AppInfo {
  name: string;
  version: string;
  platform: NodeJS.Platform;
  build: BuildMetadata;
}

export interface BuildMetadata {
  applicationVersion: string;
  buildDate: string;
  sourceRevision: string;
  sourceRevisionShort: string;
  sourceState: string;
  buildId: string;
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
  build: BuildMetadata;
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
export interface AIConnectionCheck { outcome: "available" | "provider_not_configured" | "api_key_missing" | "codex_login_required" | "authentication_rejected" | "permission_denied" | "rate_limited" | "network_unavailable" | "timeout" | "provider_error"; checkedAt: string; }
export interface AIStatus { enabled: boolean; authorized: boolean; provider: string | null; model: string | null; configured: boolean; credentialPresent: boolean; message: string; lastConnectionCheck: AIConnectionCheck | null; }
export type AIStatusResponse = { ok: true; ai: AIStatus } | BackendHealthFailure;
export type AIProviderSelectionResponse = AIStatusResponse;
export type AIConnectionCheckResponse = { ok: true; check: AIConnectionCheck } | BackendHealthFailure;
/** Never includes an API-key value or fingerprint. */
export type AICredentialResponse = { ok: true; credentialPresent: boolean; message: string } | BackendHealthFailure;
export type CodexConnectionResponse = { ok: true; message: string } | BackendHealthFailure;
export interface AnalysisScope { extractionId: string; chunks: number; sourceItems: number; totalSourceItems: number; excludedSourceItems: number; includedCharacters: number; excludedCharacters: number; truncated: boolean; }
export interface AnalysisCostPreview { estimateMethod: string; estimatedInputTokens: number; maxOutputTokens: number; estimatedCostUsd: string | null; limitUsd: string | null; withinLimit: boolean; maxRequests: number; maxRequestAttempts: number; }
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
export interface MatterAnalysisBatchPreview { documents: MatterAnalysisBatchPreviewDocument[]; globalAuthorization: boolean; configured: boolean; estimatedCostUsd: string | null; costLimitUsd: string | null; withinCostLimit: boolean; ready: boolean; maxDocuments: number; maxRequestsPerDocument: number; automaticRetries: number; }
export interface MatterAnalysisBatchDocument { documentId: string; filename: string; status: "pending" | "running" | "completed" | "failed" | "blocked"; analysisJobId: string | null; errorCode: string | null; }
export interface MatterAnalysisBatch { batchId: string; createdAt: string; status: "queued" | "running" | "completed" | "partial"; estimatedCostUsd: string | null; costLimitUsd: string | null; documents: MatterAnalysisBatchDocument[]; summary: { total: number; completed: number; failed: number; pending: number; }; }
export type MatterAnalysisBatchPreviewResponse = { ok: true; preview: MatterAnalysisBatchPreview } | DocumentOperationFailure;
export type MatterAnalysisBatchResponse = { ok: true; batch: MatterAnalysisBatch } | DocumentOperationFailure;
export interface MatterQuestionCitation {
  documentId: string;
  filename: string;
  sourceReference: string;
  evidenceSnippet: string;
  /** Present only when ClaimFlow selected the cited evidence after no provider finding was available. */
  origin?: "local_fallback";
}
/** ClaimFlow's single local workspace is one implicit matter. */
export const WORKSPACE_MATTER_ID = "workspace" as const;
/** Every matter question names its scope explicitly; selected mode never widens to all documents. */
export type MatterQuestionScopeRequest = { mode: "all" } | { mode: "selected"; documentIds: string[] };
/**
 * A follow-up names an earlier backend-held turn.  The renderer never supplies
 * the previous question or answer text; Python checks that turn's source
 * documents remain inside the new scope before reusing it as context.
 */
export interface MatterQuestionRequest { matterId: string; scope: MatterQuestionScopeRequest; question: string; followUpQuestionId?: string; }
/** The documents the user agreed may have bounded excerpts sent for this question. */
export interface MatterQuestionSubmission extends MatterQuestionRequest { consentedDocumentIds: string[]; }
export type MatterUnavailableReason = "not_parsed" | "extraction_failed" | "unsupported" | "extraction_unavailable" | "no_searchable_text";
export interface MatterDocumentScope {
  matterId: string;
  mode: "all" | "selected";
  documentsInScope: number;
  searchableDocuments: number;
  unavailableDocuments: Array<{ documentId: string; filename: string; reason: MatterUnavailableReason }>;
  incompleteDocuments: Array<{ documentId: string; filename: string }>;
  documentsWithEvidence: number;
  transmittedDocuments: Array<{ documentId: string; filename: string; previouslyAuthorized: boolean }>;
  searchTruncated: boolean;
  /** Documents the question itself named (by file name or file-name date); they only narrow the scope. */
  resolvedDocuments: Array<{ documentId: string; filename: string; matchedBy: string }>;
  notes: string[];
}
export type MatterQuestionIntent = "inventory" | "relationship" | "specific_lookup" | "holdings_comparison" | "historical_reconstruction" | "identity_resolution" | "holdings_conflict" | "change" | "legal_status" | "comparison" | "calculation" | "general";
export interface MatterRelationship { documentId: string; filename: string; subject: string; subjectRole: string; subjectReference: string; related: string; relatedRole: string; relatedReference: string; kind: "explicit_statement" | "explicit_heading" | "document_section" | "same_row"; basis: string; }
export interface MatterHoldingSide { documentId: string; filename: string; sheet: string; row: number; rowReference: string | null; quantity: string | null; quantityReference: string | null; isinReference: string | null; accountReference: string | null; entityReferences: string[]; unit: string | null; unitReference: string | null; currency: string | null; currencyReference: string | null; rowDate: string | null; rowDateReference: string | null; cells: Array<{ heading: string; value: string; reference: string }>; }
export interface MatterHoldingRow { entities: Array<{ kind: string; name: string }>; account: string | null; isin: string | null; currency: string | null; earlier: MatterHoldingSide | null; later: MatterHoldingSide | null; difference: string | null; status: "compared" | "only_earlier" | "only_later" | "duplicate_rows" | "unusable_quantity" | "unit_mismatch" | "currency_mismatch"; note: string | null; }
/** One holdings table: header, records, investor label, stated date and control total, by cell. */
export interface MatterHoldingsTable { sheet: string; headerReference: string; recordReference: string | null; quantityHeading: string; quantityHeadingReference: string; quantityReference: string | null; investor: { name: string; reference: string; label: string; labelReference: string } | null; statedDate: { date: string | null; text: string; reference: string; labelReference: string } | null; controlTotal: { reference: string; label: string; labelReference: string; value: string | null; formula: string | null; recordsTotal: string | null; status: "formula_covers_records" | "matches" | "differs" | "not_verified"; note: string; recordRows?: number; comparisonRows?: number; comparisonTotal?: string | null; reconciled?: boolean } | null; }
/** An entity the question named, and how it was matched to a name in the spreadsheets. */
export interface MatterRequestedEntity { text: string; name: string | null; match: "exact" | "alias" | "ambiguous" | "not_found"; candidates: string[]; positions: number; }
/** A local, exact (Decimal) comparison of two dated holdings spreadsheets. */
export interface MatterHoldingsComparison { documents: Array<{ documentId: string; filename: string; date: string | null; dateBasis: string | null; tables: MatterHoldingsTable[] }>; orderBasis: string; rows: MatterHoldingRow[]; totals: { earlier: string | null; later: string | null; difference: string | null; currency: string | null; coverage?: "all_records" | "selected_rows"; note: string | null }; requestedEntities?: MatterRequestedEntity[]; issues: string[]; caveats: Array<{ text: string; sources: Array<{ documentId: string; filename: string; reference: string }> }>; legalQuestionNotAnswered: boolean; }
export interface MatterInventoryEvidence { documentId: string; filename: string; sourceReference: string; evidenceSnippet: string; role: string; basis: string; status: "document_label" | "provisional_ai_finding"; }
export interface MatterInventoryEntity { name: string; roles: string[]; related: Array<{ name: string; role: string }>; evidence: MatterInventoryEvidence[]; documentCount: number; mentionCount: number; uncertainty: string | null; }
/** A locally built, evidence-linked list of explicitly labelled entities; roles remain provisional. */
export interface MatterEntityInventory {
  requestedRoles: string[];
  entities: MatterInventoryEntity[];
  otherRoleCounts: Record<string, number>;
  coverage: { eligibleDocuments: number; searchedDocuments: number; documentsWithRoleLabels: number; documentsWithListedEntities: number; partial: boolean; partialDocuments: Array<{ documentId: string; filename: string }>; entitiesOmitted: number; };
  relationships: MatterRelationship[];
}
export interface MatterQuestionPreview { intent: MatterQuestionIntent; provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; localOnly: boolean; scope: DocumentQuestionScope; documentScope: MatterDocumentScope; cost: DocumentQuestionCost; }
/** Non-sensitive execution state for a user-initiated Matter Review question. */
export interface MatterQuestionExecution {
  timestamp: string;
  provider: string | null;
  /** A provider routing label, not necessarily a verified underlying model. */
  model: string | null;
  providerCallAttempted: boolean;
  providerOutcome: "queued" | "attempting" | "completed" | "provider_failure" | "local_only";
  fallbackUsed: boolean;
  fallbackReason: string | null;
  documentsSelected: string[];
  documentsIncluded: string[];
  documentsExcluded: string[];
  evidenceTruncated: boolean;
  requirements: Record<string, boolean>;
  modelUnknown: boolean | null;
  responseStatus: "complete" | "partial" | "no_supported_findings" | "fallback" | "local" | null;
  validatedSectionKinds: string[];
  validatedSectionCounts: Record<string, number>;
  validatedSectionStatusCounts: Record<string, number>;
  validatedCitationCount: number;
  evidenceItemCounts: Record<string, { available: number; included: number; excluded: number }>;
}
export interface MatterQuestionStatus { questionId: string; matterId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: MatterQuestionCitation[]; errorCode: string | null; model: string | null; intent: MatterQuestionIntent; inventory: MatterEntityInventory | null; holdingsComparison: MatterHoldingsComparison | null; scope: DocumentQuestionScope; documentScope: MatterDocumentScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; execution: MatterQuestionExecution; }
export type MatterQuestionPreviewResponse = { ok: true; preview: MatterQuestionPreview } | DocumentOperationFailure;
export type MatterQuestionResponse = { ok: true; question: MatterQuestionStatus } | DocumentOperationFailure;
export interface BillingPreviewRow { id: string; label: string; eligibleAmount: string; }
export interface BillingPreviewResult { feePool: string; currency: string; totalEligibleAmount: string; allocatedTotal: string; allocations: Array<BillingPreviewRow & { share: string; allocatedFee: string; currency: string }>; assumptions: string[]; }
export type BillingPreviewResponse = { ok: true; preview: BillingPreviewResult } | DocumentOperationFailure;
