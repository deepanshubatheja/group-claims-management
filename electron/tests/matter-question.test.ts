import assert from "node:assert/strict";
import test from "node:test";
import { MAX_MATTER_QUESTION_CHARACTERS, unicodeCodePointCount } from "../src/ipc-contract";
import { isMatterDocumentScope, isMatterEntityInventory, isMatterHoldingsComparison, isMatterQuestionIntent, matterQuestionRequest } from "../src/matter-question";

const first = "11111111-1111-4111-8111-111111111111";
const second = "22222222-2222-4222-8222-222222222222";
const base = { matterId: "workspace", question: "Who joined the litigation?" };

test("accepts explicit all-document and selected-document scopes", () => {
  assert.deepEqual(matterQuestionRequest({ ...base, scope: { mode: "all" } }, false), { ...base, scope: { mode: "all" } });
  assert.deepEqual(
    matterQuestionRequest({ ...base, scope: { mode: "selected", documentIds: [first, second] } }, false),
    { ...base, scope: { mode: "selected", documentIds: [first, second] } },
  );
});

test("accepts multiline and Unicode Matter Review questions through the 5,000-code-point boundary", () => {
  const multiline = "1. Reconstruct the holdings.\n2. Identify transfers.\n3. Flag contradictions.";
  const unicodeAtLimit = "😀".repeat(MAX_MATTER_QUESTION_CHARACTERS);
  assert.equal(unicodeCodePointCount(unicodeAtLimit), MAX_MATTER_QUESTION_CHARACTERS);
  assert.deepEqual(
    matterQuestionRequest({ ...base, question: multiline, scope: { mode: "all" } }, false),
    { ...base, question: multiline, scope: { mode: "all" } },
  );
  assert.equal(matterQuestionRequest({ ...base, question: "a".repeat(MAX_MATTER_QUESTION_CHARACTERS - 1), scope: { mode: "all" } }, false)?.question.length, MAX_MATTER_QUESTION_CHARACTERS - 1);
  assert.equal(matterQuestionRequest({ ...base, question: unicodeAtLimit, scope: { mode: "all" } }, false)?.question, unicodeAtLimit);
  assert.equal(matterQuestionRequest({ ...base, question: "😀".repeat(MAX_MATTER_QUESTION_CHARACTERS + 1), scope: { mode: "all" } }, false), undefined);
  assert.equal(matterQuestionRequest({ ...base, question: "\n \t", scope: { mode: "all" } }, false), undefined);
});

test("rejects implicit, empty, widened, or malformed scopes", () => {
  const invalid: unknown[] = [
    { ...base, documentIds: [first] },
    { ...base },
    { ...base, scope: { mode: "selected", documentIds: [] } },
    { ...base, scope: { mode: "all", documentIds: [first] } },
    { ...base, scope: { mode: "selected", documentIds: [first, first] } },
    { ...base, scope: { mode: "selected", documentIds: ["../etc/passwd"] } },
    { ...base, scope: { mode: "everything" } },
    { ...base, matterId: "../other", scope: { mode: "all" } },
    { ...base, scope: { mode: "all" }, followUp: { question: "q", answer: "renderer-supplied answer" } },
    { ...base, scope: { mode: "all" }, followUpQuestionId: "not-a-uuid" },
  ];
  for (const payload of invalid) assert.equal(matterQuestionRequest(payload, false), undefined, JSON.stringify(payload));
});

test("a submission needs acknowledgement and a valid explicit consent list", () => {
  const request = { ...base, scope: { mode: "all" } };
  assert.equal(matterQuestionRequest({ ...request, consentedDocumentIds: [first] }, true), undefined);
  assert.equal(matterQuestionRequest({ ...request, acknowledged: true }, true), undefined);
  assert.equal(matterQuestionRequest({ ...request, acknowledged: true, consentedDocumentIds: ["x"] }, true), undefined);
  assert.equal(matterQuestionRequest({ ...request, acknowledged: true, consentedDocumentIds: [first, first] }, true), undefined);
  assert.equal(matterQuestionRequest({ ...request, consentedDocumentIds: [] }, false), undefined);
  assert.deepEqual(
    matterQuestionRequest({ ...request, acknowledged: true, consentedDocumentIds: [first], followUpQuestionId: second }, true),
    { ...request, followUpQuestionId: second, consentedDocumentIds: [first] },
  );
});

test("guards the backend document scope before it reaches the renderer", () => {
  const scope = {
    matterId: "workspace", mode: "all", documentsInScope: 2, searchableDocuments: 1, documentsWithEvidence: 1, searchTruncated: false,
    unavailableDocuments: [{ documentId: second, filename: "scan.pdf", reason: "no_searchable_text" }],
    incompleteDocuments: [],
    transmittedDocuments: [{ documentId: first, filename: "a.csv", previouslyAuthorized: false }],
    resolvedDocuments: [{ documentId: first, filename: "a.csv", matchedBy: "the file name" }], notes: [],
  };
  assert.equal(isMatterDocumentScope(scope), true);
  assert.equal(isMatterDocumentScope({ ...scope, notes: [1] }), false);
  assert.equal(isMatterDocumentScope({ ...scope, mode: "global" }), false);
  assert.equal(isMatterDocumentScope({ ...scope, unavailableDocuments: [{ documentId: second, filename: "x", reason: "other" }] }), false);
  assert.equal(isMatterDocumentScope({ ...scope, transmittedDocuments: [{ documentId: first, filename: "a.csv" }] }), false);
});

test("guards the local entity inventory and question intent", () => {
  const inventory = {
    requestedRoles: ["investor", "subfund"],
    entities: [{
      name: "Alpha Pension Fund", roles: ["investor"], related: [{ name: "Alpha Sub I", role: "subfund" }],
      evidence: [{ documentId: first, filename: "a.csv", sourceReference: "CSV!A2", evidenceSnippet: "Alpha Pension Fund", role: "investor", basis: "column heading", status: "document_label" }],
      documentCount: 1, mentionCount: 1, uncertainty: null,
    }],
    otherRoleCounts: { custodian: 1 },
    coverage: { eligibleDocuments: 24, searchedDocuments: 24, documentsWithRoleLabels: 3, documentsWithListedEntities: 2, partial: false, partialDocuments: [], entitiesOmitted: 0 },
    relationships: [{ documentId: first, filename: "a.xlsx", subject: "Alpha Sub I", subjectRole: "subfund", subjectReference: "Holdings!B2", related: "Alpha Pension Fund", relatedRole: "investor", relatedReference: "Holdings!A2", kind: "same_row", basis: "row 2" }],
  };
  assert.equal(isMatterEntityInventory(inventory), true);
  assert.equal(isMatterEntityInventory({ ...inventory, coverage: { ...inventory.coverage, partial: "no" } }), false);
  const [entity] = inventory.entities;
  assert.equal(isMatterEntityInventory({ ...inventory, entities: [{ ...entity, evidence: [{ ...entity!.evidence[0], status: "confirmed" }] }] }), false);
  assert.equal(isMatterQuestionIntent("inventory"), true);
  assert.equal(isMatterQuestionIntent("agent"), false);
});

test("guards a dated holdings comparison", () => {
  const side = {
    documentId: first, filename: "01.xlsx", sheet: "Holdings", row: 8, rowReference: "Holdings!A8:H8", quantity: "1000000", quantityReference: "Holdings!G8",
    isinReference: "Holdings!D8", accountReference: "Holdings!C8", entityReferences: ["Holdings!B8", "Holdings!B4"], unit: null, unitReference: null,
    currency: "USD", currencyReference: "Holdings!F8", rowDate: "2023-03-28 00:00:00", rowDateReference: "Holdings!A8",
    cells: [{ heading: "Eligible nominal amount", value: "1000000", reference: "Holdings!G8" }],
  };
  const table = {
    sheet: "Holdings", headerReference: "Holdings!A7:H7", recordReference: "Holdings!A8:H10", quantityHeading: "Eligible nominal amount",
    quantityHeadingReference: "Holdings!G7", quantityReference: "Holdings!G8:G10",
    investor: { name: "Northstar Investors LP", reference: "Holdings!B4", label: "Investor / legal entity", labelReference: "Holdings!A4" },
    statedDate: { date: "2023-03-28", text: "2023-03-28 00:00:00", reference: "Holdings!B3", labelReference: "Holdings!A3" },
    controlTotal: { reference: "Holdings!G12", label: "Control total", labelReference: "Holdings!A12", value: null, formula: "=SUM(G8:G10)", recordsTotal: "2000000", status: "formula_covers_records", note: "covers" },
  };
  const comparison = {
    documents: [{ documentId: first, filename: "01.xlsx", date: "2023-03-28", dateBasis: "file name", tables: [table] }], orderBasis: "file-name dates", issues: [],
    rows: [{ entities: [{ kind: "subfund", name: "Fund A" }], account: "AC-1001", isin: "XS1076957700", currency: "USD", earlier: side, later: { ...side, documentId: second }, difference: "-400000", status: "compared", note: null }],
    totals: { earlier: "1000000", later: "600000", difference: "-400000", currency: "USD", note: null },
    caveats: [{ text: "A holding movement does not establish a legal-right assignment.", sources: [{ documentId: first, filename: "01.xlsx", reference: "Holdings!A14" }] }],
    legalQuestionNotAnswered: false,
  };
  assert.equal(isMatterHoldingsComparison(comparison), true);
  assert.equal(isMatterHoldingsComparison({ ...comparison, rows: [{ ...comparison.rows[0], status: "currency_mismatch", difference: null }] }), true);
  const requestedEntities = [{ text: "Fund A", name: "Horizon Credit Fund A", match: "alias", candidates: [], positions: 1 }];
  const covered = { ...comparison, requestedEntities, totals: { ...comparison.totals, coverage: "all_records" } };
  assert.equal(isMatterHoldingsComparison(covered), true);
  assert.equal(isMatterHoldingsComparison({ ...covered, requestedEntities: [{ ...requestedEntities[0], match: "guessed" }] }), false);
  assert.equal(isMatterHoldingsComparison({ ...covered, totals: { ...covered.totals, coverage: "most" } }), false);
  assert.equal(isMatterHoldingsComparison({ ...comparison, documents: [{ ...comparison.documents[0], tables: [{ ...table, controlTotal: { ...table.controlTotal, reconciled: "yes" } }] }] }), false);
  assert.equal(isMatterHoldingsComparison({ ...comparison, rows: [{ ...comparison.rows[0], status: "sold" }] }), false);
  assert.equal(isMatterHoldingsComparison({ ...comparison, rows: [{ ...comparison.rows[0], difference: -400000 }] }), false);
  assert.equal(isMatterHoldingsComparison({ ...comparison, totals: { ...comparison.totals, earlier: 1000000 } }), false);
  assert.equal(isMatterHoldingsComparison({ ...comparison, caveats: [{ text: "x", sources: [{ documentId: first, filename: "01.xlsx" }] }] }), false);
  assert.equal(isMatterHoldingsComparison({ ...comparison, documents: [{ ...comparison.documents[0], tables: [{ ...table, controlTotal: { ...table.controlTotal, status: "ok" } }] }] }), false);
  assert.equal(isMatterQuestionIntent("holdings_comparison"), true);
  assert.equal(isMatterQuestionIntent("status"), false);
});
