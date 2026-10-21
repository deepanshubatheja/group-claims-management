import { evidenceTarget, spreadsheetRequest } from "./document-viewer-navigation.js";

/** ClaimFlow's single local workspace is one implicit matter; Python validates this ID. */
export const WORKSPACE_MATTER_ID = "workspace";
export const MAX_SELECTED_QUESTION_DOCUMENTS = 20;

export type ChatScopeMode = "all" | "selected";

/** Entering a matter starts with every matter document in scope; selecting files is never required. */
export const DEFAULT_CHAT_SCOPE: ChatScopeMode = "all";

/**
 * The explicit scope sent with a question. Selected mode with no documents
 * returns null: it is never widened to all documents.
 */
export function questionScope(mode: ChatScopeMode, selectedIds: readonly string[]): MatterQuestionScopeRequest | null {
  if (mode === "all") return { mode: "all" };
  return selectedIds.length > 0 ? { mode: "selected", documentIds: [...selectedIds] } : null;
}

/** Why the composer cannot submit in the current scope, or null when it can. */
export function scopeSubmissionBlock(mode: ChatScopeMode, selectedIds: readonly string[], matterDocumentCount: number): string | null {
  if (matterDocumentCount === 0) return "Import documents into this matter before asking a question.";
  if (mode === "selected" && selectedIds.length === 0) {
    return "Selected documents only is chosen, but no documents are selected. Select at least one document below, or choose All documents in this matter.";
  }
  if (mode === "selected" && selectedIds.length > MAX_SELECTED_QUESTION_DOCUMENTS) {
    return `Select up to ${MAX_SELECTED_QUESTION_DOCUMENTS} documents, or choose All documents in this matter.`;
  }
  return null;
}

/** Scope summary shown beside the composer before a question is checked. */
export function composerScopeSummary(mode: ChatScopeMode, selectedCount: number, documents: readonly DocumentSummary[]): string {
  if (mode === "selected") {
    return `Selected documents only · ${selectedCount} document${selectedCount === 1 ? "" : "s"} in scope`;
  }
  const parsed = documents.filter((document) => document.processing_status === "parsed").length;
  const unavailable = documents.length - parsed;
  return `All documents in this matter · ${documents.length} document${documents.length === 1 ? "" : "s"} in scope`
    + (unavailable > 0 ? ` (${unavailable} not yet parsed and cannot be searched)` : "");
}

/** The scope label permanently attached to an answer. */
export function answerScopeLabel(scope: MatterDocumentScope): string {
  const base = scope.mode === "all" ? "All documents in this matter" : "Selected documents only";
  const searchable = scope.searchableDocuments === scope.documentsInScope
    ? `${scope.documentsInScope} document${scope.documentsInScope === 1 ? "" : "s"}`
    : `${scope.searchableDocuments} of ${scope.documentsInScope} documents searchable`;
  // Named documents only ever narrow the scope; say so instead of implying all were used.
  const named = (scope.resolvedDocuments ?? []).length;  // absent on answers from before named-document resolution
  const narrowed = named > 0 ? ` · narrowed by the question to ${named} named document${named === 1 ? "" : "s"}` : "";
  return `${base} · ${searchable}${narrowed} · evidence from ${scope.documentsWithEvidence}`;
}

export function unavailableReasonLabel(reason: MatterUnavailableReason): string {
  return {
    not_parsed: "not parsed yet",
    extraction_failed: "extraction failed",
    unsupported: "unsupported format",
    extraction_unavailable: "no current extraction",
    no_searchable_text: "no searchable text (for example, OCR unavailable)",
  }[reason];
}

/** Documents in this request that have not previously been authorized for the selected provider. */
export function documentsNeedingConsent(scope: MatterDocumentScope): MatterDocumentScope["transmittedDocuments"] {
  return scope.transmittedDocuments.filter((document) => !document.previouslyAuthorized);
}

/**
 * Whether an earlier answer can be followed up in the current scope. Python
 * enforces the same rule; this lets the UI explain it before a request.
 */
export function canFollowUpInScope(turnScope: MatterDocumentScope, mode: ChatScopeMode, selectedIds: readonly string[]): boolean {
  if (mode === "all") return true;
  const selected = new Set(selectedIds);
  return turnScope.transmittedDocuments.every((document) => selected.has(document.documentId));
}

export type DocumentCitationGroup = { documentId: string; filename: string; citations: MatterQuestionCitation[] };

/** Keep each finding's supporting document visible in a multi-document answer. */
export function citationsByDocument(citations: readonly MatterQuestionCitation[]): DocumentCitationGroup[] {
  const groups = new Map<string, DocumentCitationGroup>();
  for (const citation of citations) {
    const group = groups.get(citation.documentId) ?? { documentId: citation.documentId, filename: citation.filename, citations: [] };
    group.citations.push(citation);
    groups.set(citation.documentId, group);
  }
  return [...groups.values()];
}

/** Map a backend-validated citation to the document viewer request that shows it. */
export function sourceLocationRequest(sourceReference: string): DocumentContentOptions | null {
  const target = evidenceTarget(sourceReference);
  if (target === null) return null;
  if (target.kind === "pdf") return { pageNumber: target.pageNumber };
  return spreadsheetRequest(target.sheetName, Math.max(1, target.rowNumber - 3), Math.max(1, target.columnNumber - 3));
}

const ROLE_LABELS: Record<string, string> = {
  investor: "Investor",
  investment_vehicle: "Investment vehicle",
  subfund: "Subfund",
  custodian: "Custodian",
  broker_dealer: "Broker/dealer",
  legal_claimant: "Legal claimant",
  other_organization: "Other organization",
  unknown: "Unknown role",
  investor_or_legal_entity: "Investor or legal entity",
  investment_vehicle_or_subfund: "Investment vehicle or subfund",
};

/** Readable role name; unrecognised roles stay visibly unknown rather than guessed. */
export function entityRoleLabel(role: string): string {
  return ROLE_LABELS[role] ?? "Unknown role";
}

export function questionIntentLabel(intent: MatterQuestionIntent): string {
  return {
    inventory: "Listing across all documents in scope (searched locally)",
    relationship: "Entity relationship mapping (built locally)",
    specific_lookup: "Exact account/ISIN lookup",
    holdings_comparison: "Dated holdings comparison (calculated locally)",
    change: "Document change or transfer analysis",
    legal_status: "Legal claim status",
    comparison: "Cross-document comparison",
    calculation: "Pro-rata calculation",
    general: "General evidence question",
  }[intent];
}

/** One-line coverage statement for an entity inventory. */
export function inventoryCoverageSummary(inventory: MatterEntityInventory): string {
  const coverage = inventory.coverage;
  const searched = coverage.searchedDocuments === coverage.eligibleDocuments
    ? `Searched all ${coverage.eligibleDocuments} searchable document${coverage.eligibleDocuments === 1 ? "" : "s"}`
    : `Searched ${coverage.searchedDocuments} of ${coverage.eligibleDocuments} searchable documents`;
  return `${searched}; ${coverage.documentsWithListedEntities} contain listed entities.${coverage.partial ? ` Partial: ${coverage.partialDocuments.length} document${coverage.partialDocuments.length === 1 ? "" : "s"} exceeded the local scanning limit.` : ""}`;
}

export function relationshipKindLabel(kind: MatterRelationship["kind"]): string {
  return {
    explicit_statement: "Documented: the source states it",
    explicit_heading: "Documented: a column heading states it",
    document_section: "Documented association: the sheet’s investor label covers the table listing this subfund",
    same_row: "Same row only (association, not a documented relationship)",
  }[kind];
}

export function holdingStatusLabel(status: MatterHoldingRow["status"]): string {
  return {
    compared: "Compared",
    only_earlier: "Only in the earlier file",
    only_later: "Only in the later file",
    duplicate_rows: "Duplicate rows — not summed",
    unusable_quantity: "Quantity not usable",
    unit_mismatch: "Units differ — not compared",
    currency_mismatch: "Currencies differ — not compared",
  }[status];
}

/** Thousands-grouped display of an exact decimal string, without converting it to a binary float. */
export function groupedQuantity(value: string | null): string {
  if (value === null) return "—";
  const match = /^([+-]?)(\d+)(\.\d+)?$/.exec(value.trim());
  if (!match) return value;
  const fraction = match[3] && /[1-9]/.test(match[3]) ? match[3] : "";
  return `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
}

export function controlTotalLabel(status: NonNullable<MatterHoldingsTable["controlTotal"]>["status"]): string {
  return {
    formula_covers_records: "Formula covers exactly the record rows",
    matches: "Matches the record rows",
    differs: "Does not match the record rows",
    not_verified: "Not verified",
  }[status];
}
