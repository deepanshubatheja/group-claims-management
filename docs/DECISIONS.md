# Architecture decisions

## 2026-10-14 — Local persistence boundary

Phase 4 uses Electron's `app.getPath('userData')` as the authoritative per-user
storage root and passes it only as a fixed Python child-process startup argument.
The backend validates the root, initializes private managed directories, and
uses SQLite's standard library driver with transactional numbered migrations.
Document copies remain local, immutable managed files named by generated IDs;
SQLite stores their relative paths and SHA-256 hashes. No public local server,
cloud storage, or automatic encryption is introduced.

## 2026-10-14 — Local PDF OCR fallback

Phase 5.2 uses PyMuPDF for deterministic coordinate-aware native PDF text blocks. Tesseract is a
strictly local, per-page fallback only when a page has no usable native text layer. Raw text,
cleaned presentation text, page coordinates, engine/language metadata, confidence where available,
and quality warnings are stored with a versioned extraction attempt. No OCR content leaves the
device; incomplete OCR is never represented as reliable extraction. Future packaged builds must
bundle the reviewed OCR runtime and language data rather than requiring an end-user installation.

## 2026-10-14 — Deterministic extraction inspection

Spreadsheet extraction attempts retain worksheet position, row, and column for new parses. The
document viewer retrieves a bounded worksheet grid in that order rather than UUID insertion order;
it represents omitted blank positions visually and preserves stored values and formulas unchanged.
Older extraction attempts without a worksheet position use a stable sheet-name fallback and can be
reprocessed from their immutable vault copy to capture original sheet order. PDF presentation keeps
raw blocks and cleaned text separate, with warnings whenever layout or hyphenation cleanup may
affect reading order.

## 2026-10-14 — OpenAI provider adapter boundary

Phase 6.2A adds a Python-only OpenAI Responses API adapter using `gpt-4.1-mini`
as the configurable initial model candidate. It requests strict JSON-schema
output, disables response storage with `store=false`, sets a bounded timeout and
retry policy, and exposes no tools. `OPENAI_API_KEY` is read only by the Python
process and is never stored, logged, or exposed over Electron IPC. The adapter
has no document access or automatic call path; it can be exercised only with an
explicit caller and synthetic data until Phase 6.2B adds document-level consent
and content preparation.

## 2026-10-14 — Bounded, evidence-linked AI document analysis

Phase 6.2B keeps OpenAI calls in the Python backend and starts them only after
both a global authorization setting and a deliberate per-document Analyze action.
It prepares bounded chunks from existing extraction rows and PDF text blocks; it
never uploads a vault file by default. Each asynchronous, versioned job records
provider/model, prompt/schema version, coverage, and a non-sensitive audit event.
Model output is accepted only as a provisional proposal when its document,
extraction, and source reference resolve locally, its evidence snippet matches
the extracted source, and any proposed ISIN passes check-digit validation.
Unsupported evidence is discarded. No proposal populates confirmed domain or
billing tables.

## 2026-10-14 — Development OpenAI cost ceiling

Phase 6.2D makes paid document analysis opt-in and bounded before the first
live test: one selected document, one request, 4,000 full-request characters,
1,500 conservatively estimated input tokens, 1,000 output tokens, no automatic
paid retries, and a USD 0.01 estimated-cost ceiling by default. The Responses
SDK has `max_retries=0`; a higher bounded retry count is an explicit developer
environment override. The cost estimate uses a documented model-specific local
pricing profile and is checked before any provider transmission. The plan reports
included and excluded deterministic extraction coverage; it never silently
claims excluded content was analyzed. Actual response token usage and a local
usage-based cost estimate are retained with the job, never payloads, credentials,
or a claimed OpenAI billing balance.

The output ceiling was raised from 800 to 1,000 tokens after an explicitly
authorized live analysis reached the former structured-output limit. The
one-request limit, zero automatic retries, USD 0.01 estimate ceiling, and
document/global authorization requirements remain unchanged. The prompt now
requests no more than 10 concise, non-duplicative high-value proposals, so a
bounded preliminary analysis does not waste its response allowance on repeated
facts.

## 2026-10-15 — Evidence validation diagnostics and CSV provenance

Phase 6.2F records proposal-returned, schema-valid, evidence-validated,
persisted, and controlled rejection-reason counts inside the existing
non-sensitive AI job coverage metadata; no raw provider response or document
text is retained. New CSV extractions use stable `CSV!A1` references compatible
with the spreadsheet viewer and AI prompts. Existing CSV records are not
rewritten: their legacy row/column provenance resolves only through exact stored
coordinates, preserving previous extraction history while allowing a model's
equivalent CSV A1 reference to be validated.

Source labels are rendered in square brackets in AI prompts for readability,
but are stored without that display punctuation. The validation boundary accepts
only surrounding whitespace and one pair of outer square brackets before an
otherwise exact local evidence lookup, then persists the canonical identifier.
This corrects a presentation-format mismatch without accepting translated or
fabricated source references.

## 2026-10-15 — Resilient structured-response handling

The OpenAI response schema now enforces the backend's maximum of five proposals.
The provider validates the completed response envelope and parses SDK output
using the supported convenience property with a bounded local fallback. A single
proposal that violates a local cross-field rule is recorded only as a controlled
schema-validation rejection in job coverage; it does not invalidate the entire
completed response or enter a confirmed domain table.

The proposal and prompt schema versions were advanced for this bounded-response
contract. It requests no more than five concise, non-duplicative provisional
findings with short literal evidence snippets. This prevents a cell-heavy
document from exhausting the one-request output budget while retaining strict
evidence validation. It is explicitly a preliminary result set, not a claim of
complete source interpretation.

## 2026-10-15 — OpenAI endpoint pinning

The OpenAI adapter pins its SDK client to the official OpenAI API endpoint and
does not inherit `OPENAI_BASE_URL`. This prevents an unrelated development-shell
proxy setting from redirecting authorized extracted content or API credentials.
It also clears inherited `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` values before
a request, because a project API key is already scoped and stale routing metadata
can conflict with it. A legacy key can deliberately opt in through the
app-specific `GROUP_CLAIMS_OPENAI_ORG_ID` and
`GROUP_CLAIMS_OPENAI_PROJECT_ID` variables.

## 2026-10-15 — Explicit OpenAI metadata authentication check

The application records provider authentication state only after a user
explicitly requests a connection check in Settings. The Python-only check calls
the documented OpenAI Models metadata endpoint through the official SDK; it
sends no document content and creates no model response. Its non-sensitive,
controlled outcome is stored as an audit event (`available`, authentication,
permission, rate-limit, timeout, network, or `provider_error`) and is
shown separately from mere API-key presence. It is never invoked on startup,
import, selection, refresh, or a timer. Existing document-analysis limits and
consent controls remain unchanged.

The complete SDK client-construction and Models-list path is inside the same
controlled error boundary. An unexpected SDK failure is returned as
`provider_error`, never as a generic NDJSON backend exception or provider
response detail. Historical `request_failed` audit events are read as the new
`provider_error` status without rewriting user data.

## 2026-10-15 — Lawyer-facing AI review and cited document Q&A

The Documents workspace presents evidence-validated provisional findings rather
than the raw spreadsheet/PDF parser view. Deterministic extracted content and
coordinates remain in the local provenance store; they are not deleted or
altered, but are not the default lawyer-facing presentation.

Document Q&A is a separate, per-question OpenAI Responses request. Before a
question leaves the device, the user must prepare its bounded scope, see the
provider/model, estimated cost, excluded coverage and output limit, then tick a
fresh consent checkbox and deliberately press **Ask AI**. The Python process
constructs context from current evidence-validated findings plus a bounded
deterministic source excerpt; no file path, vault file, raw IPC capability, or
chat history is exposed to React. Answers must use strict JSON and cite local
source references whose literal snippets are revalidated against stored
extraction evidence. Unsupported answers are discarded. Q&A is in-memory for
the current backend session so neither questions nor answer text are added to
SQLite or operational logs; no confirmed domain table is ever written.

## 2026-10-15 — Evidence-linked document-analysis summary

Each newly authorized document analysis requests one short, provisional summary
in the same bounded OpenAI response as its findings; it does not create a
second provider request. The summary is shown only when at least one cited
reference resolves to locally extracted evidence. It is retained with the
versioned analysis job, never promoted into confirmed domain records, and is
explicitly limited to the material covered by that analysis. Historical jobs
are preserved and may have no stored summary.

The Q&A response contract always requires a short answer string and at least
one exact citation. When the model marks an answer as unknown, the backend
discards its required placeholder answer string and displays a controlled
no-conclusion state with the cited context instead. This removes an ambiguous
schema/local-validator mismatch without weakening evidence validation.

Provisional MVP Billing and Legal Rules

We will proceed using the available pseudonymised client documents and illustrative examples rather than waiting for finalized client-specific legal and billing agreements.

Implementation decisions:

1. Implement a configurable, deterministic pro-rata billing engine using eligible amounts divided by total eligible amounts multiplied by the configured fee pool.
2. Use Python Decimal for financial calculations.
3. Support both parent-level and individual subfund invoice recipients, but do not infer the correct recipient without explicit mapping.
4. Preserve dated holdings and detect additions, reductions, and changes.
5. Represent security transfers and legal claim/right assignments separately.
6. Do not automatically assume legal claim eligibility from security ownership.
7. Preserve original currencies and require explicit conversion/tax settings when applicable.
8. Use existing sample documents for parsing and reconciliation tests, and synthetic billing fixtures for calculations.
9. Flag missing information or material inconsistencies rather than inventing values.
10. Keep fee rules configurable and versioned so we can change them later without restructuring the application.
11. Label any outputs produced using provisional assumptions as provisional, not legally approved invoices.
12. Do not implement application-level local storage encryption in the current MVP.

Do not implement billing functionality now. Record this decision for the future billing milestone.

Preserve the existing architecture and guardrails. Do not commit without approval.

## 2026-10-15 — Consolidated matter review and non-persistent billing preview

The Matter Review workspace reads only evidence-validated, provisional findings
from the most recent completed AI review for each document the user selects. It
does not merge legal entities, holdings, claims, or invoice recipients, and it
does not send any content to an AI provider. Documents without a completed
review are reported as absent from the consolidation rather than represented by
guessed data.

The accompanying pro-rata result is a transient calculation preview. The user
must explicitly enter its fee pool, currency, and each eligible allocation
basis. Python uses `Decimal` and a largest-remainder cent allocation so the
preview reconciles exactly to the entered pool. No fee agreement, invoice,
invoice line, payment, or confirmed domain record is created. Legal claim
eligibility, tax, FX, recipient, and final fee-rule decisions remain inputs for
a later approved billing workflow.

## 2026-10-15 — Explicit bounded consolidated analysis

Matter Review may prepare an explicit batch analysis for selected parsed
documents; it is not triggered by import, selection, startup, or refresh. The
local-only plan shows each document's bounded scope and a combined estimate
before a separate batch consent action can start any transfer. The development
batch is limited to 20 documents, one sequential request per document, zero
automatic retries, and a USD 0.20 aggregate estimate by default (reducible by
environment configuration). Existing per-document limits still apply. Each
document remains a separately versioned AI job, so its evidence-linked summary
and provisional findings can be inspected and consolidated without repeating a
provider request. Withdrawal, authentication, permission, or rate-limit
failure blocks pending batch requests; completed jobs remain preserved.

Consolidated Q&A is a separate, explicitly authorized request. It is limited
to evidence-linked provisional findings from the selected completed analyses,
requires fresh question-specific consent and locally revalidates every returned
citation against the relevant extraction record. Questions and answers are
in-memory only and never create confirmed domain records.

## 2026-10-15 — Participation and rights question retrieval

For a consolidated question about litigation participation, claims, rights,
assignments, transfers, withdrawals, or settlements, the Python backend adds a
small, bounded set of question-relevant deterministic source excerpts to the
already authorized Q&A context. It prioritizes matching English and German
legal/rights terms, retains only stable source references, and re-resolves every
returned citation against local extraction data. This corrects the earlier gap
where a relevant statement outside a document's preliminary proposals could not
be considered by Matter Review Q&A. It does not treat a holding, account, or
security movement as evidence of litigation participation, and it does not
create confirmed legal, claims, or billing records.

## 2026-10-16 — Standalone macOS ClaimFlow packaging

The initial distribution build uses `electron-builder` for an Apple Silicon
DMG and PyInstaller in one-directory mode for the Python backend. Electron
launches that immutable backend executable from its packaged resources using
the existing private NDJSON stdin/stdout protocol; no HTTP service or
development Python environment is introduced. Mutable databases, documents,
exports, logs, backups, and settings remain under Electron's per-user data
directory, outside both the DMG installation and the repository.

The macOS release builder also copies Tesseract plus English and German
language data into the app resources. The backend receives those trusted paths
only from Electron in packaged mode and reports local OCR as unavailable if a
future platform build omits them. The beta DMG is unsigned and not notarized;
it is not a production release. Windows NSIS configuration is prepared, but
Windows packaging and clean-machine verification remain outstanding.

## 2026-10-16 — Packaged macOS OpenAI credential storage

The packaged macOS application stores a user-provided OpenAI API key as a
non-synchronizing Keychain generic-password item. A small native Security
framework helper receives the key only on stdin, and Electron main receives it
only on a private stdout pipe before launching the Python backend. The renderer
can save, replace, remove, and learn the presence of a credential through typed
allowlisted IPC, but cannot read a stored key. The key is never placed in
SQLite, configuration files, logs, command-line arguments, or installer assets.
Connection checks remain explicit Models metadata requests and document AI work
continues to require existing global and per-request authorization.

## 2026-10-16 — Document-backed provisional pro-rata answers

Matter Review can produce an in-memory, local pro-rata calculation only when
one selected parsed document expressly supplies an approved instruction, a fee
pool and currency, an allocation rule based on eligible nominal amounts, and
the named eligible participants with their bases. Python uses `Decimal` and the
existing largest-remainder cent allocation. The result cites that instruction,
does not contact OpenAI, and is labelled as a provisional calculation preview,
not an invoice, billing record, or legal-entitlement decision. If zero or more
than one qualifying instruction is selected, ClaimFlow does not infer or choose
a billing policy; the normal evidence-linked Q&A path remains available.

Opaque provider citation handles are never shown in the lawyer-facing answer.
They are replaced with the resolved local filename and source reference before
the answer is rendered. Follow-up questions retain their ordinary local-source
retrieval scope rather than treating a prior answer or a provider handle as
evidence.

## 2026-10-16 — Local Codex/ChatGPT connection for AI review

The desktop app now selects `codex` by default for its Python-owned AI review.
The user explicitly starts browser sign-in from Settings. The backend runs the
published `openai-codex` Python SDK with its pinned local Codex runtime, an
app-data `CODEX_HOME`, and OS keyring credential storage. It does not use an
OpenAI API key for this path. The previous API-key adapter remains available.

Connection and consent are separate. Sign-in and metadata checks send no
document content. Every analysis or question still requires existing global
cloud authorization and a specific user action. Only bounded extracted text is
sent; original files remain local. Codex uses ephemeral, read-only threads
with approvals denied; shell, connector, multi-agent, and web-search features
are disabled for this dedicated runtime. Its JSON responses still pass the
same local schema, evidence, and citation checks before display or persistence.
The app does not log prompts, OAuth URLs, credentials, or raw model responses.

Codex subscription usage is governed by the connected ChatGPT plan, not the
OpenAI API pricing profile. Previews therefore show no USD estimate or USD
limit for Codex; local input-size and request-count bounds still apply. The
Codex SDK path cannot enforce the Responses API's output-token cap, and the UI
does not claim that it can.

This app-server managed sign-in is for the local single-user/internal build.
Official OpenAI documentation permits existing local or open-source app-server
integrations but says managed app-server authentication is not permitted for
commercial or hosted services. Before distributing ClaimFlow commercially,
replace this login flow with the official Sign in with ChatGPT integration
and complete its required onboarding/review.

## 2026-10-17 — Per-user AI provider selection

Settings can select `codex` or `openai` and saves only that provider name in a
small per-user app-data file. A saved choice overrides the development
`GROUP_CLAIMS_AI_PROVIDER` fallback. The backend restarts on a change, and
cloud-processing authorization is withdrawn before the new provider is used;
the user must enable it again for the selected provider. Credentials remain in
their existing OS-protected stores and are never copied into this settings file.

## 2026-10-21 — Explicitly scoped Matter Review questions

ClaimFlow has no separate matter table: the single-user local workspace is one
implicit matter with the fixed identifier `workspace`. Every Matter Review
question now names that matter and an explicit scope: **all documents in this
matter** (the default, requiring no selection) or **selected documents only**.
Python validates the matter, resolves all-document scope from `source_documents`
itself, rejects any selected ID that is not a matter document, and never widens
an empty selection to all documents. Scope is resolved before any retrieval,
context preparation, or provider request. A real matters model, if approved,
would replace only this resolution step.

Retrieval no longer depends on a completed per-document AI review or on the
first 2,400 extracted rows. A derived SQLite FTS5 index (trigram tokenizer, so
existing case-insensitive substring rules and German compounds still match)
covers each document's current parsed extraction: spreadsheet/CSV cells and
PDF text blocks (page-level rows only for legacy page-only PDFs). It is built
lazily on the first scoped question, pruned when a document is reparsed, and
deleted with the document. It stores a copy of extracted text in the local
database, like the extraction tables it derives from; it is never evidence by
itself. Hits are interleaved across documents, capped (40 per document, 400
total), filtered by the unchanged relevance rules, and every candidate is
re-resolved against `extracted_content`/`pdf_text_blocks` before it can be
sent or cited. Short cells and blocks carry bounded neighbouring row/block
context. Existing input-character, token, cost, one-request, and zero-retry
limits are unchanged; partial coverage, unsearchable documents (not parsed,
failed, unsupported, no text), and incomplete OCR pages are reported rather
than implied to have been examined. Earlier AI review summaries/findings are
used only when produced from the document's current extraction.

Consent is document-specific. The local preview lists every document whose
excerpts would be transmitted and whether it was already authorized for the
current provider since cloud processing was last enabled (a prior document
analysis or matter question). New documents require an explicit checkbox; the
ask request must name all transmitted documents, and Python rejects it if the
evidence set changed. Each consent is audited per document without content.
Follow-ups reference an earlier backend-held turn by ID; React can no longer
supply previous question/answer text. A follow-up is refused when that turn's
source documents fall outside the new scope, so narrowing the scope cannot
carry excerpts from excluded documents into a new request. Questions and
answers remain in memory only. Answers carry their scope label, citations are
grouped by document and open the cited cell or PDF block in the Documents
viewer, and the prompt asks the model to surface disagreements between
documents with one citation from each rather than choosing a source. The
provider contract still allows at most two citations per answer.

## 2026-10-21 — Question intent routing and local entity inventories

A listing question such as "list all investors, investment vehicles, and
subfunds" was answered from at most ~3 excerpts: the fixed instructions use
about 2,800 of the 4,000-character development request ceiling. The word
"across" also routed it as a comparison, so its incomplete coverage replaced
the provider answer with a generic cited fallback. The general term extractor
kept filler words ("documents", "uploaded") and plural forms that do not
match singular labels.

Matter Review now classifies each question deterministically (calculation,
status/change, comparison, inventory, specific lookup, general) to choose a
retrieval strategy only. Inventory questions (a listing cue and a role noun)
are answered locally without a provider request or cost: every eligible
in-scope document's indexed extraction is examined for explicit role
evidence — spreadsheet column headings, "Role: name" labels, "X, a sub-fund of
Y" wording — plus earlier evidence-validated AI review names, which keep
their broader provisional type. Names merge only when identical apart from
case, spacing, and surrounding punctuation. Same-row values are shown as an
association, never as ownership, legal personality, claimant status, or an
invoicing relationship. Conflicting role evidence stays unresolved. Unlabelled
organisation names are not classified. Local scan limits (hits, table rows,
cells, listed entities) are reported as partial coverage. Specific lookups
search their literal identifiers; comparisons take one excerpt per document
first. The general path drops filler words and plural suffixes. Paid-request
limits are unchanged.

Extraction-cell evidence text previously ended with a literal "\n" because
SQLite does not interpret that escape; it now uses a real newline, so cited
spreadsheet snippets display the stored value exactly.

## 2026-10-21 — Explicit document constraints, relationships, and dated holdings comparisons

Two live questions naming the March 28 and April 30 holdings workbooks were
answered from an unrelated email and a PDF address block. Both were routed as
"status/change" because the change keyword list matches "holdings" (and
"difference"); that path replaced the question's own terms with ~55 generic
change words, ignored the named files and funds, and had no relevance check,
so high-overlap PDFs filled the bounded prompt.

Questions that name documents by exact file name, or by a date together with a
document word ("spreadsheets dated March 28"), now resolve those documents from
the matter's stored file names and formats only; dates are not inferred from
content. The references only narrow the authorized scope; an excluded, missing,
or ambiguous reference produces a local clarification and nothing is sent.

Intent precedence is: pro-rata calculation, legal claim status, dated holdings
comparison, entity relationship mapping, inventory, change/transfer analysis,
comparison, exact identifier lookup, general. Relationship questions are
answered locally: each pairing shows the exact cells of both names and is
labelled as an explicit statement, an explicit relationship heading (e.g.
"Umbrella fund"), or a same-row association that documents no legal
relationship. Dated holdings comparisons are calculated locally with `Decimal`
from header-identified ISIN/quantity/unit/account columns of exactly two
spreadsheets ordered by file-name date. Rows match only on identical entity,
account and ISIN; duplicates are not summed; formula cells (stored without a
calculated value), ambiguous number formats, ambiguous quantity headings, and
unit mismatches are reported with their cells instead of calculated. A quantity
difference is never presented as a trade, transfer, or change in claim rights.

On the provider path, capitalised names and identifiers in the question must
appear in each excerpt, its spreadsheet row, or a heading row above it before
it can be sent; names absent from the scope produce a targeted local answer. A
follow-up borrows the earlier question's wording for retrieval only when it
names no documents, entities or identifiers of its own, and an earlier answer
is dropped as context when it relied on documents outside those the new
question names.

## 2026-10-22 — Primary operation routing, holdings control totals and separate legal caveats

Legal-status and billing keyword rules are evaluated on the question without
its caveat requests and negative instructions ("include legal-transfer
caveats", "must not be treated as an assignment of legal claim rights"), so
wording that only qualifies the answer no longer selects the route. An
explicit quantitative holdings comparison (compare / differences / quantities
on both dates) is the primary operation even when the question also asks a
legal question; that part is declined in a separate caveat instead of being
answered from the comparison. Affirmative legal questions still route to legal
claim status.

The dated holdings comparison reads complete rows under their header (cell
references for every column), recognises an "Eligible nominal amount"-style
quantity heading, a separate currency column, a sheet-level "Investor / legal
entity" label and an "As of date" label (a disagreement with the file-name
date is reported, not resolved). A control-total row is checked against the
exact `Decimal` total of the table's own record rows; a `=SUM(...)` formula
without a stored result is reported as covering exactly those rows only when
its range equals them. Column totals are shown only for one ISIN, currency and
unit. Legal wording elsewhere in the sheet is returned only as a cited caveat,
never as a holding value. A question that names documents still only narrows
the authorized scope, and the answer's scope label now says so.

## 2026-10-22 — Answer-first Matter Review presentation

Matter Review answers render from the typed payloads Python already returns,
through one presentation model (`answer-presentation.ts`) and one card
(`MatterAnswer.tsx`): conclusion → structured result (holdings metrics and
table, entity or relationship table) → material warnings → short legal
limitations (with any caveat the source itself states) → collapsible
supporting evidence grouped by document and row/page → collapsible technical
details (ranges, formulas, date bases, scope mechanics, plain-text summary).
Nothing is parsed out of answer prose; for structured answers the backend's
narrative is kept only in technical details so values are not shown twice.
Quantities are display-formatted from the exact decimal strings (grouping,
padding to one precision, true minus sign) and never rounded; values the
backend could not use are shown verbatim. Differences are styled neutrally and
no currency symbol is added to nominal amounts. Model and document text is
rendered as React text; no Markdown or HTML rendering was added. A holdings
answer states directly that the compared spreadsheets do not establish legal
claim status instead of telling the user to ask again.

## 2026-10-22 — Section-level investor associations and grounded legal-rights answers

A spreadsheet label cell whose row holds only that label and one value
(e.g. "Investor / legal entity" | "Northstar Investors LP") is read as a
labelled value. When an investor label directly precedes a table of subfund
rows (no other table header in between, no investor column of its own), each
subfund is paired with that investor as a "document_section" association,
citing both cells. It is presented as a documented association in the source
layout, never as ownership, separate legal personality, claimant status or
invoicing responsibility.

Earlier AI review summaries and findings now pass the named-entity relevance
gate only on the source text they cite, so a summary that names a fund but
cites an unrelated cell (a sheet title) is no longer sent or cited. A
legal-status question that refers to recorded holding movements, with exactly
two holdings spreadsheets in scope, gets the dated quantity rows from a local
`Decimal` comparison as primary evidence and the spreadsheets' own caveat as
supporting context. If those are the only documents in scope and they contain
no other rights-related wording, the answer is local; otherwise the holdings
record is pinned first in the provider context (other evidence, such as an
assignment notice, follows), and the dated cells are cited with the answer.

## 2026-10-22 — Requested entities and control-total reconciliation in holdings comparisons

A holdings comparison now takes its requested entities from the names actually
present in the scoped spreadsheets that appear in the question; capitalised
question text is used only for the remainder. A full stop ends an extracted
name (except after a title such as "No." or "St."). Shorthand such as "Fund A"
resolves only to the single name ending with those words (preferring names the
question already gave); otherwise it is reported as ambiguous or, when it reads
like an entity name, as missing, and the comparison is marked incomplete.
Every requested entity is listed with how it matched and its position count.

Comparison totals are labelled as full holdings totals only when the compared
rows are every record row of both tables. A source control total is shown as
reconciling the comparison only when it covers exactly the compared rows and
its record total equals their exact `Decimal` total; otherwise it is reported
as checking its own rows only, or as failed.

## 2026-10-22 — Structured pro-rata billing answers

A document-backed allocation request now returns a typed `billingAllocation`
payload alongside the plain-text answer. The allocation itself is unchanged.
Python `Decimal` allocates cents using the largest-remainder method, and those
cent amounts are the ones displayed. The payload carries the following:

- the instruction's stated inputs, each with its block reference: fee pool,
  currency, billing period, allocation rule and "Approved by";
- the eligible participants, with eligible nominal amount, exact share,
  display percentage, allocated amount, unrounded amount and rounding cent;
- the expressly excluded participants, with the stated reason and a zero
  allocation (a pro-rata pool shared among the eligible participants leaves an
  excluded one with no part of it);
- an investor group only when the instruction states one.

Exclusions are recognised in both the listed form ("Excluded participant /
Name (ID): …") and the sentence form ("Name (ID) is excluded because …").

**Undetermined subfunds.** A subfund that the in-scope spreadsheets list but
the instruction does not name is shown as "Undetermined". It never gets a
zero bill.

**Narrow cross-check, not reconciliation.** Each eligible amount is compared
with holding records in scope that meet all of these conditions:

- dated on the instruction's stated as-of date, or failing that the
  billing-period end;
- naming exactly one participant;
- stating one nominal quantity;
- for the instruction's ISIN, when the instruction names one.

A differing record is reported as an unresolved discrepancy. The instruction
amount is kept, and `issuanceBlocked` is set. Records on other dates, records
naming several participants, and documents outside the authorized scope are
not used.

**Incomplete or conflicting instructions.** If an allocation request finds an
instruction missing a required input, or finds two approved instructions, it
gets a local "No allocation was calculated" answer that names what is missing.
Nothing is assumed, and no model is asked to calculate.

**Display.** The renderer shows the following in order:

1. context and status;
2. summary figures (the remainder only when it is not zero);
3. the allocation table, paginated after 50 rows;
4. the formula;
5. visible discrepancies and warnings;
6. legal limitations;
7. collapsible evidence and technical details.

Amounts carry the currency code and are padded to cents but never rounded.
Nominal amounts carry no currency. ClaimFlow still issues no invoices.
