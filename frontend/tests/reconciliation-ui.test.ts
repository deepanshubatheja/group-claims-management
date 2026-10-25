import assert from "node:assert/strict";
import test from "node:test";
import { identityQuestionPrompt, rereadSummary } from "../src/components/reconciliation-ui.js";

test("describes a same-as question in plain terms", () => {
  const prompt = identityQuestionPrompt({
    id: "11111111-1111-4111-8111-111111111111",
    targetType: "investment_vehicle",
    observedText: "Horizon Credit Fund A",
    matchBasis: "same_account_reference",
    suggested: { label: "Horizon Credit Fund A (SF-A)", aliases: [{ text: "Horizon Credit Fund A (SF-A)" }, { text: "HORIZON CREDIT FUND A (SF-A)" }, { text: "Horizon Credit Fund A (SF-A)" }] },
  });
  assert.deepEqual(prompt, {
    id: "11111111-1111-4111-8111-111111111111",
    recordKind: "Subfund",
    observedText: "Horizon Credit Fund A",
    suggestedLabel: "Horizon Credit Fund A (SF-A)",
    basis: "The document uses a known account number but names it differently.",
    knownAliases: ["Horizon Credit Fund A (SF-A)", "HORIZON CREDIT FUND A (SF-A)"],
  });
});

test("ignores malformed questions", () => {
  assert.equal(identityQuestionPrompt({ id: "x", observedText: "Alpha" }), null);
  assert.equal(identityQuestionPrompt({ suggested: { label: "Alpha" } }), null);
});

test("summarises a document re-read", () => {
  assert.equal(rereadSummary({ documentsRead: 6, documentsFailed: 0, documentsWithNewCandidates: 0, newCandidates: 0, truncated: false }), "Re-read 6 documents. No new records found.");
  assert.equal(
    rereadSummary({ documentsRead: 1, documentsFailed: 1, documentsWithNewCandidates: 1, newCandidates: 2, truncated: true }),
    "Re-read 1 document. 2 new records from 1 document ready for review. 1 could not be read. Only the first 500 imported documents were re-read.",
  );
});
