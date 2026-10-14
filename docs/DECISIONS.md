# Architecture decisions

## 2026-10-14 — Local persistence boundary

Phase 4 uses Electron's `app.getPath('userData')` as the authoritative per-user
storage root and passes it only as a fixed Python child-process startup argument.
The backend validates the root, initializes private managed directories, and
uses SQLite's standard library driver with transactional numbered migrations.
Document copies remain local, immutable managed files named by generated IDs;
SQLite stores their relative paths and SHA-256 hashes. No public local server,
cloud storage, or automatic encryption is introduced.

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