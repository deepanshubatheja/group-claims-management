import assert from "node:assert/strict";
import test from "node:test";
import { aiConnectionMessage, isAvailablePage, platformName, recentDocuments, workspaceNotices } from "../src/components/workspace-ui.js";
import { analysisFailureMessage, analysisOverviewFromFindings, analysisValidationCounts, canStartDocumentAnalysis, findingFieldLabel, proposalTypeLabel, validationReasonLabel } from "../src/components/analysis-ui.js";

const document = (id: string, importedAt: string): DocumentSummary => ({ id, original_filename: `${id}.pdf`, file_size_bytes: 1, imported_at: importedAt, file_format: "pdf", processing_status: "parsed", processing_error: null, ai_review_status: "new" });

test("uses persisted document metadata for recent documents", () => {
  assert.deepEqual(recentDocuments([document("old", "2025-01-01T00:00:00Z"), document("new", "2026-01-01T00:00:00Z")]).map((item) => item.id), ["new", "old"]);
});
test("keeps unavailable workspace areas disabled and settings available", () => {
  assert.equal(isAvailablePage("Settings"), true);
  assert.equal(isAvailablePage("Holdings"), false);
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
test("requires both cloud and document authorization before analysis starts", () => {
  assert.equal(canStartDocumentAnalysis(false, true, true, true, null), false);
  assert.equal(canStartDocumentAnalysis(true, false, true, true, null), false);
  assert.equal(canStartDocumentAnalysis(true, true, false, true, null), false);
  assert.equal(canStartDocumentAnalysis(true, true, true, false, null), false);
  assert.equal(canStartDocumentAnalysis(true, true, true, true, "running"), false);
  assert.equal(canStartDocumentAnalysis(true, true, true, true, null), true);
  assert.match(analysisFailureMessage("AI_AUTHORIZATION_WITHDRAWN") ?? "", /withdrawn/);
  assert.match(analysisFailureMessage("AI_AUTHENTICATION_FAILED") ?? "", /rejected the configured API key/);
  assert.match(analysisFailureMessage("AI_AUTHENTICATION_FAILED") ?? "", /non-empty credential/);
  assert.match(analysisFailureMessage("AI_OUTPUT_LIMIT_REACHED") ?? "", /output limit/);
  assert.match(analysisFailureMessage("AI_INCOMPLETE") ?? "", /structured response/);
  assert.match(analysisFailureMessage("AI_RESPONSE_CONTRACT_INVALID") ?? "", /required structured result/);
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

test("creates a concise local overview only from evidence-validated findings", () => {
  const overview = analysisOverviewFromFindings([{
    proposalType: "account", fieldName: "account_number", value: "002-710093-06", unknown: false,
    sourceReference: "page 1 block 3", evidenceSnippet: "Brid LLC 002-710093-06",
  }]);
  assert.match(overview.text ?? "", /Account number: 002-710093-06/);
  assert.equal(overview.citations[0]?.sourceReference, "page 1 block 3");
  assert.equal(analysisOverviewFromFindings([{ proposalType: "account", fieldName: "account_number", value: null, unknown: true, sourceReference: "page 1 block 3", evidenceSnippet: null }]).text, null);
});
