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
