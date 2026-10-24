"""End-to-end tests for the local evidence-to-record reconciliation pipeline."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import pymupdf

from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.reconciliation import ReconciliationError, ReconciliationService
from group_claims_backend.storage.service import StorageService


ROOT = Path(__file__).resolve().parents[2]
GENERATOR_ROOT = ROOT / "scripts" / "validation-sprint"
NAMES = (
    "01_opening_holdings_2025-01-31.xlsx",
    "02_partial_transfer_effective_2025-02-15.pdf",
    "03_claim_right_assignment_effective_2025-02-18.pdf",
    "04_later_holdings_2025-02-28.xlsx",
    "05_contradictory_custodian_statement_2025-02-28.pdf",
    "06_approved_billing_instruction_2025-03-01.pdf",
)


class ReconciliationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-reconciliation-pipeline-")
        self.root = Path(self.temporary.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for script in ("build_synthetic_pdfs.py", "build_synthetic_workbooks.py"):
            subprocess.run([sys.executable, str(GENERATOR_ROOT / script), str(self.sources)], check=True)
        self.storage = StorageService(str(self.root / "app-data"))
        self.assertEqual(self.storage.initialize().status, "ready")
        self.imports = DocumentImportService(self.storage)
        self.service = ReconciliationService(self.storage)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self, names: tuple[str, ...] = NAMES) -> None:
        result = self.imports.import_authorized_paths([str(self.sources / name) for name in names])
        self.assertTrue(all(item["ok"] for item in result["documents"]))

    def _candidate_rows(self):
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            return connection.execute(
                "SELECT c.*, d.original_filename, d.imported_at FROM reconciliation_candidates c "
                "JOIN source_documents d ON d.id = c.source_document_id ORDER BY c.created_at, c.id"
            ).fetchall()

    def _review_batch(self) -> dict[str, object]:
        rows = self._candidate_rows()
        return dict(self.service.apply_policy({
            "candidateIds": [str(row["id"]) for row in rows],
            "policy": "reviewed_batch",
            "acknowledged": True,
        }))

    def test_import_extract_candidates_reviewed_batch_and_restart(self) -> None:
        self._import()
        rows = self._candidate_rows()
        self.assertEqual(len(rows), 9)
        self.assertEqual(sum(row["candidate_type"] == "holding_snapshot" for row in rows), 7)
        self.assertEqual(sum(row["candidate_type"] == "securities_transfer" for row in rows), 1)
        self.assertEqual(sum(row["candidate_type"] == "claim_assignment" for row in rows), 1)
        self.assertTrue(all(row["extraction_id"] and row["parser_version"] and row["imported_at"] and row["created_at"] for row in rows))
        self.assertTrue(all("block" in row["source_reference"] or "!" in row["source_reference"] for row in rows))

        batch = self._review_batch()
        self.assertEqual(len(batch["confirmed"]), 6)  # five uncontested snapshots and one transfer
        self.assertEqual(
            {item["reason"] for item in batch["reviewRequired"]},
            {"conflicting_dated_holding", "legal_assignment_requires_individual_review"},
        )
        assignment = next(row for row in self._candidate_rows() if row["candidate_type"] == "claim_assignment")
        self.assertEqual(self.service.confirm({"candidateId": assignment["id"], "acknowledged": True})["type"], "claim_assignment")

        before_transfer = self.service.holdings_as_of({"asOfDate": "2025-02-14"})["positions"]
        after_transfer = self.service.holdings_as_of({"asOfDate": "2025-02-16"})["positions"]
        self.assertEqual({item["account"]: item["quantity"] for item in before_transfer}, {
            "CUST-ALPHA-01": "600000", "CUST-ALPHA-02": "800000", "CUST-ALPHA-03": "500000",
        })
        self.assertEqual({item["account"]: item["quantity"] for item in after_transfer}, {
            "CUST-ALPHA-01": "400000", "CUST-ALPHA-02": "1000000", "CUST-ALPHA-03": "500000",
        })

        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_right_assignments").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence_references").fetchone()[0], 7)
            conflicts = connection.execute(
                "SELECT payload_json, validation_status FROM reconciliation_candidates "
                "WHERE candidate_type = 'holding_snapshot' AND payload_json LIKE '%2025-02-28%' AND payload_json LIKE '%CUST-ALPHA-02%'"
            ).fetchall()
            self.assertEqual({row["validation_status"] for row in conflicts}, {"review_required"})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflicts WHERE status = 'open'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflict_candidates").fetchone()[0], 2)

        restarted = StorageService(str(self.root / "app-data"))
        self.assertEqual(restarted.initialize().status, "ready")
        with restarted.database.connection() as connection:  # type: ignore[union-attr]
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_right_assignments").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
            self.assertGreaterEqual(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 11)

    def test_candidate_queue_persists_competing_provisional_holdings_without_confirming_them(self) -> None:
        self._import()
        queue = self.service.candidates({"status": "candidate"})["candidates"]
        conflicts = [item for item in queue if item.get("potentialConflict") is not None]
        self.assertEqual(len(conflicts), 2)
        for candidate in conflicts:
            conflict = candidate["potentialConflict"]
            self.assertEqual(conflict["type"], "competing_dated_holding")
            self.assertEqual(conflict["account"], "CUST-ALPHA-02")
            self.assertEqual(conflict["isin"], "XS0123456789")
            self.assertEqual(conflict["asOfDate"], "2025-02-28")
            self.assertEqual(conflict["difference"], "100000")
            self.assertEqual({item["quantity"] for item in conflict["observations"]}, {"900000", "1000000"})
            self.assertEqual(candidate["status"], "candidate")
            self.assertEqual(candidate["validationStatus"], "review_required")
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflicts WHERE status = 'open'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflict_candidates").fetchone()[0], 2)
        issues = self.service.open_discrepancies({})["issues"]
        self.assertEqual(len(issues), 1)
        self.assertEqual({item["quantity"] for item in issues[0]["observations"]}, {"900000", "1000000"})
        self.assertEqual(issues[0]["differenceAmounts"], ["100000"])
        self.assertEqual(len(issues[0]["sourceDocumentIds"]), 2)

    def test_conflict_is_idempotent_retained_after_rejection_and_can_be_explicitly_backfilled(self) -> None:
        self._import()
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            conflict_document = connection.execute(
                "SELECT source_document_id FROM reconciliation_candidates WHERE payload_json LIKE '%900000%'"
            ).fetchone()
        self.assertIsNotNone(conflict_document)
        # Reanalysis stages no duplicate candidate or issue.
        self.service.generate({"documentId": conflict_document["source_document_id"], "includeAi": False})
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflicts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
            candidate = connection.execute("SELECT id FROM reconciliation_candidates WHERE payload_json LIKE '%900000%'").fetchone()
        self.service.reject({"candidateId": candidate["id"], "reason": "Conflicting custodian observation retained for review.", "acknowledged": True})
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflict_candidates").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'reconciliation_candidate_rejected'").fetchone()[0], 1)

        # Model a database created before v15, then prove the explicit repair
        # creates one issue without confirming a candidate or business record.
        with self.storage.database.transaction() as connection:
            connection.execute("DELETE FROM issues")
            connection.execute("DELETE FROM reconciliation_holding_conflict_candidates")
            connection.execute("DELETE FROM reconciliation_holding_conflicts")
            connection.execute("UPDATE reconciliation_candidates SET validation_status = 'supported' WHERE candidate_type = 'holding_snapshot'")
        repaired = self.service.backfill_provisional_holding_conflicts({"acknowledged": True})
        self.assertEqual(repaired["openConflicts"], 1)
        self.assertEqual(repaired["issuesCreated"], 1)
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
        repeated = self.service.backfill_provisional_holding_conflicts({"acknowledged": True})
        self.assertEqual(repeated["issuesCreated"], 0)

    def test_conflict_with_a_confirmed_holding_creates_a_persistent_issue(self) -> None:
        self._import(NAMES[:4])
        self._review_batch()
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 6)
        result = self.imports.import_authorized_paths([str(self.sources / NAMES[4])])
        self.assertTrue(result["documents"][0]["ok"])
        with self.storage.database.connection() as connection:
            candidate = connection.execute("SELECT id, lifecycle_status, validation_status FROM reconciliation_candidates WHERE payload_json LIKE '%900000%'").fetchone()
            self.assertEqual(candidate["lifecycle_status"], "candidate")
            self.assertEqual(candidate["validation_status"], "review_required")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)
        with self.assertRaisesRegex(ReconciliationError, "conflicting dated holding"):
            self.service.confirm({"candidateId": candidate["id"], "acknowledged": True})
        issue = self.service.open_discrepancies({})["issues"][0]
        self.assertEqual({item["quantity"] for item in issue["observations"]}, {"900000", "1000000"})

    def test_reversed_arrival_order_is_reconciled_without_double_counting(self) -> None:
        self._import(tuple(reversed(NAMES)))
        batch = self._review_batch()
        self.assertEqual(len(batch["confirmed"]), 6)
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open'").fetchone()[0], 1)

    def test_historical_view_keeps_documented_reconstructed_and_unresolved_values_distinct(self) -> None:
        self._import()
        self._review_batch()
        assignment = next(row for row in self._candidate_rows() if row["candidate_type"] == "claim_assignment")
        self.service.confirm({"candidateId": assignment["id"], "acknowledged": True})

        opening = self.service.holdings_as_of({"asOfDate": "2025-01-31"})
        self.assertEqual({row["subfund"]: row["quantity"] for row in opening["positions"]}, {
            "Horizon Credit Fund A (SF-A)": "600000",
            "Horizon Credit Fund B (SF-B)": "800000",
            "Meridian Income Fund C (SF-C)": "500000",
        })
        self.assertTrue(all(row["reconstructionState"] == "documented" for row in opening["positions"]))
        self.assertTrue(all(row["snapshot"]["source"]["extractionId"] for row in opening["positions"]))
        self.assertTrue(all(row["snapshot"]["source"]["parserVersion"] for row in opening["positions"]))

        before_transfer = self.service.holdings_as_of({"asOfDate": "2025-02-14"})
        self.assertTrue(all(row["reconstructionState"] == "inferred" for row in before_transfer["positions"]))
        on_transfer = self.service.holdings_as_of({"asOfDate": "2025-02-15"})
        self.assertEqual({row["account"]: row["quantity"] for row in on_transfer["positions"]}, {
            "CUST-ALPHA-01": "400000", "CUST-ALPHA-02": "1000000", "CUST-ALPHA-03": "500000",
        })
        moved = [row for row in on_transfer["positions"] if row["reconstructionState"] == "reconstructed"]
        self.assertEqual(len(moved), 2)
        self.assertTrue(all(row["appliedTransfers"] for row in moved))
        self.assertTrue(all("Undocumented intervening events" in " ".join(row["assumptions"]) for row in moved))

        closing = self.service.holdings_as_of({"asOfDate": "2025-02-28"})
        self.assertEqual({row["account"]: row["quantity"] for row in closing["positions"]}, {
            "CUST-ALPHA-01": "400000", "CUST-ALPHA-02": "1000000", "CUST-ALPHA-03": "500000",
        })
        self.assertEqual(next(row["reconstructionState"] for row in closing["positions"] if row["account"] == "CUST-ALPHA-02"), "reconstructed")
        self.assertTrue(all(row["reconstructionState"] == "documented" for row in closing["positions"] if row["account"] != "CUST-ALPHA-02"))
        self.assertEqual(self.service.holdings_as_of({"asOfDate": "2025-01-01"})["positions"], [])
        self.assertIn("before the earliest", self.service.holdings_as_of({"asOfDate": "2025-01-01"})["limitations"][0])

        timeline = self.service.holdings_timeline({"investor": "Redwood Investors LP"})
        self.assertEqual([event["kind"] for event in timeline["events"]].count("documented_holding_snapshot"), 5)
        self.assertEqual([event["kind"] for event in timeline["events"]].count("securities_transfer"), 1)
        transfer = self.service.transfer_history({"isin": "XS0123456789"})["events"]
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer[0]["effectiveDate"], "2025-02-15")
        self.assertEqual(transfer[0]["from"]["account"], "CUST-ALPHA-01")
        self.assertEqual(transfer[0]["to"]["account"], "CUST-ALPHA-02")

        assignments = self.service.claim_assignment_history({"claimReference": "CR-2025-ALPHA"})["events"]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["effectiveDate"], "2025-02-18")
        self.assertEqual(assignments[0]["percentage"], "100")
        self.assertIn("does not evidence a securities movement", assignments[0]["limitation"])

        discrepancies = self.service.open_discrepancies({"isin": "XS0123456789"})["issues"]
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["status"], "unresolved")
        self.assertEqual({item["quantity"] for item in discrepancies[0]["observations"]}, {"900000", "1000000"})
        self.assertEqual(discrepancies[0]["differenceAmounts"], ["100000"])
        self.assertTrue(discrepancies[0]["candidate"]["sourceReference"])
        self.assertTrue(discrepancies[0]["auditReferences"])

        result = self.service.reconciliation_result({"asOfDate": "2025-02-28", "investor": "Redwood Investors LP"})
        self.assertEqual(len(result["openDiscrepancies"]["issues"]), 1)
        self.assertEqual(len(result["claimRightAssignments"]["events"]), 1)

    def test_sequential_and_same_date_transfers_are_not_silently_reordered(self) -> None:
        self._import()
        self._review_batch()
        first = self.sources / "07_sequential_transfer_2025-02-16.pdf"
        second = self.sources / "08_sequential_transfer_2025-02-17.pdf"
        same_day = self.sources / "09_same_day_transfer_2025-02-28.pdf"
        self._write_pdf(first, """SYNTHETIC TRANSFER\n\nIssue date: 16 February 2025\nEffective date: 16 February 2025\nInvestor group: Redwood Investors LP\nAtlas Custody Bank confirms a securities transfer of 100,000 nominal units of ISIN XS0123456789 from Horizon Credit Fund B (SF-B), account CUST-ALPHA-02, to Horizon Credit Fund A (SF-A), account CUST-ALPHA-01.""")
        self._write_pdf(second, """SYNTHETIC TRANSFER\n\nIssue date: 17 February 2025\nEffective date: 17 February 2025\nInvestor group: Redwood Investors LP\nAtlas Custody Bank confirms a securities transfer of 50,000 nominal units of ISIN XS0123456789 from Horizon Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.""")
        self._write_pdf(same_day, """SYNTHETIC TRANSFER\n\nIssue date: 28 February 2025\nEffective date: 28 February 2025\nInvestor group: Redwood Investors LP\nAtlas Custody Bank confirms a securities transfer of 1 nominal units of ISIN XS0123456789 from Horizon Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.""")
        self.assertTrue(all(item["ok"] for item in self.imports.import_authorized_paths([str(first), str(second), str(same_day)])["documents"]))
        rows = self._candidate_rows()
        added = [row["id"] for row in rows if row["original_filename"] in {first.name, second.name, same_day.name}]
        self.service.apply_policy({"candidateIds": added, "policy": "reviewed_batch", "acknowledged": True})

        between = self.service.holdings_as_of({"asOfDate": "2025-02-20"})["positions"]
        self.assertEqual({row["account"]: row["quantity"] for row in between}, {
            "CUST-ALPHA-01": "450000", "CUST-ALPHA-02": "950000", "CUST-ALPHA-03": "500000",
        })
        closing = self.service.holdings_as_of({"asOfDate": "2025-02-28"})["positions"]
        self.assertEqual(next(row["quantity"] for row in closing if row["account"] == "CUST-ALPHA-01"), "400000")
        self.assertEqual(len(next(row["sameDateTransfers"] for row in closing if row["account"] == "CUST-ALPHA-01")), 1)

    def test_conflicting_account_identifier_is_rejected_without_partial_transfer(self) -> None:
        self._import()
        self._review_batch()
        conflicting = self.sources / "conflicting-account-transfer.pdf"
        self._write_pdf(conflicting, """SYNTHETIC TRANSFER\n\nIssue date: 20 February 2025\nEffective date: 20 February 2025\nInvestor group: Redwood Investors LP\nAtlas Custody Bank confirms a securities transfer of 10,000 nominal units of ISIN XS0123456789 from Horizon Credit Fund B (SF-B), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.""")
        self.assertTrue(self.imports.import_authorized_paths([str(conflicting)])["documents"][0]["ok"])
        row = next(row for row in self._candidate_rows() if row["original_filename"] == conflicting.name)
        with self.assertRaisesRegex(ReconciliationError, "conflicts with its established investor or subfund"):
            self.service.apply_policy({"candidateIds": [row["id"]], "policy": "reviewed_batch", "acknowledged": True})
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)

    def test_cross_document_duplicate_transfer_and_assignment_add_no_second_business_event(self) -> None:
        self._import()
        self._review_batch()
        original_assignment = next(row for row in self._candidate_rows() if row["candidate_type"] == "claim_assignment")
        self.service.confirm({"candidateId": original_assignment["id"], "acknowledged": True})
        transfer_notice = self.sources / "duplicate-transfer.pdf"
        assignment_notice = self.sources / "duplicate-assignment.pdf"
        self._write_pdf(transfer_notice, """SYNTHETIC DUPLICATE TRANSFER NOTICE

Issue date: 20 February 2025
Effective date: 15 February 2025
Investor group: Redwood Investors LP
Atlas Custody Bank confirms a partial securities transfer of 200,000 nominal units of ISIN XS0123456789, Example Credit Notes 6.25% Perpetual, from Horizon Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.
This duplicate notice changes no securities movement.""")
        self._write_pdf(assignment_notice, """SYNTHETIC DUPLICATE CLAIM-RIGHTS ASSIGNMENT

Issue date: 21 February 2025
Effective date: 18 February 2025
Claim reference: CR-2025-ALPHA
Redwood Investors LP assigns 100 percent of its legal claim rights associated with ISIN XS0123456789 to Northstar Claims SPV, effective 18 February 2025.
This duplicate notice does not move securities.""")
        self.assertTrue(all(item["ok"] for item in self.imports.import_authorized_paths([str(transfer_notice), str(assignment_notice)])["documents"]))
        rows = self._candidate_rows()
        duplicate_transfer = next(row for row in rows if row["original_filename"] == "duplicate-transfer.pdf")
        duplicate_assignment = next(row for row in rows if row["original_filename"] == "duplicate-assignment.pdf")
        self.assertTrue(self.service.apply_policy({"candidateIds": [duplicate_transfer["id"]], "policy": "reviewed_batch", "acknowledged": True})["confirmed"][0]["duplicateEvidence"])
        self.assertTrue(self.service.confirm({"candidateId": duplicate_assignment["id"], "acknowledged": True})["duplicateEvidence"])
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_right_assignments").fetchone()[0], 1)

    def test_ai_validated_fields_stage_but_never_enter_the_batch_policy(self) -> None:
        self._import((NAMES[0],))
        assert self.storage.database is not None
        with self.storage.database.transaction() as connection:
            document = connection.execute("SELECT id FROM source_documents WHERE original_filename = ?", (NAMES[0],)).fetchone()
            extraction = connection.execute("SELECT id FROM document_extractions WHERE source_document_id = ? AND status = 'parsed'", (document["id"],)).fetchone()
            job_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO ai_processing_jobs (id, source_document_id, extraction_id, provider, model, prompt_version, schema_version, status, created_at, completed_at) VALUES (?, ?, ?, 'mock', 'mock-model', 'test', 'test', 'completed', '2025-02-03T00:00:00+00:00', '2025-02-03T00:00:00+00:00')",
                (job_id, document["id"], extraction["id"]),
            )
            facts = (
                ("investor_legal_entity", "name", "Redwood Investors LP", "Holdings!B5"),
                ("investment_vehicle_subfund", "name", "Horizon Credit Fund A (SF-A)", "Holdings!C8"),
                ("account", "account_identifier", "CUST-ALPHA-01", "Holdings!A8"),
                ("security", "isin", "XS0123456789", "Holdings!D8"),
                ("holding_position", "quantity", 600000, "Holdings!G8"),
                ("holding_position", "as_of_date", "2025-01-31", "Holdings!H8"),
            )
            for proposal_type, field_name, value, reference in facts:
                connection.execute(
                    "INSERT INTO ai_proposals (id, job_id, proposal_type, field_name, value_json, is_unknown, confidence, validation_status, source_reference, evidence_snippet, model_version, created_at) VALUES (?, ?, ?, ?, ?, 0, 0.9, 'validated', ?, 'synthetic', 'mock-model', '2025-02-03T00:00:00+00:00')",
                    (str(uuid.uuid4()), job_id, proposal_type, field_name, json.dumps(value), reference),
                )
        generated = self.service.generate({"documentId": document["id"], "includeAi": True})
        ai_candidate = next(item for item in generated["staged"] if item["origin"] == "ai_validated")
        result = self.service.apply_policy({"candidateIds": [ai_candidate["id"]], "policy": "reviewed_batch", "acknowledged": True})
        self.assertEqual(result["reviewRequired"], [{"candidateId": ai_candidate["id"], "reason": "ai_origin_requires_individual_review"}])

    def test_review_queue_preserves_provenance_and_audits_rejection(self) -> None:
        self._import()
        queue = self.service.candidates({})["candidates"]
        self.assertEqual(len(queue), 9)
        self.assertTrue(all(item["sourceDocumentId"] and item["sourceReference"] and item["evidenceSnippet"] for item in queue))
        assignment = next(item for item in queue if item["type"] == "claim_assignment")
        self.service.apply_policy({"candidateIds": [assignment["id"]], "policy": "reviewed_batch", "acknowledged": True})
        reviewed = next(item for item in self.service.candidates({})["candidates"] if item["id"] == assignment["id"])
        self.assertIn("individual confirmation", reviewed["reviewReason"])
        rejected = self.service.reject({"candidateId": assignment["id"], "reason": "Awaiting signed authority.", "acknowledged": True})
        self.assertEqual(rejected["status"], "rejected")
        rejected_queue = self.service.candidates({"status": "rejected"})["candidates"]
        self.assertEqual(rejected_queue[0]["reviewReason"], "Awaiting signed authority.")
        with self.assertRaisesRegex(ReconciliationError, "rejected candidate"):
            self.service.confirm({"candidateId": assignment["id"], "acknowledged": True})

    def test_missing_dates_unsupported_citations_and_batch_failure_do_not_persist_partial_records(self) -> None:
        self._import((NAMES[0],))
        opening = next(row for row in self._candidate_rows() if json.loads(row["payload_json"])["account"] == "CUST-ALPHA-01")
        source = self.sources / "overdraw-transfer.pdf"
        self._write_pdf(source, """SYNTHETIC TRANSFER\n\nIssue date: 20 February 2025\nEffective date: 16 February 2025\nInvestor group: Redwood Investors LP\nAtlas Custody Bank confirms a securities transfer of 700,000 nominal units of ISIN XS0123456789 from Horizon Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.""")
        imported = self.imports.import_authorized_paths([str(source)])
        self.assertTrue(imported["documents"][0]["ok"])
        overdraw = next(row for row in self._candidate_rows() if row["original_filename"] == "overdraw-transfer.pdf")
        with self.assertRaisesRegex(ReconciliationError, "exceeds the confirmed source balance"):
            self.service.apply_policy({"candidateIds": [opening["id"], overdraw["id"]], "policy": "reviewed_batch", "acknowledged": True})
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 0)

        missing = self.sources / "missing-date-transfer.pdf"
        self._write_pdf(missing, "Investor group: Redwood Investors LP. A securities transfer of 200,000 nominal units of ISIN XS0123456789 from Horizon Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.")
        self.assertTrue(self.imports.import_authorized_paths([str(missing)])["documents"][0]["ok"])
        self.assertFalse(any(row["original_filename"] == "missing-date-transfer.pdf" for row in self._candidate_rows()))
        with self.assertRaises(ReconciliationError) as invalid:
            self.service.stage({"type": "holding_snapshot", "sourceDocumentId": opening["source_document_id"], "sourceReference": "Holdings!Z999", "evidenceSnippet": "invalid", "documentIssueDate": "2025-02-02", "origin": "deterministic", "payload": json.loads(opening["payload_json"])})
        self.assertEqual(invalid.exception.code, "INVALID_EVIDENCE")

    @staticmethod
    def _write_pdf(path: Path, text: str) -> None:
        document = pymupdf.open()
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(54, 54, 558, 738), text, fontsize=11)
        document.save(path)
        document.close()


if __name__ == "__main__":
    unittest.main()
