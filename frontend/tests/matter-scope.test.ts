import assert from "node:assert/strict";
import test from "node:test";
import {
  answerScopeLabel, canFollowUpInScope, citationsByDocument, composerScopeSummary, DEFAULT_CHAT_SCOPE,
  controlTotalLabel, documentsNeedingConsent, entityRoleLabel, groupedQuantity, holdingStatusLabel, inventoryCoverageSummary, questionIntentLabel, questionScope, relationshipKindLabel,
  scopeSubmissionBlock, sourceLocationRequest, WORKSPACE_MATTER_ID,
} from "../src/components/matter-scope.js";

const first = "11111111-1111-4111-8111-111111111111";
const second = "22222222-2222-4222-8222-222222222222";
const document = (id: string, status: DocumentSummary["processing_status"] = "parsed"): DocumentSummary => ({
  id, original_filename: `${id}.pdf`, file_size_bytes: 1, imported_at: "2026-01-01T00:00:00Z", file_format: "pdf",
  processing_status: status, processing_error: null, ai_review_status: "new",
});
const scope = (overrides: Partial<MatterDocumentScope> = {}): MatterDocumentScope => ({
  matterId: WORKSPACE_MATTER_ID, mode: "all", documentsInScope: 3, searchableDocuments: 2, documentsWithEvidence: 2,
  unavailableDocuments: [], incompleteDocuments: [], searchTruncated: false, resolvedDocuments: [], notes: [],
  transmittedDocuments: [
    { documentId: first, filename: "a.csv", previouslyAuthorized: true },
    { documentId: second, filename: "b.pdf", previouslyAuthorized: false },
  ],
  ...overrides,
});

test("a matter starts with all documents in scope and needs no selection for a general question", () => {
  assert.equal(DEFAULT_CHAT_SCOPE, "all");
  assert.deepEqual(questionScope(DEFAULT_CHAT_SCOPE, []), { mode: "all" });
  assert.equal(scopeSubmissionBlock(DEFAULT_CHAT_SCOPE, [], 4), null);
  // Selected IDs do not leak into an all-document request.
  assert.deepEqual(questionScope("all", [first]), { mode: "all" });
});

test("selected mode uses exactly the selected documents", () => {
  assert.deepEqual(questionScope("selected", [first]), { mode: "selected", documentIds: [first] });
  assert.deepEqual(questionScope("selected", [first, second]), { mode: "selected", documentIds: [first, second] });
});

test("an empty selected scope blocks submission instead of widening to all documents", () => {
  assert.equal(questionScope("selected", []), null);
  assert.match(scopeSubmissionBlock("selected", [], 4) ?? "", /no documents are selected/);
  assert.match(scopeSubmissionBlock("all", [], 0) ?? "", /Import documents/);
});

test("the composer and each answer show the scope used", () => {
  assert.equal(composerScopeSummary("all", 0, [document(first), document(second, "imported")]), "All documents in this matter · 2 documents in scope (1 not yet parsed and cannot be searched)");
  assert.equal(composerScopeSummary("selected", 1, [document(first)]), "Selected documents only · 1 document in scope");
  assert.equal(answerScopeLabel(scope()), "All documents in this matter · 2 of 3 documents searchable · evidence from 2");
  assert.equal(answerScopeLabel(scope({ mode: "selected", documentsInScope: 1, searchableDocuments: 1, documentsWithEvidence: 1 })), "Selected documents only · 1 document · evidence from 1");
  // A question naming documents narrows, and the label says so rather than implying all were used.
  const named = [{ documentId: first, filename: "01_Holdings_2023-03-28.xlsx", matchedBy: "the date" }, { documentId: second, filename: "02_Holdings_2023-04-30.xlsx", matchedBy: "the date" }];
  assert.equal(answerScopeLabel(scope({ documentsInScope: 5, searchableDocuments: 5, resolvedDocuments: named })), "All documents in this matter · 5 documents · narrowed by the question to 2 named documents · evidence from 2");
});

test("consent is requested only for documents not previously authorized", () => {
  assert.deepEqual(documentsNeedingConsent(scope()).map((item) => item.documentId), [second]);
});

test("a follow-up is offered only when the earlier answer's documents stay in scope", () => {
  assert.equal(canFollowUpInScope(scope(), "all", []), true);
  assert.equal(canFollowUpInScope(scope(), "selected", [first, second]), true);
  assert.equal(canFollowUpInScope(scope(), "selected", [second]), false);
});

test("multi-document citations stay grouped by their supporting document", () => {
  const groups = citationsByDocument([
    { documentId: first, filename: "a.csv", sourceReference: "CSV!A2", evidenceSnippet: "100" },
    { documentId: second, filename: "b.pdf", sourceReference: "page 1 block 3", evidenceSnippet: "60" },
    { documentId: first, filename: "a.csv", sourceReference: "CSV!B2", evidenceSnippet: "XS0000000001" },
  ]);
  assert.deepEqual(groups.map((group) => [group.filename, group.citations.map((citation) => citation.sourceReference)]), [
    ["a.csv", ["CSV!A2", "CSV!B2"]],
    ["b.pdf", ["page 1 block 3"]],
  ]);
});

test("citations map to the viewer request for their page or cell", () => {
  assert.deepEqual(sourceLocationRequest("page 4 block 2"), { pageNumber: 4 });
  assert.deepEqual(sourceLocationRequest("Holdings!C12"), { sheetName: "Holdings", rowStart: 9, rowLimit: 10, columnStart: 1, columnLimit: 10 });
  assert.deepEqual(sourceLocationRequest("CSV!A1"), { sheetName: "CSV", rowStart: 1, rowLimit: 10, columnStart: 1, columnLimit: 10 });
  assert.equal(sourceLocationRequest("an entire document"), null);
});

test("inventory answers label roles conservatively and state coverage", () => {
  assert.equal(entityRoleLabel("subfund"), "Subfund");
  assert.equal(entityRoleLabel("investor_or_legal_entity"), "Investor or legal entity");
  assert.equal(entityRoleLabel("made_up_role"), "Unknown role");
  assert.match(questionIntentLabel("inventory"), /searched locally/);
  const coverage = { eligibleDocuments: 24, searchedDocuments: 24, documentsWithRoleLabels: 5, documentsWithListedEntities: 4, partial: false, partialDocuments: [], entitiesOmitted: 0 };
  const inventory = { requestedRoles: ["investor"], entities: [], otherRoleCounts: {}, coverage, relationships: [] };
  assert.equal(inventoryCoverageSummary(inventory), "Searched all 24 searchable documents; 4 contain listed entities.");
  assert.match(inventoryCoverageSummary({ ...inventory, coverage: { ...coverage, partial: true, partialDocuments: [{ documentId: first, filename: "big.xlsx" }] } }), /Partial: 1 document exceeded/);
});

test("relationship and holdings labels never overstate the evidence", () => {
  assert.match(relationshipKindLabel("same_row"), /not a documented relationship/);
  assert.match(relationshipKindLabel("explicit_statement"), /Documented/);
  assert.match(holdingStatusLabel("unit_mismatch"), /not compared/);
  assert.match(holdingStatusLabel("duplicate_rows"), /not summed/);
  assert.match(questionIntentLabel("holdings_comparison"), /calculated locally/);
  assert.match(questionIntentLabel("holdings_reconstruction"), /reconstruction \(calculated locally\)/);
  assert.match(holdingStatusLabel("currency_mismatch"), /not compared/);
  assert.match(controlTotalLabel("formula_covers_records"), /exactly the record rows/);
});

test("holdings quantities are grouped exactly, without float conversion", () => {
  assert.equal(groupedQuantity("1000000"), "1,000,000");
  assert.equal(groupedQuantity("-400000"), "-400,000");
  assert.equal(groupedQuantity("+400000"), "+400,000");
  assert.equal(groupedQuantity("0.0"), "0");
  assert.equal(groupedQuantity("12345678901234567890.25"), "12,345,678,901,234,567,890.25");
  assert.equal(groupedQuantity("1.000 (ambiguous)"), "1.000 (ambiguous)");
  assert.equal(groupedQuantity(null), "—");
});
