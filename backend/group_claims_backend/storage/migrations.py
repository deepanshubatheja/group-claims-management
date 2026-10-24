"""Versioned, transactional SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .database import Database


class MigrationError(RuntimeError):
    """Raised when the existing database cannot be migrated safely."""


MigrationFunction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    apply: MigrationFunction


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _migration_001_initial_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE legal_entities (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            legal_name TEXT,
            entity_type TEXT NOT NULL,
            jurisdiction TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE investors (
            id TEXT PRIMARY KEY,
            legal_entity_id TEXT NOT NULL UNIQUE REFERENCES legal_entities(id) ON DELETE RESTRICT,
            external_reference TEXT UNIQUE,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE investment_vehicles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            legal_entity_id TEXT REFERENCES legal_entities(id) ON DELETE RESTRICT,
            has_separate_legal_personality INTEGER NOT NULL CHECK (has_separate_legal_personality IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            investor_id TEXT NOT NULL REFERENCES investors(id) ON DELETE RESTRICT,
            holder_entity_id TEXT REFERENCES legal_entities(id) ON DELETE RESTRICT,
            investment_vehicle_id TEXT REFERENCES investment_vehicles(id) ON DELETE RESTRICT,
            external_reference TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (investor_id, external_reference)
        )
        """,
        """
        CREATE TABLE securities (
            id TEXT PRIMARY KEY,
            isin TEXT NOT NULL UNIQUE CHECK (length(isin) = 12),
            name TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE source_documents (
            id TEXT PRIMARY KEY,
            original_filename TEXT NOT NULL,
            managed_relative_path TEXT NOT NULL UNIQUE,
            content_sha256 TEXT NOT NULL UNIQUE CHECK (length(content_sha256) = 64),
            file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes >= 0),
            imported_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE holding_positions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
            position_date TEXT NOT NULL,
            quantity TEXT NOT NULL,
            source_document_id TEXT REFERENCES source_documents(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE claim_rights (
            id TEXT PRIMARY KEY,
            claim_reference TEXT,
            legal_claimant_entity_id TEXT REFERENCES legal_entities(id) ON DELETE RESTRICT,
            related_security_id TEXT REFERENCES securities(id) ON DELETE RESTRICT,
            status TEXT NOT NULL,
            asserted_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE claim_right_assignments (
            id TEXT PRIMARY KEY,
            claim_right_id TEXT NOT NULL REFERENCES claim_rights(id) ON DELETE RESTRICT,
            from_entity_id TEXT REFERENCES legal_entities(id) ON DELETE RESTRICT,
            to_entity_id TEXT REFERENCES legal_entities(id) ON DELETE RESTRICT,
            effective_date TEXT,
            source_document_id TEXT REFERENCES source_documents(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE invoice_recipients (
            id TEXT PRIMARY KEY,
            legal_entity_id TEXT NOT NULL UNIQUE REFERENCES legal_entities(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE billing_agreements (
            id TEXT PRIMARY KEY,
            invoice_recipient_id TEXT REFERENCES invoice_recipients(id) ON DELETE RESTRICT,
            agreement_reference TEXT,
            effective_from TEXT,
            effective_to TEXT,
            terms_reference TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE invoices (
            id TEXT PRIMARY KEY,
            invoice_recipient_id TEXT REFERENCES invoice_recipients(id) ON DELETE RESTRICT,
            billing_agreement_id TEXT REFERENCES billing_agreements(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (status IN ('draft', 'issued', 'partially_paid', 'paid', 'void')),
            issued_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE invoice_line_items (
            id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
            description TEXT NOT NULL,
            amount_decimal TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE payments (
            id TEXT PRIMARY KEY,
            invoice_id TEXT REFERENCES invoices(id) ON DELETE RESTRICT,
            payer_entity_id TEXT REFERENCES legal_entities(id) ON DELETE RESTRICT,
            amount_decimal TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            received_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE financial_adjustments (
            id TEXT PRIMARY KEY,
            invoice_id TEXT REFERENCES invoices(id) ON DELETE RESTRICT,
            adjustment_type TEXT NOT NULL,
            amount_decimal TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE evidence_references (
            id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE RESTRICT,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            source_location TEXT,
            description TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE issues (
            id TEXT PRIMARY KEY,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            affected_type TEXT,
            affected_id TEXT,
            reason TEXT NOT NULL,
            source_document_id TEXT REFERENCES source_documents(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """,
        """
        CREATE TABLE audit_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            details_json TEXT
        )
        """,
        "CREATE INDEX idx_holding_positions_account_date ON holding_positions(account_id, position_date)",
        "CREATE INDEX idx_holding_positions_security_date ON holding_positions(security_id, position_date)",
        "CREATE INDEX idx_claim_rights_claimant ON claim_rights(legal_claimant_entity_id)",
        "CREATE INDEX idx_claim_assignments_claim ON claim_right_assignments(claim_right_id)",
        "CREATE INDEX idx_invoice_line_items_invoice ON invoice_line_items(invoice_id)",
        "CREATE INDEX idx_payments_invoice ON payments(invoice_id)",
        "CREATE INDEX idx_evidence_references_document ON evidence_references(source_document_id)",
        "CREATE INDEX idx_issues_status_severity ON issues(status, severity)",
        "CREATE INDEX idx_audit_events_target ON audit_events(target_type, target_id)",
        """
        CREATE TRIGGER prevent_issued_invoice_line_mutation
        BEFORE UPDATE ON invoice_line_items
        WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) = 'issued'
        BEGIN
            SELECT RAISE(ABORT, 'Issued invoice line items are immutable.');
        END
        """,
        """
        CREATE TRIGGER prevent_issued_invoice_line_deletion
        BEFORE DELETE ON invoice_line_items
        WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) = 'issued'
        BEGIN
            SELECT RAISE(ABORT, 'Issued invoice line items are immutable.');
        END
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migration_002_add_investment_vehicle_type(connection: sqlite3.Connection) -> None:
    """Keep legacy rows explicitly unresolved rather than assuming vehicle or subfund."""

    connection.execute(
        """
        ALTER TABLE investment_vehicles
        ADD COLUMN vehicle_type TEXT NOT NULL DEFAULT 'unspecified'
        CHECK (vehicle_type IN ('vehicle', 'subfund', 'unspecified'))
        """
    )


def _migration_003_add_document_extractions(connection: sqlite3.Connection) -> None:
    """Store versioned deterministic extraction output with source locations."""

    connection.executescript(
        """
        ALTER TABLE source_documents ADD COLUMN file_format TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE source_documents ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'imported'
            CHECK (processing_status IN ('imported', 'parsed', 'unsupported', 'failed'));
        ALTER TABLE source_documents ADD COLUMN parser_version TEXT;
        ALTER TABLE source_documents ADD COLUMN parsed_at TEXT;
        ALTER TABLE source_documents ADD COLUMN processing_error TEXT;

        CREATE TABLE document_extractions (
            id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE RESTRICT,
            parser_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('processing', 'parsed', 'unsupported', 'failed')),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_code TEXT,
            error_message TEXT
        );
        CREATE TABLE extracted_content (
            id TEXT PRIMARY KEY,
            extraction_id TEXT NOT NULL REFERENCES document_extractions(id) ON DELETE RESTRICT,
            content_kind TEXT NOT NULL CHECK (content_kind IN ('spreadsheet_cell', 'csv_cell', 'pdf_page')),
            sheet_name TEXT,
            row_number INTEGER,
            column_number INTEGER,
            page_number INTEGER,
            value_text TEXT,
            value_type TEXT,
            formula_text TEXT,
            is_empty INTEGER NOT NULL CHECK (is_empty IN (0, 1)),
            source_reference TEXT NOT NULL
        );
        CREATE INDEX idx_document_extractions_source ON document_extractions(source_document_id, started_at DESC);
        CREATE INDEX idx_extracted_content_extraction ON extracted_content(extraction_id, id);
        """
    )


def _migration_004_add_pdf_page_extractions(connection: sqlite3.Connection) -> None:
    """Keep coordinate-aware PDF output alongside the original generic extraction rows.

    Existing extraction attempts remain immutable.  This table is intentionally
    additive so a reprocess creates a new, independently reviewable attempt.
    """

    connection.executescript(
        """
        CREATE TABLE pdf_page_extractions (
            id TEXT PRIMARY KEY,
            extraction_id TEXT NOT NULL REFERENCES document_extractions(id) ON DELETE RESTRICT,
            page_number INTEGER NOT NULL CHECK (page_number > 0),
            extraction_method TEXT NOT NULL CHECK (extraction_method IN ('native_text', 'ocr', 'unavailable')),
            raw_text TEXT,
            cleaned_text TEXT,
            quality_warning TEXT,
            ocr_language TEXT,
            ocr_engine_version TEXT,
            mean_confidence REAL,
            block_count INTEGER NOT NULL DEFAULT 0 CHECK (block_count >= 0),
            UNIQUE (extraction_id, page_number)
        );
        CREATE TABLE pdf_text_blocks (
            id TEXT PRIMARY KEY,
            pdf_page_extraction_id TEXT NOT NULL REFERENCES pdf_page_extractions(id) ON DELETE RESTRICT,
            block_index INTEGER NOT NULL CHECK (block_index >= 0),
            x0 REAL,
            y0 REAL,
            x1 REAL,
            y1 REAL,
            text TEXT NOT NULL,
            confidence REAL,
            is_table_candidate INTEGER NOT NULL DEFAULT 0 CHECK (is_table_candidate IN (0, 1)),
            UNIQUE (pdf_page_extraction_id, block_index)
        );
        CREATE INDEX idx_pdf_page_extractions_attempt ON pdf_page_extractions(extraction_id, page_number);
        CREATE INDEX idx_pdf_text_blocks_page ON pdf_text_blocks(pdf_page_extraction_id, block_index);
        """
    )


def _migration_005_add_extraction_order_indexes(connection: sqlite3.Connection) -> None:
    """Add stable grid ordering for new spreadsheet extraction attempts.

    Earlier attempts did not retain workbook sheet positions; their sheet order
    remains a deterministic name fallback until the user chooses reprocessing.
    """

    connection.executescript(
        """
        ALTER TABLE extracted_content ADD COLUMN worksheet_index INTEGER;
        CREATE INDEX idx_extracted_content_grid
            ON extracted_content (extraction_id, content_kind, worksheet_index, sheet_name, row_number, column_number);
        """
    )

def _migration_006_add_ai_foundation(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE ai_authorizations (id TEXT PRIMARY KEY, authorized INTEGER NOT NULL CHECK (authorized IN (0,1)), provider TEXT, model TEXT, authorization_text_version TEXT NOT NULL, occurred_at TEXT NOT NULL);
        CREATE TABLE ai_processing_jobs (id TEXT PRIMARY KEY, source_document_id TEXT REFERENCES source_documents(id) ON DELETE RESTRICT, extraction_id TEXT REFERENCES document_extractions(id) ON DELETE RESTRICT, provider TEXT NOT NULL, model TEXT, prompt_version TEXT NOT NULL, schema_version TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','blocked')), error_code TEXT, created_at TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE ai_proposals (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ai_processing_jobs(id) ON DELETE RESTRICT, proposal_type TEXT NOT NULL, field_name TEXT NOT NULL, value_json TEXT, is_unknown INTEGER NOT NULL CHECK (is_unknown IN (0,1)), confidence REAL, validation_status TEXT NOT NULL CHECK (validation_status IN ('unvalidated','invalid','validated')), source_reference TEXT NOT NULL, evidence_snippet TEXT, model_version TEXT, created_at TEXT NOT NULL);
        CREATE INDEX idx_ai_jobs_source ON ai_processing_jobs(source_document_id, created_at DESC);
        CREATE INDEX idx_ai_proposals_job ON ai_proposals(job_id);
    """)


def _migration_007_add_ai_job_coverage(connection: sqlite3.Connection) -> None:
    """Keep document-level authorization and bounded-analysis coverage with each job."""

    connection.executescript(
        """
        ALTER TABLE ai_processing_jobs ADD COLUMN authorization_event_id TEXT;
        ALTER TABLE ai_processing_jobs ADD COLUMN coverage_json TEXT;
        CREATE UNIQUE INDEX idx_ai_jobs_active_source_extraction
            ON ai_processing_jobs(source_document_id, extraction_id)
            WHERE status IN ('queued', 'running');
        """
    )


def _migration_008_add_ai_usage_cost_metadata(connection: sqlite3.Connection) -> None:
    """Persist non-sensitive token/cost estimates without retaining AI payloads."""

    connection.executescript(
        """
        ALTER TABLE ai_processing_jobs ADD COLUMN estimated_cost_usd TEXT;
        ALTER TABLE ai_processing_jobs ADD COLUMN input_tokens INTEGER;
        ALTER TABLE ai_processing_jobs ADD COLUMN output_tokens INTEGER;
        ALTER TABLE ai_processing_jobs ADD COLUMN total_tokens INTEGER;
        ALTER TABLE ai_processing_jobs ADD COLUMN actual_cost_estimate_usd TEXT;
        """
    )


def _migration_009_add_ai_analysis_summaries(connection: sqlite3.Connection) -> None:
    """Persist a concise, cited summary separately from proposal records."""

    connection.executescript(
        """
        ALTER TABLE ai_processing_jobs ADD COLUMN summary_text TEXT;
        ALTER TABLE ai_processing_jobs ADD COLUMN summary_unknown INTEGER NOT NULL DEFAULT 1
            CHECK (summary_unknown IN (0,1));
        ALTER TABLE ai_processing_jobs ADD COLUMN summary_citations_json TEXT;
        """
    )

def _migration_011_add_reconciliation_candidates(connection: sqlite3.Connection) -> None:
    """Create the reconciliation schema and safely resume an interrupted local upgrade.

    ``sqlite3.executescript`` commits before executing its statements, which
    means a process interruption after table creation but before the migration
    version is recorded leaves a valid, partial schema.  Never ask a user to
    delete their database in that situation: create only missing pieces and
    verify the completed shape before recording migration 11.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_candidates (
            id TEXT PRIMARY KEY,
            candidate_type TEXT NOT NULL CHECK (candidate_type IN ('holding_snapshot','securities_transfer','claim_assignment')),
            lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('candidate','confirmed','rejected')),
            source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE RESTRICT,
            document_issue_date TEXT NOT NULL,
            origin TEXT NOT NULL CHECK (origin IN ('deterministic','ai_validated')),
            source_reference TEXT NOT NULL,
            evidence_snippet TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            confirmed_at TEXT,
            UNIQUE(source_document_id, candidate_type, source_reference, payload_sha256)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS securities_transfers (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL UNIQUE REFERENCES reconciliation_candidates(id) ON DELETE RESTRICT,
            from_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            to_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
            effective_date TEXT NOT NULL,
            quantity TEXT NOT NULL,
            source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """
    )

    def add_column_if_missing(table: str, definition: str, column: str) -> None:
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns:
            raise MigrationError(f"Required local table {table} is unavailable.")
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    add_column_if_missing(
        "holding_positions",
        "candidate_id TEXT REFERENCES reconciliation_candidates(id) ON DELETE RESTRICT",
        "candidate_id",
    )
    add_column_if_missing(
        "claim_right_assignments",
        "candidate_id TEXT REFERENCES reconciliation_candidates(id) ON DELETE RESTRICT",
        "candidate_id",
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_holding_candidate "
        "ON holding_positions(candidate_id) WHERE candidate_id IS NOT NULL"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_candidate "
        "ON claim_right_assignments(candidate_id) WHERE candidate_id IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_status "
        "ON reconciliation_candidates(lifecycle_status, candidate_type)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_date "
        "ON securities_transfers(security_id, effective_date)"
    )


def _migration_010_add_document_search_index(connection: sqlite3.Connection) -> None:
    """Add a derived, local full-text index over deterministic extraction output.

    The index is rebuildable and never authoritative: every retrieved hit is
    re-resolved against ``extracted_content`` or ``pdf_text_blocks`` before it
    can be cited.  Extractions are indexed lazily when a scoped question first
    needs them, so this migration does not rewrite or reparse existing data.
    The trigram tokenizer preserves the case-insensitive substring semantics
    of the earlier in-memory scan, including German compound words.
    """

    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS document_search_fts USING fts5(
            text,
            source_document_id UNINDEXED,
            extraction_id UNINDEXED,
            source_kind UNINDEXED,
            source_reference UNINDEXED,
            sheet_name UNINDEXED,
            worksheet_index UNINDEXED,
            row_number UNINDEXED,
            column_number UNINDEXED,
            page_number UNINDEXED,
            block_index UNINDEXED,
            tokenize = 'trigram case_sensitive 0'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_search_index (
            extraction_id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL,
            item_count INTEGER NOT NULL CHECK (item_count >= 0),
            unavailable_page_count INTEGER NOT NULL DEFAULT 0 CHECK (unavailable_page_count >= 0),
            indexed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_search_index_document "
        "ON document_search_index(source_document_id)"
    )


def _migration_012_repair_document_search_index(connection: sqlite3.Connection) -> None:
    """Repair an interrupted v10 derived-search migration without touching evidence.

    ``document_search_fts`` and ``document_search_index`` contain only a
    rebuildable local lookup cache.  Some early databases recorded migration
    10 even though those tables were absent, so Matter Review later failed
    while preparing a question.  Re-running this idempotent schema creation
    restores the cache; it is lazily repopulated from immutable extractions.
    """

    _migration_010_add_document_search_index(connection)


def _migration_013_add_candidate_provenance(connection: sqlite3.Connection) -> None:
    """Pin reconciliation candidates to the extraction that supported them.

    Existing candidates predate the extraction-version requirement and remain
    readable.  New candidates always populate these additive fields.  The
    migration deliberately does not rewrite historical user data.
    """

    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(reconciliation_candidates)")}
    additions = (
        ("extraction_id", "extraction_id TEXT REFERENCES document_extractions(id) ON DELETE RESTRICT"),
        ("parser_version", "parser_version TEXT"),
        ("confidence", "confidence REAL"),
        (
            "validation_status",
            "validation_status TEXT NOT NULL DEFAULT 'supported' "
            "CHECK (validation_status IN ('supported', 'validated_ai', 'review_required'))",
        ),
    )
    for column, definition in additions:
        if column not in columns:
            connection.execute(f"ALTER TABLE reconciliation_candidates ADD COLUMN {definition}")

    assignment_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(claim_right_assignments)")
    }
    if "assignment_percentage" not in assignment_columns:
        connection.execute(
            "ALTER TABLE claim_right_assignments ADD COLUMN assignment_percentage TEXT"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_extraction_status "
        "ON reconciliation_candidates(extraction_id, lifecycle_status)"
    )


def _migration_014_add_evidence_backed_billing(connection: sqlite3.Connection) -> None:
    """Add confirmation-gated billing rules and immutable calculation runs.

    A billing instruction is deliberately staged separately from a confirmed
    billing agreement.  Calculation runs are not invoices: they preserve the
    rule, inputs, evidence, and open issues that were known at calculation
    time so a later reconciliation update never rewrites a prior result.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_rule_candidates (
            id TEXT PRIMARY KEY,
            lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('candidate','approved','rejected')),
            source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE RESTRICT,
            extraction_id TEXT NOT NULL REFERENCES document_extractions(id) ON DELETE RESTRICT,
            parser_version TEXT NOT NULL,
            source_reference TEXT NOT NULL,
            evidence_snippet TEXT NOT NULL,
            document_issue_date TEXT NOT NULL,
            origin TEXT NOT NULL CHECK (origin IN ('deterministic','ai_validated')),
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            UNIQUE(source_document_id, source_reference, payload_sha256)
        )
        """
    )
    agreement_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(billing_agreements)")
    }
    additions = (
        ("billing_rule_candidate_id", "billing_rule_candidate_id TEXT REFERENCES billing_rule_candidates(id) ON DELETE RESTRICT"),
        ("source_document_id", "source_document_id TEXT REFERENCES source_documents(id) ON DELETE RESTRICT"),
        ("source_reference", "source_reference TEXT"),
        ("rule_json", "rule_json TEXT"),
        ("approval_status", "approval_status TEXT NOT NULL DEFAULT 'legacy' CHECK (approval_status IN ('legacy','approved','rejected'))"),
        ("approved_at", "approved_at TEXT"),
    )
    for column, definition in additions:
        if column not in agreement_columns:
            connection.execute(f"ALTER TABLE billing_agreements ADD COLUMN {definition}")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_agreement_candidate "
        "ON billing_agreements(billing_rule_candidate_id) WHERE billing_rule_candidate_id IS NOT NULL"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_calculation_runs (
            id TEXT PRIMARY KEY,
            billing_agreement_id TEXT NOT NULL REFERENCES billing_agreements(id) ON DELETE RESTRICT,
            previous_run_id TEXT REFERENCES billing_calculation_runs(id) ON DELETE RESTRICT,
            run_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('approved_calculation','provisional','blocked')),
            allocation_date TEXT,
            currency_code TEXT,
            fee_pool_decimal TEXT,
            total_eligible_decimal TEXT,
            rounding_method TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            related_issues_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(billing_agreement_id, run_version),
            UNIQUE(billing_agreement_id, input_fingerprint)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_calculation_allocations (
            id TEXT PRIMARY KEY,
            calculation_run_id TEXT NOT NULL REFERENCES billing_calculation_runs(id) ON DELETE RESTRICT,
            participant_key TEXT NOT NULL,
            participant_label TEXT NOT NULL,
            subfund_reference TEXT,
            eligible_basis_decimal TEXT,
            share_decimal TEXT,
            allocated_fee_decimal TEXT,
            currency_code TEXT,
            eligibility_status TEXT NOT NULL CHECK (eligibility_status IN ('eligible','excluded','unresolved')),
            eligibility_reason TEXT,
            holdings_record_ids_json TEXT NOT NULL,
            evidence_references_json TEXT NOT NULL,
            invoice_recipient_status TEXT NOT NULL CHECK (invoice_recipient_status IN ('unresolved','specified')),
            created_at TEXT NOT NULL,
            UNIQUE(calculation_run_id, participant_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_rule_candidate_status "
        "ON billing_rule_candidates(lifecycle_status, source_document_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_calculation_agreement "
        "ON billing_calculation_runs(billing_agreement_id, run_version)"
    )


def _migration_015_add_provisional_holding_conflicts(connection: sqlite3.Connection) -> None:
    """Persist contradictory holding observations before either is confirmed.

    A conflict is keyed by the business identity shared by the observations,
    while the link table pins every competing amount to the exact candidate,
    extraction, document, and source locator that established it.  This keeps
    an issue auditable even if a candidate is later rejected or a separate
    resolution workflow is introduced.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_holding_conflicts (
            id TEXT PRIMARY KEY,
            conflict_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('open','resolved')),
            account_reference TEXT NOT NULL,
            security_isin TEXT NOT NULL,
            position_date TEXT NOT NULL,
            investor_name TEXT,
            subfund_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_holding_conflict_candidates (
            conflict_id TEXT NOT NULL REFERENCES reconciliation_holding_conflicts(id) ON DELETE RESTRICT,
            candidate_id TEXT NOT NULL UNIQUE REFERENCES reconciliation_candidates(id) ON DELETE RESTRICT,
            quantity TEXT NOT NULL,
            source_document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE RESTRICT,
            extraction_id TEXT REFERENCES document_extractions(id) ON DELETE RESTRICT,
            parser_version TEXT,
            source_reference TEXT NOT NULL,
            document_issue_date TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            PRIMARY KEY (conflict_id, candidate_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_holding_conflict_open_identity "
        "ON reconciliation_holding_conflicts(status, account_reference, security_isin, position_date)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_holding_conflict_candidate "
        "ON reconciliation_holding_conflict_candidates(candidate_id)"
    )


def _migration_016_link_invoice_lines_to_calculation_allocations(connection: sqlite3.Connection) -> None:
    """Permit an explicitly verified invoice line to reference one allocation.

    Imports and AI findings never populate this link.  It exists only so a
    separately verified invoice record can be compared to a persisted,
    evidence-backed calculation without guessing from a description or a
    subfund name.
    """
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(invoice_line_items)")}
    if "billing_calculation_allocation_id" not in columns:
        connection.execute(
            "ALTER TABLE invoice_line_items ADD COLUMN billing_calculation_allocation_id "
            "TEXT REFERENCES billing_calculation_allocations(id) ON DELETE RESTRICT"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoice_line_allocation "
        "ON invoice_line_items(billing_calculation_allocation_id)"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, _migration_001_initial_schema),
    Migration(2, _migration_002_add_investment_vehicle_type),
    Migration(3, _migration_003_add_document_extractions),
    Migration(4, _migration_004_add_pdf_page_extractions),
    Migration(5, _migration_005_add_extraction_order_indexes),
    Migration(6, _migration_006_add_ai_foundation),
    Migration(7, _migration_007_add_ai_job_coverage),
    Migration(8, _migration_008_add_ai_usage_cost_metadata),
    Migration(9, _migration_009_add_ai_analysis_summaries),
    Migration(10, _migration_010_add_document_search_index),
    Migration(11, _migration_011_add_reconciliation_candidates),
    Migration(12, _migration_012_repair_document_search_index),
    Migration(13, _migration_013_add_candidate_provenance),
    Migration(14, _migration_014_add_evidence_backed_billing),
    Migration(15, _migration_015_add_provisional_holding_conflicts),
    Migration(16, _migration_016_link_invoice_lines_to_calculation_allocations),
)


class MigrationRunner:
    """Applies each ordered migration once and records its version atomically."""

    def __init__(self, database: Database, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        self._database = database
        self._migrations = tuple(migrations)

    def initialize(self) -> int:
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise MigrationError("The local database integrity check failed.")
                current_version = self._current_version(connection)

            known_versions = {migration.version for migration in self._migrations}
            if current_version > max(known_versions, default=0):
                raise MigrationError("The local database schema is newer than this application.")

            for migration in self._migrations:
                if migration.version <= current_version:
                    continue
                with self._database.transaction() as connection:
                    migration.apply(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (migration.version, _utc_timestamp()),
                    )
                current_version = migration.version

            return current_version
        except (sqlite3.Error, MigrationError) as error:
            if isinstance(error, MigrationError):
                raise
            raise MigrationError("The local database could not be migrated safely.") from error

    @staticmethod
    def _current_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0])
