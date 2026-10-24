import { contextBridge, ipcRenderer, webUtils } from "electron";
import type { AppInfo, BackendHealthResponse, StorageStatusResponse, AIStatusResponse, AIProviderSelectionResponse, AIConnectionCheckResponse, AICredentialResponse, DocumentContentResponse, DocumentImportResponse, DocumentRemoveResponse, DocumentListResponse, DocumentRetryResponse, DocumentContentOptions, FolderImportPreviewResponse, FolderImportResponse, FolderImportProgress, DocumentAnalysisPreviewResponse, DocumentAnalysisStartResponse, DocumentAnalysisStatusResponse, DocumentAnalysisFindingsResponse, DocumentQuestionPreviewResponse, DocumentQuestionStartResponse, DocumentQuestionStatusResponse, DocumentQuestionFollowUp, MatterReviewResponse, MatterAnalysisBatchPreviewResponse, MatterAnalysisBatchResponse, MatterQuestionPreviewResponse, MatterQuestionResponse, MatterQuestionRequest, MatterQuestionSubmission, BillingPreviewResponse, BillingPreviewRow, ReconciliationFilters, ReconciliationAsOfFilters, ReconciliationReadResponse, ReconciliationWriteResponse, BillingCalculationRequest, BillingCalculationReadResponse, BillingCalculationWriteResponse } from "./ipc-contract";

// Sandboxed preloads cannot load arbitrary local CommonJS modules at runtime.
// Keep this one allowlisted channel local while sharing the response type.
// This input boundary must stay local as well: importing ipc-contract for a
// runtime constant would make the sandboxed preload require that module.
const MAX_MATTER_QUESTION_CHARACTERS = 5_000;
function unicodeCodePointCount(value: string): number {
  return Array.from(value).length;
}
const GET_APP_INFO_CHANNEL = "app:get-info" as const;
const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;
const GET_AI_STATUS_CHANNEL = "app:get-ai-status" as const;
const SET_AI_PROVIDER_CHANNEL = "app:set-ai-provider" as const;
const CHECK_AI_CONNECTION_CHANNEL = "app:check-ai-connection" as const;
const SET_AI_AUTHORIZATION_CHANNEL = "app:set-ai-authorization" as const;
const SAVE_AI_API_KEY_CHANNEL = "app:save-ai-api-key" as const;
const REMOVE_AI_API_KEY_CHANNEL = "app:remove-ai-api-key" as const;
const CONNECT_CODEX_CHANNEL = "app:connect-codex" as const;
const DISCONNECT_CODEX_CHANNEL = "app:disconnect-codex" as const;

/** Copy only the scoped request fields; Electron main and Python validate them again. */
function matterQuestionPayload(request: MatterQuestionRequest): MatterQuestionRequest {
  const scope = request.scope;
  const question = request.question.trim();
  if (!/^[a-z0-9-]{1,64}$/.test(request.matterId) || !question || unicodeCodePointCount(question) > MAX_MATTER_QUESTION_CHARACTERS) throw new Error("Invalid matter question.");
  if (scope.mode === "selected" && (!Array.isArray(scope.documentIds) || scope.documentIds.length < 1 || scope.documentIds.length > 20 || !scope.documentIds.every((id) => /^[a-f0-9-]{36}$/i.test(id)))) throw new Error("Select between one and 20 valid documents.");
  if (request.followUpQuestionId !== undefined && !/^[a-f0-9-]{36}$/i.test(request.followUpQuestionId)) throw new Error("Invalid follow-up reference.");
  return {
    matterId: request.matterId,
    scope: scope.mode === "all" ? { mode: "all" } : { mode: "selected", documentIds: [...scope.documentIds] },
    question,
    ...(request.followUpQuestionId !== undefined ? { followUpQuestionId: request.followUpQuestionId } : {}),
  };
}

function documentQuestionFollowUpPayload(followUp: DocumentQuestionFollowUp): DocumentQuestionFollowUp {
  return Object.freeze({
    question: followUp.question.trim(),
    answer: followUp.answer.trim(),
  });
}
const GET_DOCUMENT_ANALYSIS_PREVIEW_CHANNEL = "documents:get-analysis-preview" as const;
const START_DOCUMENT_ANALYSIS_CHANNEL = "documents:start-analysis" as const;
const GET_DOCUMENT_ANALYSIS_STATUS_CHANNEL = "documents:get-analysis-status" as const;
const GET_DOCUMENT_ANALYSIS_FINDINGS_CHANNEL = "documents:get-analysis-findings" as const;
const PREVIEW_DOCUMENT_QUESTION_CHANNEL = "documents:preview-question" as const;
const ASK_DOCUMENT_QUESTION_CHANNEL = "documents:ask-question" as const;
const GET_DOCUMENT_QUESTION_STATUS_CHANNEL = "documents:get-question-status" as const;
const GET_MATTER_REVIEW_CHANNEL = "matter:get-review" as const;
const GET_BILLING_PREVIEW_CHANNEL = "matter:get-billing-preview" as const;
const PREVIEW_MATTER_ANALYSIS_BATCH_CHANNEL = "matter:preview-analysis-batch" as const;
const START_MATTER_ANALYSIS_BATCH_CHANNEL = "matter:start-analysis-batch" as const;
const GET_MATTER_ANALYSIS_BATCH_STATUS_CHANNEL = "matter:get-analysis-batch-status" as const;
const PREVIEW_MATTER_QUESTION_CHANNEL = "matter:preview-question" as const;
const ASK_MATTER_QUESTION_CHANNEL = "matter:ask-question" as const;
const GET_MATTER_QUESTION_STATUS_CHANNEL = "matter:get-question-status" as const;
const GET_RECONCILIATION_HOLDINGS_AS_OF_CHANNEL = "reconciliation:get-holdings-as-of" as const;
const GET_RECONCILIATION_TIMELINE_CHANNEL = "reconciliation:get-timeline" as const;
const GET_RECONCILIATION_TRANSFERS_CHANNEL = "reconciliation:get-transfers" as const;
const GET_RECONCILIATION_ASSIGNMENTS_CHANNEL = "reconciliation:get-assignments" as const;
const GET_RECONCILIATION_DISCREPANCIES_CHANNEL = "reconciliation:get-discrepancies" as const;
const GET_RECONCILIATION_RESULT_CHANNEL = "reconciliation:get-result" as const;
const GET_RECONCILIATION_CANDIDATES_CHANNEL = "reconciliation:get-candidates" as const;
const CONFIRM_RECONCILIATION_CANDIDATE_CHANNEL = "reconciliation:confirm-candidate" as const;
const REJECT_RECONCILIATION_CANDIDATE_CHANNEL = "reconciliation:reject-candidate" as const;
const APPLY_RECONCILIATION_POLICY_CHANNEL = "reconciliation:apply-policy" as const;
const GET_BILLING_RULE_CANDIDATES_CHANNEL = "billing:get-rule-candidates" as const;
const APPROVE_BILLING_RULE_CHANNEL = "billing:approve-rule" as const;
const SAVE_BILLING_CALCULATION_CHANNEL = "billing:save-calculation" as const;
const GET_BILLING_RULE_CHANNEL = "billing:get-rule" as const;
const GET_BILLING_CALCULATION_PREVIEW_CHANNEL = "billing:get-calculation-preview" as const;
const GET_BILLING_CALCULATION_RUNS_CHANNEL = "billing:get-calculation-runs" as const;
const GET_BILLING_CALCULATION_RUN_CHANNEL = "billing:get-calculation-run" as const;
const COMPARE_BILLING_CALCULATION_RUNS_CHANNEL = "billing:compare-calculation-runs" as const;
const SELECT_AND_IMPORT_DOCUMENTS_CHANNEL = "documents:select-and-import" as const;
const SELECT_FOLDER_FOR_IMPORT_CHANNEL = "documents:select-folder-for-import" as const;
const CONFIRM_FOLDER_IMPORT_CHANNEL = "documents:confirm-folder-import" as const;
const CANCEL_FOLDER_IMPORT_CHANNEL = "documents:cancel-folder-import" as const;
const FOLDER_IMPORT_PROGRESS_CHANNEL = "documents:folder-import-progress" as const;
const IMPORT_DROPPED_DOCUMENTS_CHANNEL = "documents:import-dropped" as const;
const REMOVE_DOCUMENT_CHANNEL = "documents:remove" as const;
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
      candidate.platform === "linux") &&
    isBuildMetadata(candidate.build)
  );
}

function isBuildMetadata(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return ["applicationVersion", "buildDate", "sourceRevision", "sourceRevisionShort", "sourceState", "buildId"].every((key) => typeof candidate[key] === "string" && candidate[key].length > 0);
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
    typeof (platform as Record<string, unknown>).machine === "string" &&
    isBuildMetadata(health.build)
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
  async getAIStatus(): Promise<AIStatusResponse> { return Object.freeze(await ipcRenderer.invoke(GET_AI_STATUS_CHANNEL) as AIStatusResponse); },
  async setAIProvider(provider: "codex" | "openai"): Promise<AIProviderSelectionResponse> {
    if (provider !== "codex" && provider !== "openai") throw new Error("Invalid AI provider.");
    return Object.freeze(await ipcRenderer.invoke(SET_AI_PROVIDER_CHANNEL, { provider }) as AIProviderSelectionResponse);
  },
  async connectCodex() { return Object.freeze(await ipcRenderer.invoke(CONNECT_CODEX_CHANNEL)); },
  async disconnectCodex() { return Object.freeze(await ipcRenderer.invoke(DISCONNECT_CODEX_CHANNEL)); },
  async checkAIConnection(): Promise<AIConnectionCheckResponse> { return Object.freeze(await ipcRenderer.invoke(CHECK_AI_CONNECTION_CHANNEL) as AIConnectionCheckResponse); },
  async setAIAuthorization(authorized: boolean): Promise<AIStatusResponse> { return Object.freeze(await ipcRenderer.invoke(SET_AI_AUTHORIZATION_CHANNEL, { authorized }) as AIStatusResponse); },
  async saveAIApiKey(apiKey: string): Promise<AICredentialResponse> {
    if (typeof apiKey !== "string" || !apiKey.trim() || apiKey.length > 1024) throw new Error("Invalid API key.");
    return Object.freeze(await ipcRenderer.invoke(SAVE_AI_API_KEY_CHANNEL, { apiKey }) as AICredentialResponse);
  },
  async removeAIApiKey(): Promise<AICredentialResponse> { return Object.freeze(await ipcRenderer.invoke(REMOVE_AI_API_KEY_CHANNEL) as AICredentialResponse); },
  async getDocumentAnalysisPreview(documentId: string): Promise<DocumentAnalysisPreviewResponse> {
    if (!documentId || documentId.length > 128) throw new Error("Invalid document analysis request.");
    return Object.freeze(await ipcRenderer.invoke(GET_DOCUMENT_ANALYSIS_PREVIEW_CHANNEL, { documentId }) as DocumentAnalysisPreviewResponse);
  },
  async startDocumentAnalysis(documentId: string): Promise<DocumentAnalysisStartResponse> {
    if (!documentId || documentId.length > 128) throw new Error("Invalid document analysis request.");
    return Object.freeze(await ipcRenderer.invoke(START_DOCUMENT_ANALYSIS_CHANNEL, { documentId, acknowledged: true }) as DocumentAnalysisStartResponse);
  },
  async getDocumentAnalysisStatus(jobId: string): Promise<DocumentAnalysisStatusResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(jobId)) throw new Error("Invalid analysis job reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_DOCUMENT_ANALYSIS_STATUS_CHANNEL, { jobId }) as DocumentAnalysisStatusResponse);
  },
  async getDocumentAnalysisFindings(jobId: string): Promise<DocumentAnalysisFindingsResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(jobId)) throw new Error("Invalid analysis job reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_DOCUMENT_ANALYSIS_FINDINGS_CHANNEL, { jobId, limit: 25 }) as DocumentAnalysisFindingsResponse);
  },
  async previewDocumentQuestion(documentId: string, question: string, followUp?: DocumentQuestionFollowUp): Promise<DocumentQuestionPreviewResponse> {
    if (!documentId || documentId.length > 128 || !question.trim() || question.length > 600) throw new Error("Invalid document question.");
    if (followUp && (!followUp.question.trim() || !followUp.answer.trim() || followUp.question.length > 600 || followUp.answer.length > 1200)) throw new Error("Invalid document follow-up context.");
    return Object.freeze(await ipcRenderer.invoke(PREVIEW_DOCUMENT_QUESTION_CHANNEL, {
      documentId,
      question: question.trim(),
      ...(followUp ? { followUp: documentQuestionFollowUpPayload(followUp) } : {}),
    }) as DocumentQuestionPreviewResponse);
  },
  async askDocumentQuestion(documentId: string, question: string, followUp?: DocumentQuestionFollowUp): Promise<DocumentQuestionStartResponse> {
    if (!documentId || documentId.length > 128 || !question.trim() || question.length > 600) throw new Error("Invalid document question.");
    if (followUp && (!followUp.question.trim() || !followUp.answer.trim() || followUp.question.length > 600 || followUp.answer.length > 1200)) throw new Error("Invalid document follow-up context.");
    return Object.freeze(await ipcRenderer.invoke(ASK_DOCUMENT_QUESTION_CHANNEL, {
      documentId,
      question: question.trim(),
      acknowledged: true,
      ...(followUp ? { followUp: documentQuestionFollowUpPayload(followUp) } : {}),
    }) as DocumentQuestionStartResponse);
  },
  async getDocumentQuestionStatus(questionId: string): Promise<DocumentQuestionStatusResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(questionId)) throw new Error("Invalid document question reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_DOCUMENT_QUESTION_STATUS_CHANNEL, { questionId }) as DocumentQuestionStatusResponse);
  },
  async getMatterReview(documentIds: string[]): Promise<MatterReviewResponse> {
    if (!Array.isArray(documentIds) || documentIds.length < 1 || documentIds.length > 20 || !documentIds.every((id) => /^[a-f0-9-]{36}$/i.test(id))) throw new Error("Select up to 20 valid documents.");
    return Object.freeze(await ipcRenderer.invoke(GET_MATTER_REVIEW_CHANNEL, { documentIds: [...documentIds] }) as MatterReviewResponse);
  },
  async previewMatterAnalysisBatch(documentIds: string[]): Promise<MatterAnalysisBatchPreviewResponse> {
    if (!Array.isArray(documentIds) || documentIds.length < 1 || documentIds.length > 20 || !documentIds.every((id) => /^[a-f0-9-]{36}$/i.test(id))) throw new Error("Select up to 20 valid documents.");
    return Object.freeze(await ipcRenderer.invoke(PREVIEW_MATTER_ANALYSIS_BATCH_CHANNEL, { documentIds: [...documentIds] }) as MatterAnalysisBatchPreviewResponse);
  },
  async startMatterAnalysisBatch(documentIds: string[]): Promise<MatterAnalysisBatchResponse> {
    if (!Array.isArray(documentIds) || documentIds.length < 1 || documentIds.length > 20 || !documentIds.every((id) => /^[a-f0-9-]{36}$/i.test(id))) throw new Error("Select up to 20 valid documents.");
    return Object.freeze(await ipcRenderer.invoke(START_MATTER_ANALYSIS_BATCH_CHANNEL, { documentIds: [...documentIds], acknowledged: true }) as MatterAnalysisBatchResponse);
  },
  async getMatterAnalysisBatchStatus(batchId: string): Promise<MatterAnalysisBatchResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(batchId)) throw new Error("Invalid batch analysis reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_MATTER_ANALYSIS_BATCH_STATUS_CHANNEL, { batchId }) as MatterAnalysisBatchResponse);
  },
  async previewMatterQuestion(request: MatterQuestionRequest): Promise<MatterQuestionPreviewResponse> {
    return Object.freeze(await ipcRenderer.invoke(PREVIEW_MATTER_QUESTION_CHANNEL, matterQuestionPayload(request)) as MatterQuestionPreviewResponse);
  },
  async askMatterQuestion(submission: MatterQuestionSubmission): Promise<MatterQuestionResponse> {
    const consented = submission.consentedDocumentIds;
    if (!Array.isArray(consented) || consented.length > 10_000 || !consented.every((id) => /^[a-f0-9-]{36}$/i.test(id))) throw new Error("Invalid document consent list.");
    return Object.freeze(await ipcRenderer.invoke(ASK_MATTER_QUESTION_CHANNEL, {
      ...matterQuestionPayload(submission), acknowledged: true, consentedDocumentIds: [...consented],
    }) as MatterQuestionResponse);
  },
  async getMatterQuestionStatus(questionId: string): Promise<MatterQuestionResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(questionId)) throw new Error("Invalid consolidated question reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_MATTER_QUESTION_STATUS_CHANNEL, { questionId }) as MatterQuestionResponse);
  },
  async getReconciliationHoldingsAsOf(filters: ReconciliationAsOfFilters): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_HOLDINGS_AS_OF_CHANNEL, { ...filters }) as ReconciliationReadResponse);
  },
  async getReconciliationTimeline(filters: ReconciliationFilters = {}): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_TIMELINE_CHANNEL, { ...filters }) as ReconciliationReadResponse);
  },
  async getReconciliationTransfers(filters: ReconciliationFilters = {}): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_TRANSFERS_CHANNEL, { ...filters }) as ReconciliationReadResponse);
  },
  async getReconciliationAssignments(filters: Pick<ReconciliationFilters, "fromDate" | "toDate" | "isin"> & { claimReference?: string; fromEntity?: string; toEntity?: string } = {}): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_ASSIGNMENTS_CHANNEL, { ...filters }) as ReconciliationReadResponse);
  },
  async getReconciliationDiscrepancies(filters: ReconciliationFilters = {}): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_DISCREPANCIES_CHANNEL, { ...filters }) as ReconciliationReadResponse);
  },
  async getReconciliationResult(filters: ReconciliationAsOfFilters): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_RESULT_CHANNEL, { ...filters }) as ReconciliationReadResponse);
  },
  async getReconciliationCandidates(status?: "candidate" | "confirmed" | "rejected"): Promise<ReconciliationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_RECONCILIATION_CANDIDATES_CHANNEL, status ? { status } : {}) as ReconciliationReadResponse);
  },
  async confirmReconciliationCandidate(candidateId: string): Promise<ReconciliationWriteResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(candidateId)) throw new Error("Invalid candidate reference.");
    return Object.freeze(await ipcRenderer.invoke(CONFIRM_RECONCILIATION_CANDIDATE_CHANNEL, { candidateId, acknowledged: true }) as ReconciliationWriteResponse);
  },
  async rejectReconciliationCandidate(candidateId: string, reason: string): Promise<ReconciliationWriteResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(candidateId) || !reason.trim() || reason.trim().length > 500) throw new Error("A short rejection reason is required.");
    return Object.freeze(await ipcRenderer.invoke(REJECT_RECONCILIATION_CANDIDATE_CHANNEL, { candidateId, reason: reason.trim(), acknowledged: true }) as ReconciliationWriteResponse);
  },
  async applyReconciliationPolicy(candidateIds: string[]): Promise<ReconciliationWriteResponse> {
    if (!Array.isArray(candidateIds) || candidateIds.length < 1 || candidateIds.length > 100 || !candidateIds.every((id) => /^[a-f0-9-]{36}$/i.test(id))) throw new Error("Invalid candidate review list.");
    return Object.freeze(await ipcRenderer.invoke(APPLY_RECONCILIATION_POLICY_CHANNEL, { candidateIds: [...new Set(candidateIds)], policy: "reviewed_batch", acknowledged: true }) as ReconciliationWriteResponse);
  },
  async getBillingRuleCandidates(status?: "candidate" | "approved"): Promise<BillingCalculationReadResponse> {
    return Object.freeze(await ipcRenderer.invoke(GET_BILLING_RULE_CANDIDATES_CHANNEL, status ? { status } : {}) as BillingCalculationReadResponse);
  },
  async approveBillingRule(candidateId: string): Promise<BillingCalculationWriteResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(candidateId)) throw new Error("Invalid billing-rule candidate reference.");
    return Object.freeze(await ipcRenderer.invoke(APPROVE_BILLING_RULE_CHANNEL, { candidateId, acknowledged: true }) as BillingCalculationWriteResponse);
  },
  async getBillingRule(billingAgreementId: string): Promise<BillingCalculationReadResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(billingAgreementId)) throw new Error("Invalid billing agreement reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_BILLING_RULE_CHANNEL, { billingAgreementId }) as BillingCalculationReadResponse);
  },
  async previewBillingCalculation(request: BillingCalculationRequest): Promise<BillingCalculationReadResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(request.billingAgreementId) || (request.asOfDate !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(request.asOfDate))) throw new Error("Invalid billing calculation request.");
    return Object.freeze(await ipcRenderer.invoke(GET_BILLING_CALCULATION_PREVIEW_CHANNEL, { ...request }) as BillingCalculationReadResponse);
  },
  async saveBillingCalculation(request: BillingCalculationRequest): Promise<BillingCalculationWriteResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(request.billingAgreementId) || (request.asOfDate !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(request.asOfDate))) throw new Error("Invalid billing calculation request.");
    return Object.freeze(await ipcRenderer.invoke(SAVE_BILLING_CALCULATION_CHANNEL, { ...request, acknowledged: true }) as BillingCalculationWriteResponse);
  },
  async getBillingCalculationRuns(billingAgreementId: string): Promise<BillingCalculationReadResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(billingAgreementId)) throw new Error("Invalid billing agreement reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_BILLING_CALCULATION_RUNS_CHANNEL, { billingAgreementId }) as BillingCalculationReadResponse);
  },
  async getBillingCalculationRun(runId: string): Promise<BillingCalculationReadResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(runId)) throw new Error("Invalid billing calculation reference.");
    return Object.freeze(await ipcRenderer.invoke(GET_BILLING_CALCULATION_RUN_CHANNEL, { runId }) as BillingCalculationReadResponse);
  },
  async compareBillingCalculationRuns(leftRunId: string, rightRunId: string): Promise<BillingCalculationReadResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(leftRunId) || !/^[a-f0-9-]{36}$/i.test(rightRunId)) throw new Error("Invalid billing calculation comparison.");
    return Object.freeze(await ipcRenderer.invoke(COMPARE_BILLING_CALCULATION_RUNS_CHANNEL, { leftRunId, rightRunId }) as BillingCalculationReadResponse);
  },
  async getBillingPreview(feePool: string, currency: string, rows: BillingPreviewRow[]): Promise<BillingPreviewResponse> {
    if (typeof feePool !== "string" || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$/.test(feePool) || !/^[A-Z]{3}$/.test(currency) || !Array.isArray(rows) || rows.length < 1 || rows.length > 50 || !rows.every((row) => typeof row.id === "string" && typeof row.label === "string" && typeof row.eligibleAmount === "string")) throw new Error("Invalid billing preview assumptions.");
    return Object.freeze(await ipcRenderer.invoke(GET_BILLING_PREVIEW_CHANNEL, { feePool, currency, rows: rows.map((row) => ({ ...row })) }) as BillingPreviewResponse);
  },
  async selectAndImportDocuments(): Promise<DocumentImportResponse> {
    const response: unknown = await ipcRenderer.invoke(SELECT_AND_IMPORT_DOCUMENTS_CHANNEL);
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid document response.");
    return Object.freeze(response) as DocumentImportResponse;
  },
  async selectFolderForImport(): Promise<FolderImportPreviewResponse> {
    const response: unknown = await ipcRenderer.invoke(SELECT_FOLDER_FOR_IMPORT_CHANNEL);
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid folder response.");
    return Object.freeze(response) as FolderImportPreviewResponse;
  },
  async confirmFolderImport(authorizationId: string): Promise<FolderImportResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(authorizationId)) throw new Error("Invalid folder import authorization.");
    const response: unknown = await ipcRenderer.invoke(CONFIRM_FOLDER_IMPORT_CHANNEL, { authorizationId });
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid folder import response.");
    return Object.freeze(response) as FolderImportResponse;
  },
  async cancelFolderImport(authorizationId: string): Promise<void> {
    if (!/^[a-f0-9-]{36}$/i.test(authorizationId)) return;
    await ipcRenderer.invoke(CANCEL_FOLDER_IMPORT_CHANNEL, { authorizationId });
  },
  onFolderImportProgress(listener: (progress: FolderImportProgress) => void): () => void {
    const receive = (_event: Electron.IpcRendererEvent, value: unknown) => {
      if (typeof value === "object" && value !== null && typeof (value as Record<string, unknown>).authorizationId === "string") listener(value as FolderImportProgress);
    };
    ipcRenderer.on(FOLDER_IMPORT_PROGRESS_CHANNEL, receive);
    return () => ipcRenderer.removeListener(FOLDER_IMPORT_PROGRESS_CHANNEL, receive);
  },
  async importDroppedDocuments(files: File[]): Promise<FolderImportPreviewResponse> {
    // A DOM File from an Electron drop has an OS path only inside this privileged preload.
    // The path is immediately sent to the allowlisted main-process channel and never returned.
    const paths = files.map((file) => {
      try { return webUtils.getPathForFile(file); } catch { return ""; }
    }).filter((path): path is string => typeof path === "string" && path.length > 0);
    if (paths.length !== files.length || paths.length === 0 || paths.length > 20) throw new Error("Drop up to 20 valid local files or folders.");
    const response: unknown = await ipcRenderer.invoke(IMPORT_DROPPED_DOCUMENTS_CHANNEL, { paths });
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid import preview response.");
    return Object.freeze(response) as FolderImportPreviewResponse;
  },
  async removeDocument(documentId: string): Promise<DocumentRemoveResponse> {
    if (!/^[a-f0-9-]{36}$/i.test(documentId)) throw new Error("Invalid document removal request.");
    const response: unknown = await ipcRenderer.invoke(REMOVE_DOCUMENT_CHANNEL, { documentId });
    if (!isDocumentResponse(response)) throw new Error("The desktop application returned an invalid document removal response.");
    return Object.freeze(response) as DocumentRemoveResponse;
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
