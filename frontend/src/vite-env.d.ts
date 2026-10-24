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
type AIConnectionCheck = { outcome: "available" | "provider_not_configured" | "api_key_missing" | "codex_login_required" | "authentication_rejected" | "permission_denied" | "rate_limited" | "network_unavailable" | "timeout" | "provider_error"; checkedAt: string; };
type AIStatus = { enabled: boolean; authorized: boolean; provider: string | null; model: string | null; configured: boolean; credentialPresent: boolean; message: string; lastConnectionCheck: AIConnectionCheck | null; };
type AIStatusResponse = { ok: true; ai: AIStatus } | { ok: false; error: { code: string; message: string } };
type AIConnectionCheckResponse = { ok: true; check: AIConnectionCheck } | { ok: false; error: { code: string; message: string } };
type AICredentialResponse = { ok: true; credentialPresent: boolean; message: string } | { ok: false; error: { code: string; message: string } };
type CodexConnectionResponse = { ok: true; message: string } | { ok: false; error: { code: string; message: string } };
type DocumentAnalysisCost = { estimateMethod: string; estimatedInputTokens: number; maxOutputTokens: number; estimatedCostUsd: string | null; limitUsd: string | null; withinLimit: boolean; maxRequests: number; maxRequestAttempts: number; };
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
type DocumentQuestionFollowUp = { question: string; answer: string; };
type DocumentQuestionStatus = { questionId: string; documentId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; errorCode: string | null; model: string | null; scope: DocumentQuestionScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; };
type DocumentQuestionPreviewResponse = { ok: true; preview: DocumentQuestionPreview } | DocumentFailure;
type DocumentQuestionStartResponse = { ok: true; question: DocumentQuestionStatus } | DocumentFailure;
type DocumentQuestionStatusResponse = { ok: true; question: DocumentQuestionStatus } | DocumentFailure;
type MatterReviewDocument = { id: string; filename: string; format: string; importedAt: string; analysisAvailable: boolean; };
type MatterReviewSummary = { selectedDocuments: number; analyzedDocuments: number; documentsWithoutCompletedAnalysis: string[]; provisionalFindings: number; findingsTruncated: boolean; };
type MatterReviewFinding = { id: string; documentId: string; proposalType: string; fieldName: string; value: unknown; unknown: boolean; sourceReference: string; evidenceSnippet: string | null; };
type MatterReviewResult = { documents: MatterReviewDocument[]; summaries: Array<{ documentId: string; text: string | null; unknown: boolean; citations: DocumentQuestionCitation[] }>; findings: MatterReviewFinding[]; summary: MatterReviewSummary; };
type MatterReviewResponse = { ok: true; review: MatterReviewResult } | DocumentFailure;
type MatterAnalysisBatchPreview = { documents: Array<{ documentId: string; filename: string; ready: boolean; errorCode: string | null; scope: DocumentAnalysisPreview["scope"] | null; cost: DocumentAnalysisCost | null; }>; globalAuthorization: boolean; configured: boolean; estimatedCostUsd: string | null; costLimitUsd: string | null; withinCostLimit: boolean; ready: boolean; maxDocuments: number; maxRequestsPerDocument: number; automaticRetries: number; };
type MatterAnalysisBatch = { batchId: string; createdAt: string; status: "queued" | "running" | "completed" | "partial"; estimatedCostUsd: string | null; costLimitUsd: string | null; documents: Array<{ documentId: string; filename: string; status: "pending" | "running" | "completed" | "failed" | "blocked"; analysisJobId: string | null; errorCode: string | null; }>; summary: { total: number; completed: number; failed: number; pending: number; }; };
type MatterAnalysisBatchPreviewResponse = { ok: true; preview: MatterAnalysisBatchPreview } | DocumentFailure;
type MatterAnalysisBatchResponse = { ok: true; batch: MatterAnalysisBatch } | DocumentFailure;
type MatterQuestionCitation = { documentId: string; filename: string; sourceReference: string; evidenceSnippet: string; };
type MatterQuestionScopeRequest = { mode: "all" } | { mode: "selected"; documentIds: string[] };
type MatterQuestionRequest = { matterId: string; scope: MatterQuestionScopeRequest; question: string; followUpQuestionId?: string; };
type MatterQuestionSubmission = MatterQuestionRequest & { consentedDocumentIds: string[]; };
type MatterUnavailableReason = "not_parsed" | "extraction_failed" | "unsupported" | "extraction_unavailable" | "no_searchable_text";
type MatterDocumentScope = { matterId: string; mode: "all" | "selected"; documentsInScope: number; searchableDocuments: number; unavailableDocuments: Array<{ documentId: string; filename: string; reason: MatterUnavailableReason }>; incompleteDocuments: Array<{ documentId: string; filename: string }>; documentsWithEvidence: number; transmittedDocuments: Array<{ documentId: string; filename: string; previouslyAuthorized: boolean }>; searchTruncated: boolean; resolvedDocuments: Array<{ documentId: string; filename: string; matchedBy: string }>; notes: string[]; };
type MatterQuestionIntent = "inventory" | "relationship" | "specific_lookup" | "holdings_comparison" | "change" | "legal_status" | "comparison" | "calculation" | "general";
type MatterRelationship = { documentId: string; filename: string; subject: string; subjectRole: string; subjectReference: string; related: string; relatedRole: string; relatedReference: string; kind: "explicit_statement" | "explicit_heading" | "document_section" | "same_row"; basis: string; };
type MatterHoldingSide = { documentId: string; filename: string; sheet: string; row: number; rowReference: string | null; quantity: string | null; quantityReference: string | null; isinReference: string | null; accountReference: string | null; entityReferences: string[]; unit: string | null; unitReference: string | null; currency: string | null; currencyReference: string | null; rowDate: string | null; rowDateReference: string | null; cells: Array<{ heading: string; value: string; reference: string }>; };
type MatterHoldingRow = { entities: Array<{ kind: string; name: string }>; account: string | null; isin: string | null; currency: string | null; earlier: MatterHoldingSide | null; later: MatterHoldingSide | null; difference: string | null; status: "compared" | "only_earlier" | "only_later" | "duplicate_rows" | "unusable_quantity" | "unit_mismatch" | "currency_mismatch"; note: string | null; };
type MatterHoldingsTable = { sheet: string; headerReference: string; recordReference: string | null; quantityHeading: string; quantityHeadingReference: string; quantityReference: string | null; investor: { name: string; reference: string; label: string; labelReference: string } | null; statedDate: { date: string | null; text: string; reference: string; labelReference: string } | null; controlTotal: { reference: string; label: string; labelReference: string; value: string | null; formula: string | null; recordsTotal: string | null; status: "formula_covers_records" | "matches" | "differs" | "not_verified"; note: string; recordRows?: number; comparisonRows?: number; comparisonTotal?: string | null; reconciled?: boolean } | null; };
type MatterRequestedEntity = { text: string; name: string | null; match: "exact" | "alias" | "ambiguous" | "not_found"; candidates: string[]; positions: number; };
type MatterHoldingsComparison = { documents: Array<{ documentId: string; filename: string; date: string | null; dateBasis: string | null; tables: MatterHoldingsTable[] }>; orderBasis: string; rows: MatterHoldingRow[]; totals: { earlier: string | null; later: string | null; difference: string | null; currency: string | null; coverage?: "all_records" | "selected_rows"; note: string | null }; requestedEntities?: MatterRequestedEntity[]; issues: string[]; caveats: Array<{ text: string; sources: Array<{ documentId: string; filename: string; reference: string }> }>; legalQuestionNotAnswered: boolean; };
type MatterBillingSource = { documentId: string; filename: string; reference: string; };
type MatterBillingParticipant = { name: string; identifier: string | null; status: "eligible" | "excluded" | "undetermined"; eligibleAmount: string | null; share: string | null; sharePercent: string | null; allocatedAmount: string | null; unroundedAmount: string | null; roundingAdjustment: string | null; reason: string | null; source: MatterBillingSource | null; };
type MatterBillingCrossCheck = { participant: string; identifier: string | null; status: "consistent" | "conflict"; instructionAmount: string; recordedAmount: string; asOfDate: string | null; source: MatterBillingSource; };
type MatterBillingAllocation = {
  calculationStatus: "complete" | "incomplete";
  resultType: "provisional_allocation";
  invoiceStatus: "not_issued";
  issuanceBlocked: boolean;
  instruction: { documentId: string; filename: string } | null;
  approvedBy: { text: string; source: MatterBillingSource | null } | null;
  investorGroup: { text: string; source: MatterBillingSource | null } | null;
  period: { text: string; source: MatterBillingSource | null } | null;
  currency: string | null;
  feePool: { amount: string; source: MatterBillingSource | null } | null;
  rule: { text: string; formula: string; source: MatterBillingSource | null } | null;
  roundingPolicy: string;
  totals: { eligibleAmount: string; allocatedAmount: string; remainder: string; sharePercent: string } | null;
  participants: MatterBillingParticipant[];
  crossChecks: MatterBillingCrossCheck[];
  crossCheck: { comparisonDate: string | null; dateBasis: string; documentsChecked: number; limitations: string[] };
  missing: string[];
  issues: string[];
};
type MatterInventoryEvidence = { documentId: string; filename: string; sourceReference: string; evidenceSnippet: string; role: string; basis: string; status: "document_label" | "provisional_ai_finding"; };
type MatterInventoryEntity = { name: string; roles: string[]; related: Array<{ name: string; role: string }>; evidence: MatterInventoryEvidence[]; documentCount: number; mentionCount: number; uncertainty: string | null; };
type MatterEntityInventory = { requestedRoles: string[]; entities: MatterInventoryEntity[]; otherRoleCounts: Record<string, number>; coverage: { eligibleDocuments: number; searchedDocuments: number; documentsWithRoleLabels: number; documentsWithListedEntities: number; partial: boolean; partialDocuments: Array<{ documentId: string; filename: string }>; entitiesOmitted: number; }; relationships: MatterRelationship[]; };
type MatterQuestionPreview = { intent: MatterQuestionIntent; provider: string | null; model: string | null; globalAuthorization: boolean; configured: boolean; localOnly: boolean; scope: DocumentQuestionScope; documentScope: MatterDocumentScope; cost: DocumentQuestionCost; };
type MatterQuestionStatus = { questionId: string; matterId: string; status: "queued" | "running" | "completed" | "failed"; answer: string | null; unknown: boolean; citations: MatterQuestionCitation[]; errorCode: string | null; model: string | null; intent: MatterQuestionIntent; inventory: MatterEntityInventory | null; holdingsComparison: MatterHoldingsComparison | null; billingAllocation?: MatterBillingAllocation | null; scope: DocumentQuestionScope; documentScope: MatterDocumentScope; usage: { inputTokens: number | null; outputTokens: number | null; totalTokens: number | null; actualCostEstimateUsd: string | null; }; };
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
  setAIProvider(provider: "codex" | "openai"): Promise<AIStatusResponse>;
  connectCodex(): Promise<CodexConnectionResponse>;
  disconnectCodex(): Promise<CodexConnectionResponse>;
  checkAIConnection(): Promise<AIConnectionCheckResponse>;
  setAIAuthorization(authorized: boolean): Promise<AIStatusResponse>;
  saveAIApiKey(apiKey: string): Promise<AICredentialResponse>;
  removeAIApiKey(): Promise<AICredentialResponse>;
  getDocumentAnalysisPreview(documentId: string): Promise<DocumentAnalysisPreviewResponse>;
  startDocumentAnalysis(documentId: string): Promise<DocumentAnalysisStartResponse>;
  getDocumentAnalysisStatus(jobId: string): Promise<DocumentAnalysisStatusResponse>;
  getDocumentAnalysisFindings(jobId: string): Promise<DocumentAnalysisFindingsResponse>;
  previewDocumentQuestion(documentId: string, question: string, followUp?: DocumentQuestionFollowUp): Promise<DocumentQuestionPreviewResponse>;
  askDocumentQuestion(documentId: string, question: string, followUp?: DocumentQuestionFollowUp): Promise<DocumentQuestionStartResponse>;
  getDocumentQuestionStatus(questionId: string): Promise<DocumentQuestionStatusResponse>;
  getMatterReview(documentIds: string[]): Promise<MatterReviewResponse>;
  previewMatterAnalysisBatch(documentIds: string[]): Promise<MatterAnalysisBatchPreviewResponse>;
  startMatterAnalysisBatch(documentIds: string[]): Promise<MatterAnalysisBatchResponse>;
  getMatterAnalysisBatchStatus(batchId: string): Promise<MatterAnalysisBatchResponse>;
  previewMatterQuestion(request: MatterQuestionRequest): Promise<MatterQuestionPreviewResponse>;
  askMatterQuestion(submission: MatterQuestionSubmission): Promise<MatterQuestionResponse>;
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
