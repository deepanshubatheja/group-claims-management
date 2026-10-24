import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildAnswerView, formatMoney, formatPercent, type AnswerInput } from "../src/components/answer-presentation.js";
import { MatterAnswer } from "../src/components/MatterAnswer.js";
import { WORKSPACE_MATTER_ID } from "../src/components/matter-scope.js";

/* Synthetic fixtures for the renderer only; the values are test data, not billing results. */
const instruction = "44444444-4444-4444-8444-444444444444";
const holdings = "55555555-5555-4555-8555-555555555555";
const custodian = "66666666-6666-4666-8666-666666666666";
const INSTRUCTION_FILE = "06_approved_billing_instruction_2025-03-01.pdf";
const HOLDINGS_FILE = "04_later_holdings_2025-02-28.xlsx";
const CUSTODIAN_FILE = "05_contradictory_custodian_statement_2025-02-28.pdf";

const at = (documentId: string, filename: string, reference: string): MatterBillingSource => ({ documentId, filename, reference });
const fromInstruction = (reference: string) => at(instruction, INSTRUCTION_FILE, reference);

function participant(overrides: Partial<MatterBillingParticipant>): MatterBillingParticipant {
  return {
    name: "Fund", identifier: null, status: "eligible", eligibleAmount: null, share: null, sharePercent: null, allocatedAmount: null,
    unroundedAmount: null, roundingAdjustment: null, reason: null, source: null, ...overrides,
  };
}

function allocation(overrides: Partial<MatterBillingAllocation> = {}): MatterBillingAllocation {
  return {
    calculationStatus: "complete", resultType: "provisional_allocation", invoiceStatus: "not_issued", issuanceBlocked: true,
    instruction: { documentId: instruction, filename: INSTRUCTION_FILE },
    approvedBy: { text: "Example billing committee", source: fromInstruction("page 1 block 1") },
    investorGroup: null,
    period: { text: "1 February 2025 to 28 February 2025", source: fromInstruction("page 1 block 1") },
    currency: "USD",
    feePool: { amount: "120000.00", source: fromInstruction("page 1 block 2") },
    rule: { text: "eligible nominal amount / total eligible nominal amount", formula: "eligible nominal amount ÷ total eligible nominal amount × fee pool", source: fromInstruction("page 1 block 3") },
    roundingPolicy: "Truncate to the cent; leftover cents go to the largest remainders.",
    totals: { eligibleAmount: "1400000", allocatedAmount: "120000.00", remainder: "0.00", sharePercent: "100.0000" },
    participants: [
      participant({ name: "Fund A", identifier: "SF-A", eligibleAmount: "400000", share: "0.2857142857", sharePercent: "28.5714", allocatedAmount: "34285.71", unroundedAmount: "34285.714286", roundingAdjustment: "0.00", source: fromInstruction("page 1 block 4") }),
      participant({ name: "Fund B", identifier: "SF-B", eligibleAmount: "1000000", share: "0.7142857143", sharePercent: "71.4286", allocatedAmount: "85714.29", unroundedAmount: "85714.285714", roundingAdjustment: "0.01", source: fromInstruction("page 1 block 5") }),
      participant({ name: "Fund C", identifier: "SF-C", status: "excluded", share: "0", sharePercent: "0", allocatedAmount: "0.00", reason: "it is not participating in the relevant litigation", source: fromInstruction("page 1 block 6") }),
    ],
    crossChecks: [
      { participant: "Fund B", identifier: "SF-B", status: "consistent", instructionAmount: "1000000", recordedAmount: "1000000", asOfDate: "2025-02-28", source: at(holdings, HOLDINGS_FILE, "Holdings!G9") },
      { participant: "Fund B", identifier: "SF-B", status: "conflict", instructionAmount: "1000000", recordedAmount: "900000", asOfDate: "2025-02-28", source: at(custodian, CUSTODIAN_FILE, "page 1 block 2") },
    ],
    crossCheck: { comparisonDate: "2025-02-28", dateBasis: "the billing-period end", documentsChecked: 3, limitations: [] },
    missing: [], issues: [],
    ...overrides,
  };
}

const PLAIN_TEXT = "Using the approved instruction, the provisional allocation of USD 120,000.00 is Fund A (SF-A): USD 34,285.71; Fund B (SF-B): USD 85,714.29.";

function billingInput(overrides: Partial<MatterBillingAllocation> = {}, input: Partial<AnswerInput> = {}): AnswerInput {
  return {
    answer: PLAIN_TEXT, unknown: false, intent: "calculation", model: "local-document-calculation",
    documentScope: {
      matterId: WORKSPACE_MATTER_ID, mode: "all", documentsInScope: 6, searchableDocuments: 6, documentsWithEvidence: 3,
      unavailableDocuments: [], incompleteDocuments: [], transmittedDocuments: [], searchTruncated: false, resolvedDocuments: [], notes: [],
    },
    citations: [
      { documentId: instruction, filename: INSTRUCTION_FILE, sourceReference: "page 1 block 2", evidenceSnippet: "Fee pool USD 120,000.00" },
      { documentId: custodian, filename: CUSTODIAN_FILE, sourceReference: "page 1 block 2", evidenceSnippet: "900,000 nominal units" },
    ],
    billingAllocation: allocation(overrides),
    ...input,
  };
}

const render = (input: AnswerInput) => renderToStaticMarkup(createElement(MatterAnswer, { input, onOpenSource: () => undefined }));
const visible = (markup: string) => markup.split('<details class="answer-details')[0];
const technical = (markup: string) => markup.split('<details class="answer-details technical">')[1] ?? "";
const count = (text: string, part: string) => text.split(part).length - 1;

test("a billing allocation renders heading, context, status, metrics, table, formula and warnings in order", () => {
  const markup = render(billingInput());
  const order = [
    "Pro-rata billing allocation", "Billing period: 1 February 2025 to 28 February 2025", "Provisional allocation · not an invoice",
    "Fee pool", "Pro-rata billing allocation table", "Each eligible subfund receives", "Unresolved discrepancy", "Supporting evidence", "Technical details",
  ].map((text) => markup.indexOf(text));
  assert.ok(order.every((position) => position >= 0), String(order));
  assert.deepEqual([...order].sort((a, b) => a - b), order);
  assert.match(markup, /<th scope="col">Subfund<\/th><th scope="col">Eligibility<\/th><th scope="col" class="numeric">Eligible nominal amount<\/th><th scope="col" class="numeric">Allocation<\/th><th scope="col" class="numeric">Pro-rata billing amount<\/th>/);
});

test("billing amounts carry the currency and cents; nominal amounts never do", () => {
  assert.equal(formatMoney("120000", "USD"), "USD 120,000.00");
  assert.equal(formatMoney("34285.71", "EUR"), "EUR 34,285.71");
  assert.equal(formatMoney("0.5", "USD"), "USD 0.50");
  // Never rounded: more precise stored values are shown as stored.
  assert.equal(formatMoney("10.125", "USD"), "USD 10.125");
  const markup = visible(render(billingInput()));
  assert.match(markup, /<td class="numeric amount">USD 34,285\.71/);
  assert.match(markup, /<td class="numeric amount">USD 85,714\.29/);
  assert.match(markup, /<td class="numeric">400,000<\/td>/);
  assert.match(markup, /<td class="numeric">1,000,000<\/td>/);
  assert.doesNotMatch(markup, /USD 400,000|USD 1,400,000/);
  assert.match(markup, /Eligible nominal base<\/dt><dd>1,400,000<small>Nominal amount, not a fee<\/small>/);
});

test("percentages come from the backend's display value and the total row shows 100%", () => {
  assert.equal(formatPercent("28.5714"), "28.5714%");
  assert.equal(formatPercent("100.0000"), "100%");
  assert.equal(formatPercent("0"), "0%");
  const markup = render(billingInput());
  assert.match(markup, /<td class="numeric">28\.5714%<\/td>/);
  assert.match(markup, /<tfoot><tr><th scope="row">Total<\/th><td><\/td><td class="numeric">1,400,000<\/td><td class="numeric">100%<\/td><td class="numeric amount">USD 120,000\.00<\/td>/);
  // The exact share stays available in technical details.
  assert.match(technical(markup), /0\.7142857143/);
  assert.match(technical(markup), /85714\.285714/);
});

test("an explicitly excluded subfund is listed with its reason, a zero bill and its citation", () => {
  const view = buildAnswerView(billingInput());
  const row = view.billing?.rows.find((item) => item.identifier === "SF-C");
  assert.equal(row?.statusLabel, "Excluded");
  assert.equal(row?.amount, "USD 0.00");
  assert.equal(row?.share, "0%");
  assert.equal(row?.eligible, "—");
  assert.equal(row?.note, "Stated reason: it is not participating in the relevant litigation");
  assert.match(render(billingInput()), /aria-label="Open source page 1 block 6 in 06_approved_billing_instruction_2025-03-01\.pdf"/);
  assert.match(view.conclusion ?? "", /1 further subfund was explicitly excluded by the instruction/);
});

test("a subfund with unknown eligibility is undetermined, never billed zero", () => {
  const extra = participant({
    name: "Fund D", identifier: "SF-D", status: "undetermined",
    reason: "Listed in the documents in scope but not named in the billing instruction, so its eligibility is not established.",
    source: at(holdings, HOLDINGS_FILE, "Holdings!C11"),
  });
  const input = billingInput({ participants: [...allocation().participants, extra] });
  const row = buildAnswerView(input).billing?.rows.find((item) => item.identifier === "SF-D");
  assert.equal(row?.statusLabel, "Undetermined");
  assert.equal(row?.amount, "Undetermined");
  assert.equal(row?.share, "Undetermined");
  const markup = visible(render(input));
  assert.match(markup, /Fund D<small>SF-D<\/small><\/th><td><span class="status-pill undetermined">Undetermined<\/span>/);
  assert.match(markup, /not named in the billing instruction/);
  assert.match(buildAnswerView(input).conclusion ?? "", /The eligibility of 1 other listed subfund is undetermined/);
});

test("missing instruction inputs give an incomplete answer without totals or amounts", () => {
  const input = billingInput({
    calculationStatus: "incomplete", feePool: null, currency: null, totals: null, participants: [], crossChecks: [],
    missing: ["the fee pool amount and currency"], issuanceBlocked: true,
  }, { answer: "No allocation was calculated: the billing instruction does not state the fee pool amount and currency." });
  const view = buildAnswerView(input);
  assert.equal(view.conclusion, "No allocation was calculated.");
  assert.equal(view.billing?.totals, null);
  assert.deepEqual(view.billing?.metrics, []);
  assert.equal(view.method, "Not calculated · inputs missing from the instruction");
  const markup = visible(render(input));
  assert.match(markup, /Incomplete · no allocation calculated/);
  assert.match(markup, /Not stated in the instruction: the fee pool amount and currency\./);
  assert.doesNotMatch(markup, /USD|Total allocated|<tfoot>/);
});

test("a reconciled allocation says so and hides a zero remainder", () => {
  const input = billingInput({ crossChecks: [], issuanceBlocked: false });
  const view = buildAnswerView(input);
  assert.deepEqual(view.billing?.metrics.map((metric) => metric.label), ["Fee pool", "Eligible nominal base", "Total allocated"]);
  assert.equal(view.billing?.metrics[2].detail, "Reconciles to the fee pool");
  assert.equal(view.conclusion, "USD 120,000.00 was allocated across 2 eligible subfunds. 1 further subfund was explicitly excluded by the instruction. The allocation reconciles to the fee pool.");
  assert.doesNotMatch(render(input), /Unallocated remainder|Do not issue invoices/);
});

test("rounding cents and a non-zero remainder are visible", () => {
  const view = buildAnswerView(billingInput({ totals: { eligibleAmount: "1400000", allocatedAmount: "119999.99", remainder: "0.01", sharePercent: "100.0000" } }));
  assert.deepEqual(view.billing?.metrics.at(-1), { label: "Unallocated remainder", value: "USD 0.01" });
  assert.match(view.conclusion ?? "", /USD 0\.01 of the fee pool is unallocated/);
  assert.equal(view.billing?.rows.find((row) => row.identifier === "SF-B")?.adjustment, "+0.01 rounding");
  assert.equal(view.billing?.rows.find((row) => row.identifier === "SF-A")?.adjustment, null);
});

test("a material conflict stays visible, keeps the instruction amount and blocks invoicing", () => {
  const markup = render(billingInput());
  const shown = visible(markup);
  assert.match(shown, /<section class="answer-discrepancies" aria-label="Unresolved discrepancies">/);
  assert.match(shown, /Fund B \(SF-B\): the instruction&#x27;s eligible amount is 1,000,000, but contradictory custodian statement 28 Feb 2025 records 900,000 as of 28 Feb 2025\. The instruction amount was used unchanged; the difference is unresolved\./);
  assert.match(shown, /later holdings 28 Feb 2025 records 1,000,000, matching the instruction\./);
  assert.match(shown, /Do not issue invoices from this allocation until the discrepancy is resolved\./);
  assert.match(shown, /<span class="status-pill conflict">Discrepancy<\/span>/);
  assert.match(buildAnswerView(billingInput()).conclusion ?? "", /but an unresolved holdings discrepancy affects the eligible amounts used\.$/);
  // The billed amount is the calculated one, not a substitute.
  assert.match(shown, /USD 85,714\.29/);
});

test("every input, exclusion and discrepancy has an exact, openable source", () => {
  const markup = render(billingInput());
  for (const [reference, filename] of [
    ["page 1 block 2", INSTRUCTION_FILE], ["page 1 block 3", INSTRUCTION_FILE], ["page 1 block 4", INSTRUCTION_FILE],
    ["page 1 block 5", INSTRUCTION_FILE], ["page 1 block 6", INSTRUCTION_FILE], ["page 1 block 2", CUSTODIAN_FILE], ["Holdings!G9", HOLDINGS_FILE],
  ]) {
    assert.match(markup, new RegExp(`aria-label="Open source ${reference.replace(/[.!]/g, "\\$&")} in ${filename.replace(/\./g, "\\.")}"`), `${filename} ${reference}`);
  }
});

test("the table is not repeated as prose; the plain-text summary stays in technical details", () => {
  const markup = render(billingInput());
  assert.equal(count(visible(markup), "USD 34,285.71"), 1);
  assert.doesNotMatch(visible(markup), /Using the approved instruction/);
  assert.match(technical(markup), /Using the approved instruction/);
});

test("the allocation table scrolls horizontally on narrow screens", () => {
  assert.match(render(billingInput()), /<div class="answer-table" role="region" aria-label="Pro-rata billing allocation table" tabindex="0"><table class="billing-table">/);
  const css = readFileSync(new URL("../../src/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.answer-table \{ overflow-x: auto;/);
  assert.match(css, /\.answer-table \.numeric \{ text-align: right;/);
});

test("long allocations show the first rows with a control for the rest; currency follows the data", () => {
  const participants = Array.from({ length: 300 }, (_unused, index) => participant({
    name: `Vehicle ${index + 1}`, identifier: `V-${index + 1}`, eligibleAmount: "1", share: "0.0033333333", sharePercent: "0.3333",
    allocatedAmount: index < 100 ? "3.34" : "3.33", source: fromInstruction(`page 2 block ${index}`),
  }));
  const markup = render(billingInput({ currency: "EUR", participants, crossChecks: [], totals: { eligibleAmount: "300", allocatedAmount: "1000.00", remainder: "0.00", sharePercent: "100.0000" } }));
  assert.equal(count(markup, '<tr class="billing-row eligible">'), 50);
  assert.match(markup, /Show all 300 subfunds/);
  const tableMarkup = markup.split('<table class="billing-table">')[1].split("</table>")[0];
  assert.match(tableMarkup, /EUR 3\.34/);
  assert.doesNotMatch(tableMarkup, /USD/);
  assert.match(markup, /EUR 1,000\.00 was allocated across 300 eligible subfunds/);
});

test("earlier calculation answers without a structured allocation still render as text", () => {
  const input = billingInput({}, { billingAllocation: undefined, answer: "The provisional allocation of USD 100.00 is Fund A: USD 100.00." });
  const view = buildAnswerView(input);
  assert.equal(view.layout, "prose");
  assert.equal(view.billing, null);
  assert.match(render(input), /The provisional allocation of USD 100\.00 is Fund A: USD 100\.00\./);
});

test("the production renderer holds no synthetic matter values", () => {
  for (const file of ["answer-presentation.ts", "MatterAnswer.tsx"]) {
    const source = readFileSync(new URL(`../../src/components/${file}`, import.meta.url), "utf8");
    for (const value of ["Redwood", "Horizon", "Meridian", "SF-A", "SF-B", "120,000", "120000", "34285", "85714", "900,000", "1,000,000"]) {
      assert.ok(!source.includes(value), `${file} contains ${value}`);
    }
  }
});
