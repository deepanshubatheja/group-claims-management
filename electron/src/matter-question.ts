import { MAX_MATTER_QUESTION_CHARACTERS, unicodeCodePointCount, type MatterDocumentScope, type MatterEntityInventory, type MatterHoldingsComparison, type MatterQuestionRequest, type MatterQuestionScopeRequest, type MatterQuestionSubmission } from "./ipc-contract";

const DOCUMENT_ID = /^[a-f0-9-]{36}$/i;
const MATTER_ID = /^[a-z0-9-]{1,64}$/;
const MAX_SELECTED_DOCUMENTS = 20;
// Consent may name every document in a large all-document scope.
const MAX_CONSENTED_DOCUMENTS = 10_000;
const UNAVAILABLE_REASONS = new Set(["not_parsed", "extraction_failed", "unsupported", "extraction_unavailable", "no_searchable_text"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function uniqueDocumentIds(value: unknown, minimum: number, maximum: number): string[] | undefined {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) return undefined;
  if (!value.every((id) => typeof id === "string" && DOCUMENT_ID.test(id)) || new Set(value).size !== value.length) return undefined;
  return [...value] as string[];
}

function scopeRequest(value: unknown): MatterQuestionScopeRequest | undefined {
  if (!isRecord(value)) return undefined;
  if (value.mode === "all") return Object.keys(value).length === 1 ? { mode: "all" } : undefined;
  if (value.mode !== "selected" || Object.keys(value).length !== 2) return undefined;
  // An empty selection is invalid here and in Python: it is never widened to all documents.
  const documentIds = uniqueDocumentIds(value.documentIds, 1, MAX_SELECTED_DOCUMENTS);
  return documentIds ? { mode: "selected", documentIds } : undefined;
}

/** Validate an untrusted renderer payload at the IPC boundary; Python validates it again. */
export function matterQuestionRequest(payload: unknown, submission: false): MatterQuestionRequest | undefined;
export function matterQuestionRequest(payload: unknown, submission: true): MatterQuestionSubmission | undefined;
export function matterQuestionRequest(payload: unknown, submission: boolean): MatterQuestionRequest | MatterQuestionSubmission | undefined {
  if (!isRecord(payload)) return undefined;
  const allowed = new Set(["matterId", "scope", "question", "followUpQuestionId", ...(submission ? ["acknowledged", "consentedDocumentIds"] : [])]);
  if (Object.keys(payload).some((key) => !allowed.has(key))) return undefined;
  if (typeof payload.matterId !== "string" || !MATTER_ID.test(payload.matterId)) return undefined;
  if (typeof payload.question !== "string") return undefined;
  const question = payload.question.trim();
  if (!question || unicodeCodePointCount(question) > MAX_MATTER_QUESTION_CHARACTERS) return undefined;
  const scope = scopeRequest(payload.scope);
  if (!scope) return undefined;
  const followUpQuestionId = payload.followUpQuestionId;
  if (followUpQuestionId !== undefined && (typeof followUpQuestionId !== "string" || !DOCUMENT_ID.test(followUpQuestionId))) return undefined;
  const request: MatterQuestionRequest = {
    matterId: payload.matterId, scope, question,
    ...(followUpQuestionId !== undefined ? { followUpQuestionId } : {}),
  };
  if (!submission) return request;
  if (payload.acknowledged !== true) return undefined;
  const consentedDocumentIds = uniqueDocumentIds(payload.consentedDocumentIds, 0, MAX_CONSENTED_DOCUMENTS);
  return consentedDocumentIds ? { ...request, consentedDocumentIds } : undefined;
}

function documentList(value: unknown, extra: (item: Record<string, unknown>) => boolean): boolean {
  return Array.isArray(value) && value.every((item) => isRecord(item) && typeof item.documentId === "string"
    && typeof item.filename === "string" && extra(item));
}

/** Guard the backend's resolved document scope before it reaches the renderer. */
export function isMatterDocumentScope(value: unknown): value is MatterDocumentScope {
  return isRecord(value) && typeof value.matterId === "string" && (value.mode === "all" || value.mode === "selected")
    && typeof value.documentsInScope === "number" && typeof value.searchableDocuments === "number"
    && typeof value.documentsWithEvidence === "number" && typeof value.searchTruncated === "boolean"
    && documentList(value.unavailableDocuments, (item) => UNAVAILABLE_REASONS.has(String(item.reason)))
    && documentList(value.incompleteDocuments, () => true)
    && documentList(value.transmittedDocuments, (item) => typeof item.previouslyAuthorized === "boolean")
    && documentList(value.resolvedDocuments, (item) => typeof item.matchedBy === "string")
    && Array.isArray(value.notes) && value.notes.every((note) => typeof note === "string");
}

const INTENTS = new Set(["inventory", "relationship", "specific_lookup", "holdings_comparison", "historical_reconstruction", "identity_resolution", "holdings_conflict", "change", "legal_status", "comparison", "calculation", "general"]);
const RELATION_KINDS = new Set(["explicit_statement", "explicit_heading", "document_section", "same_row"]);
const HOLDING_STATUSES = new Set(["compared", "only_earlier", "only_later", "duplicate_rows", "unusable_quantity", "unit_mismatch", "currency_mismatch"]);
const CONTROL_TOTAL_STATUSES = new Set(["formula_covers_records", "matches", "differs", "not_verified"]);
const nullableString = (value: unknown) => typeof value === "string" || value === null;

export function isMatterQuestionIntent(value: unknown): boolean {
  return typeof value === "string" && INTENTS.has(value);
}

/** Guard a locally built entity inventory before it reaches the renderer. */
export function isMatterEntityInventory(value: unknown): value is MatterEntityInventory {
  if (!isRecord(value) || !Array.isArray(value.requestedRoles) || !Array.isArray(value.entities) || !isRecord(value.otherRoleCounts) || !isRecord(value.coverage)) return false;
  const coverage = value.coverage;
  return Array.isArray(value.relationships) && documentList(value.relationships, (item) => ["subject", "subjectRole", "subjectReference", "related", "relatedRole", "relatedReference", "basis"].every((key) => typeof item[key] === "string") && RELATION_KINDS.has(String(item.kind)))
    && value.requestedRoles.every((role) => typeof role === "string")
    && Object.values(value.otherRoleCounts).every((count) => typeof count === "number")
    && ["eligibleDocuments", "searchedDocuments", "documentsWithRoleLabels", "documentsWithListedEntities", "entitiesOmitted"].every((key) => typeof coverage[key] === "number")
    && typeof coverage.partial === "boolean" && documentList(coverage.partialDocuments, () => true)
    && value.entities.every((entity) => isRecord(entity) && typeof entity.name === "string"
      && Array.isArray(entity.roles) && entity.roles.every((role) => typeof role === "string")
      && Array.isArray(entity.related) && entity.related.every((item) => isRecord(item) && typeof item.name === "string" && typeof item.role === "string")
      && typeof entity.documentCount === "number" && typeof entity.mentionCount === "number"
      && (typeof entity.uncertainty === "string" || entity.uncertainty === null)
      && documentList(entity.evidence, (item) => typeof item.sourceReference === "string" && typeof item.evidenceSnippet === "string"
        && typeof item.role === "string" && typeof item.basis === "string"
        && (item.status === "document_label" || item.status === "provisional_ai_finding")));
}

function isHoldingSide(value: unknown): boolean {
  return value === null || (isRecord(value) && typeof value.documentId === "string" && typeof value.filename === "string"
    && typeof value.sheet === "string" && typeof value.row === "number" && Array.isArray(value.entityReferences)
    && value.entityReferences.every((reference) => typeof reference === "string")
    && ["rowReference", "quantity", "quantityReference", "isinReference", "accountReference", "unit", "unitReference", "currency", "currencyReference", "rowDate", "rowDateReference"].every((key) => nullableString(value[key]))
    && Array.isArray(value.cells) && value.cells.every((cell) => isRecord(cell)
      && typeof cell.heading === "string" && typeof cell.value === "string" && typeof cell.reference === "string"));
}

const optionalType = (value: unknown, type: "number" | "boolean") => value === undefined || typeof value === type;
const ENTITY_MATCHES = new Set(["exact", "alias", "ambiguous", "not_found"]);

function isRequestedEntities(value: unknown): boolean {
  return value === undefined || (Array.isArray(value) && value.every((entry) => isRecord(entry) && typeof entry.text === "string"
    && nullableString(entry.name) && ENTITY_MATCHES.has(String(entry.match)) && typeof entry.positions === "number"
    && Array.isArray(entry.candidates) && entry.candidates.every((candidate) => typeof candidate === "string")));
}

const stringFields = (value: unknown, keys: string[]) => isRecord(value) && keys.every((key) => typeof value[key] === "string");

function isHoldingsTable(value: unknown): boolean {
  if (!isRecord(value) || !stringFields(value, ["sheet", "headerReference", "quantityHeading", "quantityHeadingReference"])
    || !nullableString(value.recordReference) || !nullableString(value.quantityReference)) return false;
  const control = value.controlTotal;
  return (value.investor === null || stringFields(value.investor, ["name", "reference", "label", "labelReference"]))
    && (value.statedDate === null || (stringFields(value.statedDate, ["text", "reference", "labelReference"]) && isRecord(value.statedDate) && nullableString(value.statedDate.date)))
    && (control === null || (stringFields(control, ["reference", "label", "labelReference", "note"]) && isRecord(control)
      && nullableString(control.value) && nullableString(control.formula) && nullableString(control.recordsTotal)
      && CONTROL_TOTAL_STATUSES.has(String(control.status))
      && optionalType(control.recordRows, "number") && optionalType(control.comparisonRows, "number")
      && (control.comparisonTotal === undefined || nullableString(control.comparisonTotal)) && optionalType(control.reconciled, "boolean")));
}

/** Guard a local dated holdings comparison before it reaches the renderer. */
export function isMatterHoldingsComparison(value: unknown): value is MatterHoldingsComparison {
  return isRecord(value) && typeof value.orderBasis === "string"
    && documentList(value.documents, (item) => nullableString(item.date) && nullableString(item.dateBasis)
      && Array.isArray(item.tables) && item.tables.every(isHoldingsTable))
    && isRecord(value.totals) && ["earlier", "later", "difference", "currency", "note"].every((key) => nullableString((value.totals as Record<string, unknown>)[key]))
    && typeof value.legalQuestionNotAnswered === "boolean" && isRequestedEntities(value.requestedEntities)
    && (value.totals.coverage === undefined || value.totals.coverage === "all_records" || value.totals.coverage === "selected_rows")
    && Array.isArray(value.caveats) && value.caveats.every((caveat) => isRecord(caveat) && typeof caveat.text === "string"
      && documentList(caveat.sources, (source) => typeof source.reference === "string"))
    && Array.isArray(value.issues) && value.issues.every((issue) => typeof issue === "string")
    && Array.isArray(value.rows) && value.rows.every((row) => isRecord(row) && HOLDING_STATUSES.has(String(row.status))
      && Array.isArray(row.entities) && row.entities.every((entity) => isRecord(entity) && typeof entity.kind === "string" && typeof entity.name === "string")
      && nullableString(row.account) && nullableString(row.isin) && nullableString(row.currency) && nullableString(row.difference) && nullableString(row.note)
      && isHoldingSide(row.earlier) && isHoldingSide(row.later));
}
