import assert from "node:assert/strict";
import test from "node:test";
import { createElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  buildAnswerView, formatIsoDate, formatQuantity, groupEvidence, proseBlocks, quantityPlaces, shortDocumentLabel, type AnswerInput,
} from "../src/components/answer-presentation.js";
import { FailedAnswer, MatterAnswer, PendingAnswer } from "../src/components/MatterAnswer.js";
import { WORKSPACE_MATTER_ID } from "../src/components/matter-scope.js";

const march = "11111111-1111-4111-8111-111111111111";
const april = "22222222-2222-4222-8222-222222222222";
const pdf = "33333333-3333-4333-8333-333333333333";
const MARCH_FILE = "01_Holdings_2023-03-28.xlsx";
const APRIL_FILE = "02_Holdings_2023-04-30.xlsx";
const CAVEAT = "Purpose: compare dated positions. A security holding movement is a factual observation only; it does not by itself establish a legal-right assignment, litigation status, or billing entitlement.";

const scope = (overrides: Partial<MatterDocumentScope> = {}): MatterDocumentScope => ({
  matterId: WORKSPACE_MATTER_ID, mode: "all", documentsInScope: 5, searchableDocuments: 5, documentsWithEvidence: 2,
  unavailableDocuments: [], incompleteDocuments: [], transmittedDocuments: [], searchTruncated: false,
  resolvedDocuments: [
    { documentId: march, filename: MARCH_FILE, matchedBy: "the date 28 March 2023 in its file name" },
    { documentId: april, filename: APRIL_FILE, matchedBy: "the date 30 April 2023 in its file name" },
  ],
  notes: [], ...overrides,
});

function side(documentId: string, filename: string, row: number, quantity: string): MatterHoldingSide {
  return {
    documentId, filename, sheet: "Holdings", row, rowReference: `Holdings!A${row}:H${row}`, quantity, quantityReference: `Holdings!G${row}`,
    isinReference: `Holdings!D${row}`, accountReference: `Holdings!C${row}`, entityReferences: [`Holdings!B${row}`, "Holdings!B4"],
    unit: null, unitReference: null, currency: "USD", currencyReference: `Holdings!F${row}`, rowDate: "2023-03-28 00:00:00",
    rowDateReference: `Holdings!A${row}`, cells: [{ heading: "Eligible nominal amount", value: quantity, reference: `Holdings!G${row}` }],
  };
}

function holdingRow(subfund: string, account: string, row: number, earlier: string, later: string, difference: string): MatterHoldingRow {
  return {
    entities: [{ kind: "investor", name: "Northstar Investors LP" }, { kind: "subfund", name: subfund }], account, isin: "XS1076957700",
    currency: "USD", earlier: side(march, MARCH_FILE, row, earlier), later: side(april, APRIL_FILE, row, later), difference, status: "compared", note: null,
  };
}

function table(): MatterHoldingsTable {
  return {
    sheet: "Holdings", headerReference: "Holdings!A7:H7", recordReference: "Holdings!A8:H10", quantityHeading: "Eligible nominal amount",
    quantityHeadingReference: "Holdings!G7", quantityReference: "Holdings!G8:G10",
    investor: { name: "Northstar Investors LP", reference: "Holdings!B4", label: "Investor / legal entity", labelReference: "Holdings!A4" },
    statedDate: { date: "2023-03-28", text: "2023-03-28 00:00:00", reference: "Holdings!B3", labelReference: "Holdings!A3" },
    controlTotal: {
      reference: "Holdings!G12", label: "Control total eligible nominal amount", labelReference: "Holdings!A12", value: null, formula: "=SUM(G8:G10)",
      recordsTotal: "2000000", status: "formula_covers_records", note: "Holdings!G12 holds the formula =SUM(G8:G10) with no stored result.",
    },
  };
}

function comparison(overrides: Partial<MatterHoldingsComparison> = {}): MatterHoldingsComparison {
  return {
    documents: [
      { documentId: march, filename: MARCH_FILE, date: "2023-03-28", dateBasis: "file name", tables: [table()] },
      { documentId: april, filename: APRIL_FILE, date: "2023-04-30", dateBasis: "file name", tables: [table()] },
    ],
    orderBasis: "file-name dates",
    rows: [
      holdingRow("Horizon Credit Fund A", "AC-1001", 8, "1000000", "600000", "-400000"),
      holdingRow("Horizon Credit Fund B", "AC-2001", 9, "600000", "1000000", "+400000"),
      holdingRow("Meridian Income Fund C", "AC-3001", 10, "400000", "400000", "0"),
    ],
    totals: { earlier: "2000000", later: "2000000", difference: "0", currency: "USD", note: null },
    issues: [],
    caveats: [{ text: CAVEAT, sources: [
      { documentId: march, filename: MARCH_FILE, reference: "Holdings!A14" },
      { documentId: april, filename: APRIL_FILE, reference: "Holdings!A14" },
    ] }],
    legalQuestionNotAnswered: false,
    ...overrides,
  };
}

const citation = (documentId: string, filename: string, sourceReference: string, evidenceSnippet = "x"): MatterQuestionCitation => ({ documentId, filename, sourceReference, evidenceSnippet });

const PLAIN_TEXT = "Compared 01_Holdings_2023-03-28.xlsx (2023-03-28) with 02_Holdings_2023-04-30.xlsx (2023-04-30). Horizon Credit Fund A / AC-1001: 1,000,000 → 600,000 (change -400,000).";

function holdingsInput(overrides: Partial<AnswerInput> = {}): AnswerInput {
  return {
    answer: PLAIN_TEXT, unknown: false, intent: "holdings_comparison", model: "local-holdings-comparison",
    documentScope: scope(), inventory: null, holdingsComparison: comparison(),
    citations: [
      citation(march, MARCH_FILE, "Holdings!G8", "1000000"), citation(march, MARCH_FILE, "Holdings!B8", "Horizon Credit Fund A"),
      citation(march, MARCH_FILE, "Holdings!G9", "600000"), citation(april, APRIL_FILE, "Holdings!G8", "600000"),
      citation(march, MARCH_FILE, "Holdings!A14", CAVEAT), citation(april, APRIL_FILE, "Holdings!A14", CAVEAT),
    ],
    ...overrides,
  };
}

const render = (input: AnswerInput, onOpenSource?: (documentId: string, reference: string) => void) =>
  renderToStaticMarkup(createElement(MatterAnswer, { input, onOpenSource }));
/** The always-visible part of an answer: everything before the collapsible sections. */
const visible = (markup: string) => markup.split('<details class="answer-details')[0];
const technicalDetails = (markup: string) => markup.split('<details class="answer-details technical">')[1] ?? "";

/** Expand hook-free function components so button handlers can be invoked without a DOM. */
function buttons(node: ReactNode): ReactElement<{ onClick?: () => void; "aria-label"?: string }>[] {
  if (Array.isArray(node)) return node.flatMap(buttons);
  if (!isValidElement(node)) return [];
  const element = node as ReactElement<Record<string, unknown>>;
  if (typeof element.type === "function") return buttons((element.type as (props: unknown) => ReactNode)(element.props));
  const own = element.type === "button" ? [element as ReactElement<{ onClick?: () => void; "aria-label"?: string }>] : [];
  return [...own, ...buttons(element.props.children as ReactNode)];
}

test("a holdings comparison leads with a conclusion, summary metrics, and one table", () => {
  const view = buildAnswerView(holdingsInput());
  assert.equal(view.layout, "holdings");
  assert.equal(view.title, "Holdings comparison");
  assert.equal(view.conclusion, "Between 28 Mar 2023 and 30 Apr 2023, 2 of 3 matched positions changed; the net difference across them is 0.");
  assert.deepEqual(view.holdings?.metrics.map((metric) => [metric.label, metric.value, metric.detail]), [
    ["Recorded total · 28 Mar 2023", "2,000,000", "USD"], ["Recorded total · 30 Apr 2023", "2,000,000", "USD"],
    ["Net difference", "0", "USD"], ["Matched positions", "3", "2 changed"],
  ]);
  assert.equal(view.holdings?.investor, "Northstar Investors LP");
  assert.equal(view.holdings?.isin, "XS1076957700");
  assert.equal(view.holdings?.showIsinColumn, false);
  const markup = render(holdingsInput());
  const body = visible(markup);
  assert.match(body, /<th scope="colgroup" colSpan="3" class="numeric">Recorded eligible nominal amount<\/th>/i);
  assert.match(body, /<th scope="col">Subfund<\/th><th scope="col">Account<\/th><th scope="col" class="numeric">28 Mar 2023<\/th>/);
  // The backend's plain-text narrative is not repeated beside the table.
  assert.ok(!body.includes("Compared 01_Holdings"));
  assert.ok(technicalDetails(markup).includes("Compared 01_Holdings"));
  // Bond nominal quantities never get a currency symbol.
  assert.ok(!markup.includes("$"));
});

test("differences keep exact values with clear signs and right-aligned numeric cells", () => {
  const rows = buildAnswerView(holdingsInput()).holdings!.rows;
  assert.deepEqual(rows.map((row) => [row.subfund, row.account, row.earlier.display, row.later.display, row.difference.display, row.difference.direction, row.measure]), [
    ["Horizon Credit Fund A", "AC-1001", "1,000,000", "600,000", "−400,000", "decrease", "USD"],
    ["Horizon Credit Fund B", "AC-2001", "600,000", "1,000,000", "+400,000", "increase", "USD"],
    ["Meridian Income Fund C", "AC-3001", "400,000", "400,000", "0", "none", "USD"],
  ]);
  assert.deepEqual(rows.map((row) => row.earlier.exact), ["1000000", "600000", "400000"]);
  const markup = render(holdingsInput());
  assert.match(markup, /<td class="numeric difference decrease">−400,000<\/td>/);
  assert.match(markup, /<td class="numeric difference increase">\+400,000<\/td>/);
  assert.match(markup, /<td class="numeric difference none">0<\/td>/);
  assert.match(markup, /<tfoot><tr><th scope="row">Total<\/th><td><\/td><td class="numeric">2,000,000<\/td><td class="numeric">2,000,000<\/td><td class="numeric difference">0<\/td><td>USD<\/td>/);
});

test("quantity formatting pads to one precision and never rounds or reinterprets stored text", () => {
  assert.equal(quantityPlaces(["100.10", "100.35", "1000000.0", null]), 2);
  assert.equal(formatQuantity("100.1", 2), "100.10");
  assert.equal(formatQuantity("1000000.0", 2), "1,000,000.00");
  assert.equal(formatQuantity("+0.25", 2, true), "+0.25");
  assert.equal(formatQuantity("-0.00", 2, true), "0.00");
  assert.equal(formatQuantity("12345678901234567890.123456789", 9), "12,345,678,901,234,567,890.123456789");
  // More digits than the requested precision: shown exactly as stored, not rounded.
  assert.equal(formatQuantity("1.005", 2), "1.005");
  assert.equal(formatQuantity("1,000", 0), "1,000");
  assert.equal(formatQuantity(null), "—");
  // A quantity the backend could not use is shown verbatim, not reformatted.
  const unusable: MatterHoldingRow = { ...holdingRow("Sub S", "AC-4", 11, "1.000", "1000", "0"), status: "unusable_quantity", difference: null, note: "the number format of “1.000” is ambiguous" };
  const view = buildAnswerView(holdingsInput({ holdingsComparison: comparison({ rows: [unusable], totals: { earlier: null, later: null, difference: null, currency: null, note: "Totals are not shown because at least one position could not be compared." } }) }));
  const row = view.holdings!.rows[0];
  assert.deepEqual([row.earlier.display, row.difference.display, row.difference.direction, row.status], ["1.000", "—", "not_compared", "Quantity not usable"]);
  assert.equal(view.holdings!.totals, null);
  assert.ok(view.warnings.some((notice) => notice.tone === "warning" && /could not be compared/.test(notice.text)));
  assert.match(view.conclusion ?? "", /1 position could not be compared/);
});

test("dates are formatted without time zones", () => {
  assert.equal(formatIsoDate("2023-03-28"), "28 Mar 2023");
  assert.equal(formatIsoDate("2023-13-01"), "2023-13-01");
  assert.equal(formatIsoDate(null), "Undated");
});

test("source actions open the exact stored document and reference", () => {
  const opened: Array<[string, string]> = [];
  const element = MatterAnswer({ input: holdingsInput(), onOpenSource: (documentId, reference) => opened.push([documentId, reference]) });
  const sourceButtons = buttons(element).filter((button) => button.props["aria-label"]?.startsWith("Open source"));
  const fundA = sourceButtons.filter((button) => button.props["aria-label"] === `Open source Holdings!G8 in ${MARCH_FILE}`);
  assert.ok(fundA.length >= 1);
  fundA[0].props.onClick?.();
  assert.deepEqual(opened, [[march, "Holdings!G8"]]);
  // The caveat cell opens from the limitations section.
  sourceButtons.find((button) => button.props["aria-label"] === `Open source Holdings!A14 in ${APRIL_FILE}`)?.props.onClick?.();
  assert.deepEqual(opened[1], [april, "Holdings!A14"]);
  // Without navigation the references remain visible as text.
  assert.match(render(holdingsInput()), /<span class="source-chip static" title="01_Holdings_2023-03-28.xlsx · Holdings!G8">28 Mar 2023<\/span>/);
});

test("evidence is grouped by document and spreadsheet row or PDF page, without duplicates", () => {
  const groups = groupEvidence([
    citation(march, MARCH_FILE, "Holdings!G8"), citation(march, MARCH_FILE, "Holdings!B8"), citation(march, MARCH_FILE, "Holdings!G8"),
    citation(march, MARCH_FILE, "Holdings!A14"), citation(pdf, "Notice.pdf", "page 2 block 4", "withdraws"), citation(pdf, "Notice.pdf", "page 1 block 1"),
  ]);
  assert.deepEqual(groups.map((group) => [group.label, group.count, group.groups.map((item) => [item.label, item.items.map((entry) => entry.shortReference)])]), [
    ["Holdings 28 Mar 2023", 3, [["Holdings · row 8", ["B8", "G8"]], ["Holdings · row 14", ["A14"]]]],
    ["Notice", 2, [["Page 1", ["block 1"]], ["Page 2", ["block 4"]]]],
  ]);
  // Exact references are kept for navigation.
  assert.deepEqual(groups[0].groups[0].items.map((item) => item.reference), ["Holdings!B8", "Holdings!G8"]);
  const markup = render(holdingsInput());
  assert.match(markup, /<summary>Supporting evidence · 6 references in 2 documents<\/summary>/);
  assert.match(markup, /<strong>Holdings 28 Mar 2023<\/strong><small>01_Holdings_2023-03-28.xlsx<\/small>/);
});

test("long file names get short labels while the full name stays available", () => {
  const long = "2023-04-30_Northstar_Investors_LP_Custody_Statement_Final_Version_Signed_Copy_v7.pdf";
  const label = shortDocumentLabel(long);
  assert.ok(label.length <= 48 && label.endsWith("…"));
  assert.ok(label.startsWith("30 Apr 2023 Northstar Investors LP"));
  assert.equal(shortDocumentLabel("Statement.pdf"), "Statement");
  const markup = render({ ...holdingsInput(), holdingsComparison: null, intent: "general", answer: "Recorded.", citations: [citation(pdf, long, "page 1 block 1")] });
  assert.ok(markup.includes(`<small>${long}</small>`));
  // Two files with the same short label keep their full names to stay distinguishable.
  const same = groupEvidence([citation(march, "a/Notice.pdf", "page 1"), citation(april, "Notice.PDF", "page 1"), citation(pdf, "Notice.pdf", "page 1")]);
  assert.deepEqual(same.map((group) => group.label), ["a/Notice", "Notice.PDF", "Notice.pdf"]);
});

test("answers from before the current response shape still render", () => {
  const legacyScope = { matterId: WORKSPACE_MATTER_ID, mode: "selected", documentsInScope: 2, searchableDocuments: 2, documentsWithEvidence: 2,
    unavailableDocuments: [], incompleteDocuments: [], transmittedDocuments: [], searchTruncated: false } as unknown as MatterDocumentScope;
  const legacyComparison = {
    documents: [{ documentId: march, filename: MARCH_FILE, date: "2023-03-28", dateBasis: "file name" }, { documentId: april, filename: APRIL_FILE, date: null, dateBasis: null }],
    orderBasis: "file-name dates", issues: [],
    rows: [{ entities: [{ kind: "subfund", name: "Fund A" }], account: "AC-1", isin: "XS1111111111", earlier: null, later: null, difference: null, status: "only_earlier", note: "Recorded only in 01." }],
  } as unknown as MatterHoldingsComparison;
  const view = buildAnswerView({ answer: "Old answer.", unknown: false, citations: [], documentScope: legacyScope, intent: "holdings_comparison", inventory: null, holdingsComparison: legacyComparison });
  assert.equal(view.holdings?.laterDate, "Undated");
  assert.equal(view.holdings?.totals, null);
  assert.deepEqual(view.statedCaveats, []);
  assert.match(render({ answer: "Old answer.", unknown: false, citations: [], documentScope: legacyScope, intent: "general" }), /Old answer\./);
  assert.match(render({ answer: null, unknown: true, citations: [], documentScope: legacyScope, intent: "general" }), /does not establish an answer/);
});

test("material legal limits stay visible, are short, and never ask the user to ask again", () => {
  const view = buildAnswerView(holdingsInput({ holdingsComparison: comparison({ legalQuestionNotAnswered: true }) }));
  assert.deepEqual(view.limitations, [
    "Recorded holding changes do not by themselves establish a securities transfer or an assignment of legal claim rights.",
    "Legal claim status: the compared spreadsheets record holdings only. They do not establish whether legal claim rights were assigned, transferred or withdrawn, and no other document was used for this part of the question.",
  ]);
  const markup = render(holdingsInput({ holdingsComparison: comparison({ legalQuestionNotAnswered: true }) }));
  const body = visible(markup);
  assert.ok(body.includes("Legal claim status: the compared spreadsheets record holdings only."));
  assert.ok(body.includes(`Stated in the source: “${CAVEAT}”`));
  assert.doesNotMatch(markup, /ask (?:it|this|again|separately)/i);
  // The caveat text appears once, not once per source file.
  assert.equal(body.split(CAVEAT).length - 1, 1);
});

test("unresolved issues and failed control totals are shown, not hidden in technical details", () => {
  const unverified = { ...table(), controlTotal: { ...table().controlTotal!, status: "differs" as const, note: "Holdings!G12 states 1,900,000, but the record rows total 2,000,000." } };
  const input = holdingsInput({ holdingsComparison: comparison({
    issues: ["“Zeta Growth Fund Z” does not appear under an entity heading in either spreadsheet."],
    documents: [{ documentId: march, filename: MARCH_FILE, date: "2023-03-28", dateBasis: "file name", tables: [unverified] }, comparison().documents[1]],
  }) });
  const body = visible(render(input));
  assert.ok(body.includes("Zeta Growth Fund Z"));
  assert.ok(body.includes("states 1,900,000, but the record rows total 2,000,000"));
  assert.match(body, /<li class="attention">Holdings 28 Mar 2023: control total does not match the record rows/);
  // Raw coordinates live in the technical details only.
  assert.ok(!body.includes("Holdings!A7:H7"));
  assert.ok(technicalDetails(render(input)).includes("headers Holdings!A7:H7; records Holdings!A8:H10"));
});

test("partial coverage of a prose answer is a visible warning", () => {
  const input: AnswerInput = { answer: "Fund A appears in the notice.", unknown: false, intent: "general", documentScope: scope({ searchTruncated: true, incompleteDocuments: [{ documentId: pdf, filename: "Scan.pdf" }] }), citations: [citation(pdf, "Scan.pdf", "page 1 block 1")] };
  const body = visible(render(input));
  assert.match(body, /Only part of the matching evidence fitted in this answer/);
  assert.match(body, /Some pages could not be read in 1 document/);
});

test("general answers render as concise paragraphs and lists, and the scope stays visible", () => {
  assert.deepEqual(proseBlocks("Two records match.\n\n- Fund A: AC-1001\n- Fund B: AC-2001\nBoth are dated 30 April."), [
    { kind: "paragraph", text: "Two records match." },
    { kind: "list", items: ["Fund A: AC-1001", "Fund B: AC-2001"] },
    { kind: "paragraph", text: "Both are dated 30 April." },
  ]);
  const long = Array.from({ length: 12 }, (_, index) => `Sentence ${index + 1} cites Example S.A. as the holder.`).join(" ");
  const blocks = proseBlocks(long);
  assert.ok(blocks.length > 1);
  assert.equal(blocks.map((block) => block.kind === "paragraph" ? block.text : "").join(" "), long);
  const markup = render({ answer: "Fund C withdrew from the litigation [page 1 block 2].", unknown: false, intent: "legal_status", model: "gpt-test",
    documentScope: scope({ resolvedDocuments: [], mode: "selected", documentsInScope: 1, searchableDocuments: 1, documentsWithEvidence: 1 }), citations: [citation(pdf, "04_Litigation_Status_Notice.pdf", "page 1 block 2", "withdraws")] });
  assert.match(markup, /<h4>Litigation and claim-right status<\/h4>/);
  assert.match(markup, /Selected documents only · 1 document · evidence from 1/);
  assert.match(markup, /Provisional · AI answer from cited excerpts/);
  assert.match(visible(markup), /It is not a legal conclusion on entitlement or claim status\./);
});

test("model and document text is rendered as text, never as HTML", () => {
  const hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>";
  const markup = render({ answer: hostile, unknown: false, intent: "general", documentScope: scope({ resolvedDocuments: [] }),
    citations: [citation(pdf, "<b>x</b>.pdf", "page 1 block 1", hostile)] });
  assert.ok(!markup.includes("<script>"));
  assert.ok(!markup.includes("<img"));
  assert.ok(markup.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(!markup.includes("<b>x</b>"));
});

test("entity listings show roles, associations, and source actions", () => {
  const inventory: MatterEntityInventory = {
    requestedRoles: ["investor"],
    entities: [{
      name: "Northstar Investors LP", roles: ["investor_or_legal_entity"], related: [{ name: "Horizon Credit Fund A", role: "subfund" }], documentCount: 2, mentionCount: 2, uncertainty: null,
      evidence: [{ documentId: march, filename: MARCH_FILE, sourceReference: "Holdings!B4", evidenceSnippet: "Northstar Investors LP", role: "investor_or_legal_entity", basis: "labelled “Investor / legal entity”", status: "document_label" }],
    }],
    otherRoleCounts: { subfund: 3 },
    coverage: { eligibleDocuments: 5, searchedDocuments: 5, documentsWithRoleLabels: 2, documentsWithListedEntities: 2, partial: true, partialDocuments: [{ documentId: pdf, filename: "Big.xlsx" }], entitiesOmitted: 0 },
    relationships: [],
  };
  const input: AnswerInput = { answer: "Searched all 5 searchable documents…", unknown: false, intent: "inventory", inventory, documentScope: scope({ resolvedDocuments: [] }), citations: [], model: "local-entity-inventory" };
  const view = buildAnswerView(input);
  assert.equal(view.conclusion, "1 entity found in 2 of 5 searchable documents.");
  assert.deepEqual(view.entities.map((entity) => [entity.name, entity.role, entity.related, entity.detail]), [["Northstar Investors LP", "Investor or legal entity", "Horizon Credit Fund A (Subfund)", "2 documents"]]);
  const body = visible(render(input));
  assert.match(body, /Partial coverage: 1 document exceeded the local scanning limit \(Big\.xlsx\)/);
  assert.match(body, /Also labelled but not requested: 3 × subfund\./);
  assert.match(body, /aria-label="Open source Holdings!B4 in 01_Holdings_2023-03-28.xlsx"|title="01_Holdings_2023-03-28.xlsx · Holdings!B4"/);
});

test("relationship answers distinguish documented links from same-row associations", () => {
  const inventory = {
    requestedRoles: [], entities: [], otherRoleCounts: {},
    coverage: { eligibleDocuments: 2, searchedDocuments: 2, documentsWithRoleLabels: 2, documentsWithListedEntities: 2, partial: false, partialDocuments: [], entitiesOmitted: 0 },
    relationships: [{ documentId: march, filename: MARCH_FILE, subject: "Horizon Credit Fund A", subjectRole: "subfund", subjectReference: "Holdings!B8", related: "Northstar Investors LP", relatedRole: "investor", relatedReference: "Holdings!B4", kind: "same_row" as const, basis: "row 8" }],
  };
  const view = buildAnswerView({ answer: "Found 1 pairing…", unknown: false, intent: "relationship", inventory, documentScope: scope(), citations: [] });
  assert.equal(view.layout, "relationships");
  assert.equal(view.conclusion, "1 pairing found: 0 documented associations, 1 same-row association only.");
  assert.deepEqual(view.relationships[0].sources.map((source) => source.reference), ["Holdings!B8", "Holdings!B4"]);
  assert.match(view.limitations[0], /do not prove legal ownership/);
  assert.match(view.limitations[1], /same-row association only means both names appear in one row/);
});

test("pending and failed questions keep the question visible with a status", () => {
  const pending = renderToStaticMarkup(createElement(PendingAnswer, { question: "Compare the <holdings>?" }));
  assert.match(pending, /role="status"/);
  assert.match(pending, /Compare the &lt;holdings&gt;\?/);
  const failed = renderToStaticMarkup(createElement(FailedAnswer, { question: "Which fund?", message: "The AI could not answer this question. No records were changed.", followUp: true }));
  assert.match(failed, /Follow-up question/);
  assert.match(failed, /role="alert"/);
});

const sectionRelationships = (): MatterEntityInventory => ({
  requestedRoles: [], entities: [], otherRoleCounts: {},
  coverage: { eligibleDocuments: 2, searchedDocuments: 2, documentsWithRoleLabels: 2, documentsWithListedEntities: 2, partial: false, partialDocuments: [], entitiesOmitted: 0 },
  relationships: ["Horizon Credit Fund A", "Horizon Credit Fund B", "Meridian Income Fund C"].map((subject, index) => ({
    documentId: march, filename: MARCH_FILE, subject, subjectRole: "subfund", subjectReference: `Holdings!B${8 + index}`,
    related: "Northstar Investors LP", relatedRole: "investor", relatedReference: "Holdings!B4", kind: "document_section" as const,
    basis: "the sheet labels “Investor / legal entity” (Holdings!A4) above the table headed in row 7, which lists this subfund",
  })),
});

test("a sheet-level investor label is shown as a documented association, never as ownership", () => {
  const input: AnswerInput = { answer: "Found 3 pairing(s)…", unknown: false, intent: "relationship", inventory: sectionRelationships(),
    documentScope: scope({ mode: "selected", documentsInScope: 2, searchableDocuments: 2, resolvedDocuments: [] }), citations: [], model: "local-relationship-mapping" };
  const view = buildAnswerView(input);
  assert.equal(view.conclusion, "3 pairings found: 3 documented associations.");
  assert.deepEqual(view.relationships.map((relation) => [relation.subject, relation.related, relation.documented, relation.sources.map((source) => source.reference)]), [
    ["Horizon Credit Fund A", "Northstar Investors LP", true, ["Holdings!B8", "Holdings!B4"]],
    ["Horizon Credit Fund B", "Northstar Investors LP", true, ["Holdings!B9", "Holdings!B4"]],
    ["Meridian Income Fund C", "Northstar Investors LP", true, ["Holdings!B10", "Holdings!B4"]],
  ]);
  assert.match(view.relationships[0].basis, /^Documented association: the sheet’s investor label covers the table/);
  assert.deepEqual(view.limitations, ["These associations show how the documents list the names. They do not prove legal ownership, separate legal personality, claimant status, or invoicing responsibility."]);
  const body = visible(render(input));
  assert.match(body, /Selected documents only · 2 documents/);
  assert.doesNotMatch(body, /\bowns\b|\bowned by\b|legal owner of/i);
});

test("a local legal-rights answer leads with its conclusion and shows the dated records as factual evidence", () => {
  const legalText = "The holdings spreadsheets dated 2023-03-28 and 2023-04-30 record: Horizon Credit Fund A / AC-1001: 1,000,000 → 600,000 (change -400,000).";
  const input = holdingsInput({ intent: "legal_status", model: "local-legal-status-holdings", answer: legalText,
    holdingsComparison: comparison({ legalQuestionNotAnswered: true }),
    citations: [citation(march, MARCH_FILE, "Holdings!G8", "1000000"), citation(april, APRIL_FILE, "Holdings!G8", "600000"), citation(march, MARCH_FILE, "Holdings!A14", CAVEAT)] });
  const view = buildAnswerView(input);
  assert.equal(view.title, "Litigation and claim-right status");
  assert.equal(view.conclusion, "The holdings spreadsheets record 2 changed positions between 28 Mar 2023 and 30 Apr 2023 (below). Neither spreadsheet records an assignment or transfer of legal claim rights, and no other document was used.");
  assert.deepEqual(view.limitations, [
    "Recorded holding changes do not by themselves establish a securities transfer or an assignment of legal claim rights.",
    "Establishing an assignment of legal claim rights needs explicit evidence, such as an assignment agreement or notice.",
  ]);
  const markup = render(input);
  const body = visible(markup);
  assert.match(body, /<caption>Factual evidence: dated holding records\./);
  assert.match(body, /Recorded eligible nominal amount/i);
  // The per-row figures appear once, in the table, not again as narrative.
  assert.ok(!body.includes(legalText));
  assert.ok(technicalDetails(markup).includes(legalText));
  assert.ok(!markup.includes("Holdings!A1<") && !markup.includes("Holdings!A1\""));
});

test("an AI legal-rights answer keeps its prose and adds the dated records without overclaiming scope", () => {
  const input = holdingsInput({ intent: "legal_status", model: "gpt-test", answer: "The assignment notice records 400,000 claim rights assigned from Fund A to Fund B [page 1 block 0].",
    holdingsComparison: comparison({ legalQuestionNotAnswered: true }) });
  const view = buildAnswerView(input);
  assert.equal(view.conclusion, null);
  assert.deepEqual(view.prose, [{ kind: "paragraph", text: "The assignment notice records 400,000 claim rights assigned from Fund A to Fund B [page 1 block 0]." }]);
  assert.equal(view.holdings?.evidenceRole, "Factual evidence: dated holding records");
  assert.ok(!view.limitations.some((text) => /no other document was used/.test(text)));
  assert.equal(view.method, "Provisional · AI answer; holdings calculated locally");
});

test("long evidence lists stay collapsed per document when several documents are cited", () => {
  const many = [8, 9, 10].flatMap((row) => ["B", "C", "G"].map((column) => citation(march, MARCH_FILE, `Holdings!${column}${row}`)));
  const markup = render(holdingsInput({ citations: [...many, citation(april, APRIL_FILE, "Holdings!G8")] }));
  assert.match(markup, /<details class="evidence-document"><summary class="evidence-document-name"><strong>Holdings 28 Mar 2023<\/strong>.*?9 references/);
  assert.match(markup, /<div class="evidence-document"><p class="evidence-document-name"><strong>Holdings 30 Apr 2023<\/strong>/);
});

function reconciledTable(comparisonRows: number, comparisonTotal: string, reconciled: boolean): MatterHoldingsTable {
  return { ...table(), controlTotal: { ...table().controlTotal!, recordRows: 3, comparisonRows, comparisonTotal, reconciled } };
}

test("complete comparisons show full totals and a reconciled control total", () => {
  const input = holdingsInput({ holdingsComparison: comparison({
    totals: { earlier: "2000000", later: "2000000", difference: "0", currency: "USD", coverage: "all_records", note: null },
    documents: comparison().documents.map((document) => ({ ...document, tables: [reconciledTable(3, "2000000", true)] })),
    requestedEntities: [
      { text: "Horizon Credit Fund A", name: "Horizon Credit Fund A", match: "exact", candidates: [], positions: 1 },
      { text: "Fund A", name: "Horizon Credit Fund A", match: "alias", candidates: [], positions: 1 },
    ],
  }) });
  const view = buildAnswerView(input);
  assert.deepEqual(view.holdings!.metrics.slice(0, 3).map((metric) => metric.label), ["Recorded total · 28 Mar 2023", "Recorded total · 30 Apr 2023", "Net difference"]);
  assert.deepEqual(view.holdings!.reconciliation.map((item) => item.state), ["reconciled", "reconciled"]);
  assert.equal(view.holdings!.totals?.label, "Total");
  const body = visible(render(input));
  assert.match(body, /<li class="reconciled">Holdings 28 Mar 2023: control total reconciles with the compared rows · records total 2,000,000/);
  assert.match(body, /“Fund A” was read as Horizon Credit Fund A, the only matching name in the spreadsheets\./);
});

test("a partial comparison never presents its totals as full holdings or as reconciled", () => {
  const rows = comparison().rows.slice(0, 2);
  const input = holdingsInput({ holdingsComparison: comparison({
    rows,
    totals: { earlier: "1600000", later: "1600000", difference: "0", currency: "USD", coverage: "selected_rows", note: "Totals of the 2 compared position(s) only, not the spreadsheets' full holdings totals." },
    documents: comparison().documents.map((document) => ({ ...document, tables: [reconciledTable(2, "1600000", false)] })),
    issues: ["“Zeta Growth Fund Z” does not appear under an entity heading in either spreadsheet."],
    requestedEntities: [{ text: "Zeta Growth Fund Z", name: null, match: "not_found", candidates: [], positions: 0 }],
  }) });
  const view = buildAnswerView(input);
  assert.deepEqual(view.holdings!.metrics.slice(0, 3).map((metric) => [metric.label, metric.value]), [
    ["Compared rows total · 28 Mar 2023", "1,600,000"], ["Compared rows total · 30 Apr 2023", "1,600,000"], ["Net difference (compared rows)", "0"],
  ]);
  assert.deepEqual(view.holdings!.reconciliation.map((item) => item.state), ["source_only", "source_only"]);
  const markup = render(input);
  const body = visible(markup);
  assert.doesNotMatch(body, /reconciles with the compared rows/);
  assert.match(body, /<li class="source_only">Holdings 28 Mar 2023: source control total formula covers exactly the record rows · records total 2,000,000; the comparison includes 2 of its 3 rows, so it does not reconcile the totals shown/);
  assert.match(body, /<th scope="row">Total of compared rows<\/th>/);
  assert.match(body, /<li class="warning">Totals of the 2 compared position\(s\) only, not the spreadsheets&#x27; full holdings totals\.<\/li>/);
  assert.match(body, /<li class="warning">“Zeta Growth Fund Z” does not appear under an entity heading in either spreadsheet\.<\/li>/);
  assert.doesNotMatch(body, /Recorded total/);
});
