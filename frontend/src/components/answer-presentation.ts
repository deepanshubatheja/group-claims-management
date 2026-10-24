import { answerScopeLabel, controlTotalLabel, entityRoleLabel, holdingStatusLabel, questionIntentLabel, relationshipKindLabel } from "./matter-scope.js";

/*
 * Presentation model for one Matter Review answer.
 *
 * Every figure, table row and source reference here comes from the typed
 * payload Python returned; nothing is parsed out of answer prose.  Model or
 * document text is only ever returned as plain strings for React to render
 * as text.  Optional fields may be absent on answers produced before the
 * current response shape, so every optional read has a fallback.
 */

/** The parts of a completed question that the answer card renders. */
export type AnswerInput = {
  answer: string | null;
  unknown: boolean;
  citations: MatterQuestionCitation[];
  documentScope: MatterDocumentScope;
  intent: MatterQuestionIntent;
  inventory?: MatterEntityInventory | null;
  holdingsComparison?: MatterHoldingsComparison | null;
  model?: string | null;
  execution?: MatterQuestionExecution | null;
};

export type AnswerLayout = "holdings" | "entities" | "relationships" | "prose";
export type Notice = { tone: "warning" | "info"; text: string };
export type SourceLink = { documentId: string; filename: string; reference: string; label: string };
export type ProseBlock = { kind: "paragraph"; text: string } | { kind: "list"; items: string[] };
export type TechnicalDetail = { label: string; value: string };
export type EvidenceItem = { reference: string; shortReference: string; snippet: string };
export type EvidenceGroup = { label: string; items: EvidenceItem[] };
export type EvidenceDocument = { documentId: string; filename: string; label: string; groups: EvidenceGroup[]; count: number };
export type StatedCaveat = { text: string; sources: SourceLink[] };

export type HoldingsCell = { display: string; exact: string | null; source: SourceLink | null };
export type HoldingsRowView = {
  key: string;
  subfund: string;
  investor: string;
  account: string;
  isin: string;
  measure: string;
  earlier: HoldingsCell;
  later: HoldingsCell;
  difference: { display: string; direction: "increase" | "decrease" | "none" | "not_compared" };
  status: string | null;
  note: string | null;
};
export type HoldingsView = {
  earlierDate: string;
  laterDate: string;
  investor: string | null;
  isin: string | null;
  quantityHeading: string | null;
  /** e.g. "Recorded eligible nominal amount", from the source's own column heading. */
  quantityLabel: string;
  /** Set when the table is factual evidence for another kind of answer. */
  evidenceRole: string | null;
  metrics: Array<{ label: string; value: string; detail?: string }>;
  rows: HoldingsRowView[];
  showIsinColumn: boolean;
  showInvestorColumn: boolean;
  totals: { earlier: string; later: string; difference: string; measure: string; label: string } | null;
  totalsNote: string | null;
  /** "reconciled": the control total covers exactly the compared rows and agrees; "source_only": it checks
   *  its own record rows but the comparison covers fewer; "attention": it does not agree or was not verified. */
  reconciliation: Array<{ text: string; state: "reconciled" | "source_only" | "attention"; source: SourceLink }>;
};
export type EntityRowView = { key: string; name: string; detail: string | null; role: string; uncertainty: string | null; related: string; sources: SourceLink[] };
export type RelationshipRowView = { key: string; subject: string; subjectRole: string; related: string; relatedRole: string; basis: string; documented: boolean; sources: SourceLink[] };

export type AnswerView = {
  layout: AnswerLayout;
  title: string;
  method: string;
  scopeLabel: string;
  conclusion: string | null;
  prose: ProseBlock[];
  holdings: HoldingsView | null;
  entities: EntityRowView[];
  relationships: RelationshipRowView[];
  warnings: Notice[];
  limitations: string[];
  statedCaveats: StatedCaveat[];
  evidence: EvidenceDocument[];
  technical: TechnicalDetail[];
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const TITLES: Record<MatterQuestionIntent, string> = {
  holdings_comparison: "Holdings comparison",
  historical_reconstruction: "Historical holdings reconstruction",
  inventory: "Entity listing",
  relationship: "Entity relationships",
  specific_lookup: "Account / ISIN lookup",
  legal_status: "Litigation and claim-right status",
  calculation: "Billing calculation",
  change: "Changes and transfers",
  comparison: "Cross-document comparison",
  general: "Answer",
};
const plural = (count: number, singular: string, many = `${singular}s`) => `${count} ${count === 1 ? singular : many}`;

/** "2023-03-28" → "28 Mar 2023"; anything else is returned unchanged. No time zone is involved. */
export function formatIsoDate(value: string | null | undefined): string {
  if (!value) return "Undated";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const month = match ? Number(match[2]) : 0;
  return match && month >= 1 && month <= 12 ? `${Number(match[3])} ${MONTHS[month - 1]} ${match[1]}` : value;
}

/** A short, readable label for a file name; the full name stays available alongside it. */
export function shortDocumentLabel(filename: string): string {
  const stem = filename.replace(/\.[A-Za-z0-9]{2,5}$/, "");
  const readable = stem
    .replace(/^\d{1,3}[_\-\s]+(?=\D)/, "")
    .replace(/(\d{4})[-_.](\d{2})[-_.](\d{2})/g, (date, year: string, month: string, day: string) => {
      const formatted = formatIsoDate(`${year}-${month}-${day}`);
      return formatted === `${year}-${month}-${day}` ? date : formatted;
    })
    .replace(/[_]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  const label = readable || filename;
  return label.length > 48 ? `${label.slice(0, 47).trimEnd()}…` : label;
}

/** Short labels for the documents of one answer; colliding labels fall back to the file name. */
function documentLabels(filenames: Iterable<string>): Map<string, string> {
  const names = [...new Set(filenames)];
  const counts = new Map<string, number>();
  for (const name of names) counts.set(shortDocumentLabel(name), (counts.get(shortDocumentLabel(name)) ?? 0) + 1);
  return new Map(names.map((name) => {
    const label = shortDocumentLabel(name);
    return [name, (counts.get(label) ?? 0) > 1 ? name : label];
  }));
}

const EXACT = /^([+-]?)(\d+)(?:\.(\d+))?$/;

/** Fraction digits needed to show every exact value in a set without loss. */
export function quantityPlaces(values: Iterable<string | null | undefined>): number {
  let places = 0;
  for (const value of values) {
    const match = value ? EXACT.exec(value.trim()) : null;
    if (match?.[3]) places = Math.max(places, match[3].replace(/0+$/, "").length);
  }
  return places;
}

/**
 * Thousands-grouped display of an exact decimal string with a fixed number of
 * fraction digits (padding only, never rounding: ``places`` comes from
 * ``quantityPlaces``).  A value that is not a plain decimal is shown exactly
 * as stored.  Negative values use a true minus sign; ``signed`` adds "+".
 */
export function formatQuantity(value: string | null | undefined, places = 0, signed = false): string {
  if (value === null || value === undefined || value.trim() === "") return "—";
  const match = EXACT.exec(value.trim());
  if (!match) return value;
  const [, sign, whole, fraction = ""] = match;
  const significant = fraction.replace(/0+$/, "");
  if (significant.length > places) return value;
  const digits = whole.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const padded = places > 0 ? `.${significant.padEnd(places, "0")}` : "";
  const zero = /^0+$/.test(whole) && significant === "";
  const prefix = zero ? "" : sign === "-" ? "−" : signed ? "+" : "";
  return `${prefix}${digits}${padded}`;
}

/** Split answer text into paragraphs and simple bullet lists, as plain strings. */
export function proseBlocks(text: string | null | undefined): ProseBlock[] {
  if (!text?.trim()) return [];
  const blocks: ProseBlock[] = [];
  for (const chunk of text.replace(/\r\n?/g, "\n").split(/\n\s*\n/)) {
    const lines = chunk.split("\n").map((line) => line.trim()).filter(Boolean);
    let paragraph: string[] = [];
    let list: string[] = [];
    const flushParagraph = () => {
      if (paragraph.length) blocks.push(...longParagraph(paragraph.join(" ")));
      paragraph = [];
    };
    const flushList = () => {
      if (list.length) blocks.push({ kind: "list", items: list });
      list = [];
    };
    for (const line of lines) {
      const bullet = /^(?:[-*•]|\d{1,2}[.)])\s+(.*)$/.exec(line);
      if (bullet) { flushParagraph(); list.push(bullet[1]); }
      else { flushList(); paragraph.push(line); }
    }
    flushParagraph(); flushList();
  }
  return blocks;
}

/** Break a very long single paragraph at sentence ends so it can be scanned. */
function longParagraph(text: string): ProseBlock[] {
  if (text.length <= 480) return [{ kind: "paragraph", text }];
  // Split only where a sentence ends and a new one clearly starts, so
  // abbreviations such as "S.A." or "No. 5" are not broken.
  const sentences = text.split(/(?<=[a-z0-9)”"’][.!?])\s+(?=[A-Z“"(])/);
  const blocks: ProseBlock[] = [];
  let current = "";
  for (const sentence of sentences) {
    if (current && current.length + sentence.length > 360) { blocks.push({ kind: "paragraph", text: current }); current = sentence; }
    else current = current ? `${current} ${sentence}` : sentence;
  }
  if (current) blocks.push({ kind: "paragraph", text: current });
  return blocks;
}

/** "Holdings!G8" → group "Holdings · row 8", item "G8"; "page 2 block 4" → "Page 2", "block 4". */
function referenceParts(reference: string): { group: string; order: number; short: string; position: number } {
  const cell = /^(.*)!([A-Z]{1,3})(\d+)$/.exec(reference);
  if (cell) {
    const column = [...cell[2]].reduce((total, letter) => total * 26 + letter.charCodeAt(0) - 64, 0);
    return { group: `${cell[1]} · row ${cell[3]}`, order: Number(cell[3]), short: `${cell[2]}${cell[3]}`, position: column };
  }
  const page = /^page (\d+)(?: (.*))?$/i.exec(reference);
  if (page) return { group: `Page ${page[1]}`, order: Number(page[1]), short: page[2] ?? `page ${page[1]}`, position: 0 };
  return { group: "Other references", order: Number.MAX_SAFE_INTEGER, short: reference, position: 0 };
}

/** Citations grouped by document, then by spreadsheet row or PDF page, in source order. */
export function groupEvidence(citations: readonly MatterQuestionCitation[]): EvidenceDocument[] {
  const labels = documentLabels(citations.map((citation) => citation.filename));
  const documents = new Map<string, EvidenceDocument & { orders: Map<string, number> }>();
  const seen = new Set<string>();
  for (const citation of citations) {
    const key = `${citation.documentId}\u0000${citation.sourceReference}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const document = documents.get(citation.documentId) ?? {
      documentId: citation.documentId, filename: citation.filename, label: labels.get(citation.filename) ?? citation.filename,
      groups: [], count: 0, orders: new Map<string, number>(),
    };
    documents.set(citation.documentId, document);
    const parts = referenceParts(citation.sourceReference);
    let group = document.groups.find((item) => item.label === parts.group);
    if (!group) { group = { label: parts.group, items: [] }; document.groups.push(group); document.orders.set(parts.group, parts.order); }
    group.items.push({ reference: citation.sourceReference, shortReference: parts.short, snippet: citation.evidenceSnippet });
    document.count += 1;
  }
  return [...documents.values()].map(({ orders, ...document }) => ({
    ...document,
    groups: [...document.groups]
      .sort((a, b) => (orders.get(a.label) ?? 0) - (orders.get(b.label) ?? 0))
      .map((group) => ({ ...group, items: [...group.items].sort((a, b) => referenceParts(a.reference).position - referenceParts(b.reference).position) })),
  }));
}

function sourceLink(documentId: string, filename: string, reference: string | null | undefined, labels: Map<string, string>): SourceLink | null {
  return reference ? { documentId, filename, reference, label: labels.get(filename) ?? filename } : null;
}

function methodLabel(input: AnswerInput): string {
  const model = input.model ?? "";
  // An AI answer stays labelled as one even when locally calculated records are attached.
  if (model && !model.startsWith("local-")) {
    return input.holdingsComparison ? "Provisional · AI answer; holdings calculated locally" : "Provisional · AI answer from cited excerpts";
  }
  if (input.holdingsComparison) return "Provisional · calculated locally from the spreadsheets";
  if (input.inventory) return "Provisional · built locally from the documents";
  if (model === "local-document-calculation") return "Provisional · calculated locally from the cited instruction";
  if (model.startsWith("local-")) return "Provisional · answered locally";
  return "Provisional · AI answer from cited excerpts";
}

function scopeNotices(scope: MatterDocumentScope, view: { layout: AnswerLayout }): Notice[] {
  const notices: Notice[] = [];
  for (const note of scope.notes ?? []) notices.push({ tone: "info", text: note });
  if (scope.searchTruncated && view.layout === "prose") {
    notices.push({ tone: "warning", text: "Only part of the matching evidence fitted in this answer, so it is not a review of every matching passage." });
  }
  const unavailable = scope.unavailableDocuments ?? [];
  if (unavailable.length > 0) {
    notices.push({ tone: "info", text: `${plural(unavailable.length, "document")} in scope could not be searched (not parsed, unsupported, or failed extraction).` });
  }
  const incomplete = scope.incompleteDocuments ?? [];
  if (incomplete.length > 0) {
    notices.push({ tone: "warning", text: `Some pages could not be read in ${plural(incomplete.length, "document")}; evidence from those pages is missing.` });
  }
  return notices;
}

function baseTechnical(input: AnswerInput): TechnicalDetail[] {
  const scope = input.documentScope;
  const details: TechnicalDetail[] = [
    { label: "Response type", value: questionIntentLabel(input.intent) },
    { label: "Scope", value: answerScopeLabel(scope) },
  ];
  if (input.model) details.push({
    label: "Answered by",
    value: input.model === "codex-default" ? "Codex (underlying model not reported)" : input.model,
  });
  const execution = input.execution;
  if (execution) {
    details.push(
      { label: "Provider", value: execution.provider ?? "Local processing" },
      { label: "Provider call", value: execution.providerCallAttempted ? execution.providerOutcome.replace(/_/g, " ") : "not attempted" },
      { label: "Evidence coverage", value: `${execution.documentsIncluded.length} included · ${execution.documentsExcluded.length} excluded · ${execution.documentsSelected.length} selected${execution.evidenceTruncated ? " · bounded evidence truncated" : ""}` },
    );
    if (execution.responseStatus) details.push({ label: "Response coverage", value: execution.responseStatus.replace(/_/g, " ") });
    if (execution.modelUnknown !== null) details.push({ label: "Model reported no supported findings", value: execution.modelUnknown ? "yes" : "no" });
    if (execution.validatedSectionKinds.length) {
      const sections = execution.validatedSectionKinds.map((kind) => `${kind.replace(/_/g, " ")} ${execution.validatedSectionCounts[kind] ?? 0}`);
      details.push({ label: "Validated findings", value: sections.join(" · ") });
      details.push({ label: "Validated citations", value: String(execution.validatedCitationCount) });
    }
    const evidenceItems = Object.entries(execution.evidenceItemCounts);
    if (evidenceItems.length) details.push({
      label: "Evidence items",
      value: evidenceItems.map(([kind, counts]) => `${kind.replace(/([A-Z])/g, " $1").toLowerCase()}: ${counts.included}/${counts.available}`).join(" · "),
    });
    if (execution.fallbackUsed) details.push({ label: "Fallback", value: execution.fallbackReason?.replace(/_/g, " ") ?? "used" });
    const fallbackEvidence = input.citations.filter((citation) => citation.origin === "local_fallback");
    if (fallbackEvidence.length) details.push({ label: "Fallback evidence", value: `${plural(fallbackEvidence.length, "locally selected citation")} — not provider-selected` });
  }
  for (const document of scope.resolvedDocuments ?? []) {
    details.push({ label: "Named in the question", value: `${document.filename} (matched by ${document.matchedBy})` });
  }
  const transmitted = scope.transmittedDocuments ?? [];
  details.push({
    label: "Sent to cloud AI",
    value: transmitted.length ? `Bounded excerpts from ${plural(transmitted.length, "document")}` : "Nothing (answered from local data)",
  });
  for (const document of scope.unavailableDocuments ?? []) details.push({ label: "Not searchable", value: `${document.filename} (${document.reason.replace(/_/g, " ")})` });
  for (const document of scope.incompleteDocuments ?? []) details.push({ label: "Pages unreadable", value: document.filename });
  return details;
}

function holdingsView(comparison: MatterHoldingsComparison, labels: Map<string, string>, warnings: Notice[], technical: TechnicalDetail[]): HoldingsView {
  const documents = comparison.documents ?? [];
  const rows = comparison.rows ?? [];
  const totals = comparison.totals ?? null;
  const tables = documents.flatMap((document) => (document.tables ?? []).map((table) => ({ document, table })));
  const compared = rows.filter((row) => row.status === "compared");
  // One precision for every quantity in the comparison, taken from the exact values.
  const places = quantityPlaces([
    ...compared.flatMap((row) => [row.earlier?.quantity, row.later?.quantity, row.difference]),
    totals?.earlier, totals?.later, totals?.difference,
  ]);
  const investors = [...new Set(rows.flatMap((row) => row.entities.filter((entity) => entity.kind === "investor").map((entity) => entity.name)))];
  const isins = [...new Set(rows.map((row) => row.isin).filter((isin): isin is string => Boolean(isin)))];
  const measureOf = (row: MatterHoldingRow) => row.currency ?? row.earlier?.currency ?? row.earlier?.unit ?? row.later?.currency ?? row.later?.unit ?? "—";
  const cell = (row: MatterHoldingRow, side: MatterHoldingSide | null): HoldingsCell => {
    if (!side) return { display: "—", exact: null, source: null };
    // Quantities the backend could not use are shown exactly as stored.
    const display = row.status === "compared" ? formatQuantity(side.quantity, places) : side.quantity ?? "—";
    return { display, exact: side.quantity, source: sourceLink(side.documentId, side.filename, side.quantityReference, labels) };
  };
  const earlierDate = formatIsoDate(documents[0]?.date);
  const laterDate = formatIsoDate(documents[1]?.date);
  const changed = compared.filter((row) => row.difference !== null && !/^[+-]?0+(?:\.0+)?$/.test(row.difference)).length;
  const metrics: HoldingsView["metrics"] = [];
  // Older answers have no coverage field; they were only ever totalled over every compared row.
  const fullTotals = (totals?.coverage ?? "all_records") === "all_records";
  const totalLabel = fullTotals ? "Recorded total" : "Compared rows total";
  if (totals?.earlier != null && totals.later != null && totals.difference != null) {
    const measure = totals.currency ?? "";
    metrics.push(
      { label: `${totalLabel} · ${earlierDate}`, value: formatQuantity(totals.earlier, places), detail: measure },
      { label: `${totalLabel} · ${laterDate}`, value: formatQuantity(totals.later, places), detail: measure },
      { label: fullTotals ? "Net difference" : "Net difference (compared rows)", value: formatQuantity(totals.difference, places, true), detail: measure },
    );
    if (!fullTotals) warnings.push({ tone: "warning", text: totals.note ?? "These totals cover only the compared positions, not the spreadsheets' full holdings." });
  }
  metrics.push({ label: "Matched positions", value: String(compared.length), detail: `${changed} changed${rows.length > compared.length ? ` · ${rows.length - compared.length} not compared` : ""}` });

  const reconciliation: HoldingsView["reconciliation"] = [];
  for (const { document, table } of tables) {
    const control = table.controlTotal;
    if (!control) continue;
    const source = sourceLink(document.documentId, document.filename, control.reference, labels);
    if (!source) continue;
    const total = control.recordsTotal !== null ? ` · records total ${formatQuantity(control.recordsTotal, places)}` : "";
    const sourceChecks = ["formula_covers_records", "matches"].includes(control.status);
    // Older answers lack coverage fields; their comparisons always used every record row.
    const coversComparison = control.reconciled ?? sourceChecks;
    if (sourceChecks && coversComparison) {
      reconciliation.push({ text: `${source.label}: control total reconciles with the compared rows${total}`, state: "reconciled", source });
    } else if (sourceChecks) {
      reconciliation.push({
        text: `${source.label}: source control total ${controlTotalLabel(control.status).toLowerCase()}${total}; the comparison includes ${control.comparisonRows ?? 0} of its ${control.recordRows ?? "?"} rows, so it does not reconcile the totals shown`,
        state: "source_only", source,
      });
    } else {
      reconciliation.push({ text: `${source.label}: control total ${controlTotalLabel(control.status).toLowerCase()}${total}`, state: "attention", source });
    }
    if (control.status === "differs" || control.status === "not_verified") warnings.push({ tone: "warning", text: `${source.label}: ${control.note}` });
    if (control.formula) technical.push({ label: `Control total · ${source.label}`, value: `${control.reference} ${control.formula} (no stored result)` });
  }
  for (const { document, table } of tables) {
    const label = labels.get(document.filename) ?? document.filename;
    technical.push({ label: `Table · ${label}`, value: `headers ${table.headerReference}; records ${table.recordReference ?? "none"}; quantities “${table.quantityHeading}” ${table.quantityReference ?? ""}`.trim() });
    if (table.investor) technical.push({ label: `Investor label · ${label}`, value: `${table.investor.label} at ${table.investor.labelReference}, value at ${table.investor.reference}` });
    if (table.statedDate) technical.push({ label: `Stated date · ${label}`, value: `${table.statedDate.text} at ${table.statedDate.reference}` });
  }
  for (const document of documents) {
    technical.push({ label: `Date · ${labels.get(document.filename) ?? document.filename}`, value: `${document.date ?? "undated"}${document.dateBasis ? ` (from the ${document.dateBasis})` : ""}` });
  }
  technical.push({ label: "Order", value: `Earlier → later by ${comparison.orderBasis}` });

  return {
    earlierDate, laterDate,
    investor: investors.length === 1 ? investors[0] : null,
    isin: isins.length === 1 ? isins[0] : null,
    quantityHeading: [...new Set(tables.map(({ table }) => table.quantityHeading))].join(" / ") || null,
    quantityLabel: (() => {
      const headings = [...new Set(tables.map(({ table }) => table.quantityHeading))];
      return headings.length === 1 ? `Recorded ${headings[0].charAt(0).toLowerCase()}${headings[0].slice(1)}` : "Recorded quantity";
    })(),
    evidenceRole: null,
    metrics,
    reconciliation,
    showIsinColumn: isins.length > 1 || rows.some((row) => !row.isin),
    showInvestorColumn: investors.length > 1,
    rows: rows.map((row, index) => {
      const difference = row.status === "compared" ? row.difference : null;
      const direction = difference === null ? "not_compared"
        : /^-/.test(difference) ? "decrease" : /^[+-]?0+(?:\.0+)?$/.test(difference) ? "none" : "increase";
      return {
        key: `${row.isin ?? ""}:${row.account ?? ""}:${index}`,
        subfund: row.entities.filter((entity) => entity.kind !== "investor").map((entity) => entity.name).join(" / ") || "—",
        investor: row.entities.filter((entity) => entity.kind === "investor").map((entity) => entity.name).join(" / ") || "—",
        account: row.account ?? "—",
        isin: row.isin ?? "—",
        measure: measureOf(row),
        earlier: cell(row, row.earlier),
        later: cell(row, row.later),
        difference: { display: difference === null ? "—" : formatQuantity(difference, places, true), direction },
        status: row.status === "compared" ? null : holdingStatusLabel(row.status),
        note: row.note,
      };
    }),
    totals: totals?.earlier != null && totals.later != null && totals.difference != null ? {
      earlier: formatQuantity(totals.earlier, places), later: formatQuantity(totals.later, places),
      difference: formatQuantity(totals.difference, places, true), measure: totals.currency ?? "",
      label: fullTotals ? "Total" : "Total of compared rows",
    } : null,
    totalsNote: totals?.note ?? null,
  };
}

/** Build the answer-first presentation for one completed question. */
export function buildAnswerView(input: AnswerInput): AnswerView {
  const comparison = input.holdingsComparison ?? null;
  const inventory = input.inventory ?? null;
  const layout: AnswerLayout = comparison ? "holdings"
    : inventory && input.intent === "relationship" ? "relationships"
      : inventory ? "entities" : "prose";
  const allFilenames = [
    ...input.citations.map((citation) => citation.filename),
    ...(comparison?.documents ?? []).map((document) => document.filename),
    ...(comparison?.caveats ?? []).flatMap((caveat) => caveat.sources.map((source) => source.filename)),
    ...(inventory?.entities ?? []).flatMap((entity) => entity.evidence.map((item) => item.filename)),
    ...(inventory?.relationships ?? []).map((relation) => relation.filename),
  ];
  const labels = documentLabels(allFilenames);
  const warnings: Notice[] = [];
  const limitations: string[] = [];
  const technical = baseTechnical(input);
  const view: AnswerView = {
    layout, title: TITLES[input.intent] ?? "Answer", method: methodLabel(input), scopeLabel: answerScopeLabel(input.documentScope),
    conclusion: null, prose: [], holdings: null, entities: [], relationships: [], warnings, limitations,
    statedCaveats: [], evidence: groupEvidence(input.citations), technical,
  };

  if (comparison) {
    const legal = input.intent === "legal_status";
    const localLegal = legal && input.model === "local-legal-status-holdings";
    if (!legal && input.intent !== "holdings_comparison") view.title = TITLES.holdings_comparison;
    const holdings = holdingsView(comparison, labels, warnings, technical);
    view.holdings = holdings;
    const rows = comparison.rows ?? [];
    const compared = rows.filter((row) => row.status === "compared").length;
    const changed = holdings.rows.filter((row) => row.difference.direction === "increase" || row.difference.direction === "decrease").length;
    if (legal) {
      // The legal-status answer leads; the dated records are its factual evidence.
      holdings.evidenceRole = "Factual evidence: dated holding records";
      if (localLegal) {
        view.conclusion = `The holdings spreadsheets record ${plural(changed, "changed position")} between ${holdings.earlierDate} and ${holdings.laterDate} (below). `
          + "Neither spreadsheet records an assignment or transfer of legal claim rights, and no other document was used.";
      } else {
        view.prose = input.unknown || !input.answer?.trim()
          ? [{ kind: "paragraph", text: "The evidence in scope does not establish an answer to this question." }]
          : proseBlocks(input.answer);
      }
    } else if (rows.length === 0) {
      view.conclusion = "No comparable positions were found in the two spreadsheets.";
    } else {
      const net = holdings.totals ? `; the net difference across them is ${holdings.totals.difference}` : "";
      view.conclusion = `Between ${holdings.earlierDate} and ${holdings.laterDate}, ${changed} of ${plural(compared, "matched position")} changed${net}.`
        + (rows.length > compared ? ` ${plural(rows.length - compared, "position")} could not be compared.` : "");
    }
    for (const issue of comparison.issues ?? []) warnings.push({ tone: "warning", text: issue });
    for (const entry of comparison.requestedEntities ?? []) {
      if (entry.match === "alias" && entry.name) warnings.push({ tone: "info", text: `“${entry.text}” was read as ${entry.name}, the only matching name in the spreadsheets.` });
    }
    if (rows.length > compared) warnings.push({ tone: "warning", text: `${plural(rows.length - compared, "position")} could not be compared; the table shows why and the stored values.` });
    limitations.push("Recorded holding changes do not by themselves establish a securities transfer or an assignment of legal claim rights.");
    if (localLegal) {
      limitations.push("Establishing an assignment of legal claim rights needs explicit evidence, such as an assignment agreement or notice.");
    } else if (comparison.legalQuestionNotAnswered && !legal) {
      limitations.push("Legal claim status: the compared spreadsheets record holdings only. They do not establish whether legal claim rights were assigned, transferred or withdrawn, and no other document was used for this part of the question.");
    }
    view.statedCaveats = (comparison.caveats ?? []).map((caveat) => ({
      text: caveat.text,
      sources: caveat.sources.map((source) => sourceLink(source.documentId, source.filename, source.reference, labels)).filter((link): link is SourceLink => link !== null),
    }));
  } else if (inventory) {
    const coverage = inventory.coverage;
    if (layout === "relationships") {
      const relationships = inventory.relationships ?? [];
      const documented = relationships.filter((relation) => relation.kind !== "same_row").length;
      const sameRow = relationships.length - documented;
      view.conclusion = relationships.length === 0
        ? `No pairing of labelled names was found in the ${plural(coverage.searchedDocuments, "searchable document")} used.`
        : `${plural(relationships.length, "pairing")} found: ${plural(documented, "documented association")}`
          + (sameRow ? `, ${sameRow} same-row ${sameRow === 1 ? "association" : "associations"} only` : "") + ".";
      view.relationships = relationships.map((relation) => ({
        key: `${relation.documentId}:${relation.subjectReference}:${relation.relatedReference}`,
        subject: relation.subject, subjectRole: entityRoleLabel(relation.subjectRole),
        related: relation.related, relatedRole: entityRoleLabel(relation.relatedRole),
        basis: relationshipKindLabel(relation.kind), documented: relation.kind !== "same_row",
        sources: [relation.subjectReference, relation.relatedReference]
          .map((reference) => sourceLink(relation.documentId, relation.filename, reference, labels))
          .filter((link): link is SourceLink => link !== null),
      }));
      if (relationships.length > 0) {
        limitations.push("These associations show how the documents list the names. They do not prove legal ownership, separate legal personality, claimant status, or invoicing responsibility.");
      }
      if (sameRow > 0) {
        limitations.push("A same-row association only means both names appear in one row; the source does not state a relationship.");
      }
    } else {
      const entities = inventory.entities ?? [];
      view.conclusion = entities.length === 0
        ? `No explicitly labelled entities of the requested kind were found in the ${plural(coverage.eligibleDocuments, "searchable document")} in scope.`
        : `${plural(entities.length, "entity", "entities")} found in ${coverage.documentsWithListedEntities} of ${plural(coverage.eligibleDocuments, "searchable document")}.`;
      view.entities = entities.map((entity) => ({
        key: entity.name, name: entity.name,
        detail: entity.documentCount > 1 ? `${entity.documentCount} documents` : null,
        role: entity.roles.map(entityRoleLabel).join(" / "),
        uncertainty: entity.uncertainty,
        related: entity.related.length ? entity.related.map((related) => `${related.name} (${entityRoleLabel(related.role)})`).join("; ") : "—",
        sources: entity.evidence
          .map((item) => sourceLink(item.documentId, item.filename, item.sourceReference, labels))
          .filter((link): link is SourceLink => link !== null),
      }));
      const conflicts = entities.filter((entity) => entity.uncertainty).length;
      if (conflicts) warnings.push({ tone: "warning", text: `${plural(conflicts, "entity has", "entities have")} unresolved or conflicting role evidence.` });
      const others = Object.entries(inventory.otherRoleCounts ?? {});
      if (others.length) warnings.push({ tone: "info", text: `Also labelled but not requested: ${others.map(([role, count]) => `${count} × ${entityRoleLabel(role).toLowerCase()}`).join(", ")}.` });
      if (coverage.entitiesOmitted) warnings.push({ tone: "warning", text: `${coverage.entitiesOmitted} further entities were found but not listed.` });
      limitations.push("Roles are provisional. Appearing in the same record does not establish ownership, claimant status, or invoicing responsibility.");
    }
    if (coverage.partial) {
      warnings.push({ tone: "warning", text: `Partial coverage: ${plural(coverage.partialDocuments.length, "document")} exceeded the local scanning limit (${coverage.partialDocuments.map((document) => document.filename).join(", ")}), so this result may be incomplete.` });
    }
    technical.push({ label: "Coverage", value: `${coverage.searchedDocuments} of ${coverage.eligibleDocuments} searchable documents searched; ${coverage.documentsWithRoleLabels} with role labels` });
  } else {
    view.prose = input.unknown || !input.answer?.trim()
      ? [{ kind: "paragraph", text: "The evidence in scope does not establish an answer to this question." }]
      : proseBlocks(input.answer);
    if (input.intent === "legal_status") limitations.push("This reports what the cited documents record. It is not a legal conclusion on entitlement or claim status.");
    if (input.intent === "calculation") limitations.push("Illustrative calculation from the cited instruction; it is not an issued invoice.");
  }
  // For structured layouts the backend's plain-text summary repeats the
  // table; keep it only as a technical record.
  if (layout !== "prose" && view.prose.length === 0 && input.answer?.trim()) technical.push({ label: "Plain-text summary", value: input.answer });
  warnings.unshift(...scopeNotices(input.documentScope, view).filter((notice) => notice.tone === "warning"));
  warnings.push(...scopeNotices(input.documentScope, view).filter((notice) => notice.tone === "info"));
  return view;
}
