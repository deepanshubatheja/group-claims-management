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
