# ClaimFlow functional capability audit

Audit date: 2026-10-21. This audit distinguishes a persisted schema from an end-to-end workflow.

| Capability | Classification | Evidence |
|---|---|---|
| PDF/XLSX/CSV import and local persistence | Fully implemented and tested | `backend/group_claims_backend/services/document_import.py:414-464`; synthetic import, full suite |
| Immutable local file vault and SHA-256 duplicate detection | Fully implemented and tested | `backend/group_claims_backend/storage/vault.py:52-103`; synthetic import/hash test |
| SQLite schema and migrations | Fully implemented and tested as storage infrastructure | `backend/group_claims_backend/storage/migrations.py:30-210`; storage tests |
| Spreadsheet cell and PDF page/block provenance | Fully implemented and tested | `document_import.py`; `backend/tests/test_document_import.py`; synthetic cell references and billing citations |
| Cloud AI consent, bounded excerpts, evidence validation, and provisional proposals | Fully implemented; mock-tested; live provider not verified in this sprint | `backend/group_claims_backend/services/ai_analysis.py`; `backend/tests/test_ai_analysis.py` |
| Single-document and selected-document Q&A scope | Implemented and mock-tested; live provider not verified in this sprint | `backend/group_claims_backend/services/ai_analysis.py`; `backend/group_claims_backend/services/matter_review.py:778-1129` |
| All-file analysis scope | Partially implemented | Selection is limited to 20 documents in `document_import.py:425-434`; bounded evidence may exclude sources and reports coverage in `matter_review.py:1110-1129`. No live all-file validation performed. |
| Evidence-source validation | Fully implemented and tested | `ai_analysis.py`; tests reject fabricated references and invalid proposals. |
| Issue date and effective/as-of-date handling | Partially implemented | Dates are preserved in extracted source text/cells; no importer maps them to confirmed domain events. Synthetic run retained evidence but created no dated holdings. |
| Dated holdings/history reconstruction | Missing end-to-end workflow | Schema exists at `migrations.py:89-97`; synthetic run found 0 `holding_positions` where ground truth requires 6. |
| Securities transfer detection/persistence | Missing end-to-end workflow | Transfer language can be stored/retrieved provisionally, but no confirmed transfer event record is written. Synthetic run found no persisted event. |
| Claim-right assignment persistence | Missing end-to-end workflow | Schema exists at `migrations.py:100-119`; AI proposals remain provisional; synthetic run found 0 `claim_right_assignments` where ground truth requires 1. |
| Holdings reconciliation and contradictory-source issue creation | Missing end-to-end workflow | `issues` schema exists at `migrations.py:193-203`; synthetic run found 0 issues for its deliberate contradiction. |
| Explicit document-backed pro-rata calculation | Fully implemented and tested for one explicit instruction | `matter_review.py:257-400,427-473`; synthetic USD 120,000 allocation reconciled exactly. |
| Billing agreement/invoice creation and issuance | Missing end-to-end workflow | Tables exist at `migrations.py:122-179`; `matter_review.py:249-253` explicitly says the preview is non-persistent and not an invoice. |
| Audit history | Implemented and tested for key actions | `audit_events` schema at `migrations.py:206-210`; AI authorization/connection audit tests. |

## Critical interpretation

The current application is safe in one important respect: it does **not** silently transform imported documents or AI output into confirmed holdings, ownership, claims, invoices, or issues. The tradeoff is that the historical-reconciliation workflow requested for the presentation does not exist yet. The pro-rata calculator is valid only when a single selected document contains every approved input it requires; it is not a cross-document legal or accounting engine.
