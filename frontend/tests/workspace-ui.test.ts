import assert from "node:assert/strict";
import test from "node:test";
import { aiConnectionMessage, isAvailablePage, platformName, recentDocuments, workspaceNotices } from "../src/components/workspace-ui.js";
import { analysisFailureMessage, analysisOverviewFromFindings, analysisValidationCounts, canStartDocumentAnalysis, conversationContextAnswer, findingFieldLabel, proposalTypeLabel, validationReasonLabel } from "../src/components/analysis-ui.js";
import { candidateBusinessDate, candidateDetails, isCandidateBatchEligible, potentialHoldingConflict } from "../src/components/reconciliation-ui.js";
import { MAX_MATTER_QUESTION_CHARACTERS, unicodeCodePointCount } from "../src/components/matter-scope.js";

const document = (id: string, importedAt: string): DocumentSummary => ({ id, original_filename: `${id}.pdf`, file_size_bytes: 1, imported_at: importedAt, file_format: "pdf", processing_status: "parsed", processing_error: null, ai_review_status: "new" });

test("uses persisted document metadata for recent documents", () => {
  assert.deepEqual(recentDocuments([document("old", "2025-01-01T00:00:00Z"), document("new", "2026-01-01T00:00:00Z")]).map((item) => item.id), ["new", "old"]);
});
test("makes local review screens available while keeping unimplemented areas disabled", () => {
  assert.equal(isAvailablePage("Settings"), true);
  assert.equal(isAvailablePage("Holdings & History"), true);
  assert.equal(isAvailablePage("Issues"), true);
  assert.equal(isAvailablePage("Billing"), true);
  assert.equal(isAvailablePage("Reports"), false);
});
test("uses friendly platform labels and only genuine failure notices", () => {
  assert.equal(platformName("darwin"), "macOS");
  assert.deepEqual(workspaceNotices(true, true), []);
  assert.deepEqual(workspaceNotices(false, false), ["backend", "storage"]);
});
test("renders controlled connection-check outcomes without provider internals", () => {
  const checkedAt = "2026-10-15T00:00:00+00:00";
  assert.match(aiConnectionMessage({ outcome: "authentication_rejected", checkedAt }) ?? "", /rejected/);
  assert.match(aiConnectionMessage({ outcome: "permission_denied", checkedAt }) ?? "", /denied permission/);
  assert.match(aiConnectionMessage({ outcome: "rate_limited", checkedAt }) ?? "", /rate-limited/);
  assert.match(aiConnectionMessage({ outcome: "timeout", checkedAt }) ?? "", /timed out/);
  assert.match(aiConnectionMessage({ outcome: "provider_error", checkedAt }) ?? "", /could not complete/);
});
test("requires enabled Cloud AI, a configured provider, and a safe cost before analysis starts", () => {
  assert.equal(canStartDocumentAnalysis(false, true, true, null), false);
  assert.equal(canStartDocumentAnalysis(true, false, true, null), false);
  assert.equal(canStartDocumentAnalysis(true, true, false, null), false);
  assert.equal(canStartDocumentAnalysis(true, true, true, "running"), false);
  assert.equal(canStartDocumentAnalysis(true, true, true, null), true);
  assert.match(analysisFailureMessage("AI_AUTHORIZATION_WITHDRAWN") ?? "", /withdrawn/);
  assert.match(analysisFailureMessage("AI_AUTHENTICATION_FAILED") ?? "", /rejected the configured API key/);
  assert.match(analysisFailureMessage("AI_AUTHENTICATION_FAILED") ?? "", /non-empty credential/);
  assert.match(analysisFailureMessage("AI_OUTPUT_LIMIT_REACHED") ?? "", /output limit/);
  assert.match(analysisFailureMessage("AI_INCOMPLETE") ?? "", /structured response/);
  assert.match(analysisFailureMessage("AI_RESPONSE_CONTRACT_INVALID") ?? "", /required structured result/);
});
test("keeps conversation context available after an unsupported answer", () => {
  assert.equal(conversationContextAnswer(null, "No answer was established."), "No answer was established.");
  assert.equal(conversationContextAnswer("  A supported answer.  ", "No answer was established."), "A supported answer.");
});

test("counts Matter Review questions by Unicode code point for the visible character limit", () => {
  assert.equal(MAX_MATTER_QUESTION_CHARACTERS, 5_000);
  assert.equal(unicodeCodePointCount("1. Holdings\n2. Transfers"), 24);
  assert.equal(unicodeCodePointCount("😀".repeat(MAX_MATTER_QUESTION_CHARACTERS)), MAX_MATTER_QUESTION_CHARACTERS);
  assert.equal(unicodeCodePointCount("😀".repeat(MAX_MATTER_QUESTION_CHARACTERS + 1)) > MAX_MATTER_QUESTION_CHARACTERS, true);
});
test("keeps zero generated findings distinct from evidence rejections", () => {
  assert.deepEqual(analysisValidationCounts({ validation: {
    proposalsReturned: 0, schemaValid: 0, evidenceValidated: 0, persisted: 0, rejected: 0, rejectedByReason: {},
  } }), {
    available: true, proposalsReturned: 0, schemaValid: 0, evidenceValidated: 0, persisted: 0, rejected: 0, rejectedByReason: {},
  });
  const rejected = analysisValidationCounts({ validation: {
    proposalsReturned: 2, schemaValid: 2, evidenceValidated: 0, persisted: 0, rejected: 2,
    rejectedByReason: { source_reference_not_found: 2 },
  } });
  assert.equal(rejected.rejectedByReason.source_reference_not_found, 2);
  assert.equal(rejected.available, true);
  assert.equal(analysisValidationCounts({}).available, false);
  assert.equal(validationReasonLabel("source_reference_not_found"), "source reference not found");
});

test("uses lawyer-facing labels for provisional findings", () => {
  assert.equal(proposalTypeLabel("investment_vehicle_subfund"), "Investment vehicle / subfund");
  assert.equal(findingFieldLabel("accountNumber"), "Account number");
  assert.equal(findingFieldLabel("as_of_date"), "As-of date");
});

test("uses candidate business dates and never substitutes the document issue date", () => {
  const holding = { type: "holding_snapshot", documentIssueDate: "2025-02-02", payload: { positionDate: "2025-01-31" } };
  const transfer = { type: "securities_transfer", documentIssueDate: "2025-02-20", payload: { effectiveDate: "2025-02-15" } };
  const assignment = { type: "claim_assignment", documentIssueDate: "2025-02-21", payload: { effectiveDate: "2025-02-18" } };
  assert.deepEqual(candidateBusinessDate(holding), { label: "As of", value: "2025-01-31" });
  assert.deepEqual(candidateBusinessDate(transfer), { label: "Effective", value: "2025-02-15" });
  assert.deepEqual(candidateBusinessDate(assignment), { label: "Effective", value: "2025-02-18" });
  assert.deepEqual(candidateBusinessDate({ type: "holding_snapshot", documentIssueDate: "2025-02-02", payload: {} }), { label: "As of", value: "Date unknown" });
});

test("presents complete candidate details and excludes a pre-confirmation conflict from a routine batch", () => {
  const holding = {
    id: "candidate-a", type: "holding_snapshot", status: "candidate", validationStatus: "supported",
    payload: { investor: "Redwood Investors LP", subfund: "Horizon Credit Fund B (SF-B)", account: "CUST-ALPHA-02", isin: "XS0123456789", quantity: "1000000", positionDate: "2025-02-28" },
    potentialConflict: {
      type: "competing_dated_holding", account: "CUST-ALPHA-02", isin: "XS0123456789", asOfDate: "2025-02-28", difference: "100000",
      observations: [{ candidateId: "candidate-a", quantity: "1000000" }, { candidateId: "candidate-b", quantity: "900000" }],
    },
  };
  assert.deepEqual(candidateDetails(holding), [
    { label: "Investor", value: "Redwood Investors LP" }, { label: "Subfund", value: "Horizon Credit Fund B (SF-B)" },
    { label: "Custodian account", value: "CUST-ALPHA-02" }, { label: "ISIN", value: "XS0123456789" },
    { label: "Nominal amount", value: "1000000" }, { label: "As of", value: "2025-02-28" },
  ]);
  assert.equal(potentialHoldingConflict(holding)?.difference, "100000");
  assert.equal(isCandidateBatchEligible(holding), false);
  assert.equal(isCandidateBatchEligible({ ...holding, potentialConflict: null }), true);
  assert.deepEqual(candidateDetails({ type: "securities_transfer", payload: {
    fromSubfund: "Fund A", fromAccount: "A-1", toSubfund: "Fund B", toAccount: "B-1", isin: "XS0123456789", quantity: "200000", effectiveDate: "2025-02-15",
  } })[0], { label: "From", value: "Fund A / A-1" });
  assert.deepEqual(candidateDetails({ type: "claim_assignment", payload: {
    claimReference: "CR-2025-ALPHA", fromEntity: "Redwood", toEntity: "Northstar", percentage: "100", effectiveDate: "2025-02-18",
  } })[3], { label: "Assigned", value: "100%" });
  assert.equal(candidateDetails({ type: "claim_assignment", payload: {} })[0]?.value, "Unknown");
});

test("creates a concise local overview only from evidence-validated findings", () => {
  const overview = analysisOverviewFromFindings([{
    proposalType: "account", fieldName: "account_number", value: "002-710093-06", unknown: false,
    sourceReference: "page 1 block 3", evidenceSnippet: "Brid LLC 002-710093-06",
  }]);
  assert.match(overview.text ?? "", /Account number: 002-710093-06/);
  assert.equal(overview.citations[0]?.sourceReference, "page 1 block 3");
  assert.equal(analysisOverviewFromFindings([{ proposalType: "account", fieldName: "account_number", value: null, unknown: true, sourceReference: "page 1 block 3", evidenceSnippet: null }]).text, null);
});
