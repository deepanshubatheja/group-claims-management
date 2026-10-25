"""Tests for re-reading already imported documents with current rules."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
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


class RereadDocumentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-reread-")
        self.root = Path(self.temporary.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for script in ("build_synthetic_pdfs.py", "build_synthetic_workbooks.py"):
            subprocess.run([sys.executable, str(GENERATOR_ROOT / script), str(self.sources)], check=True)
        self.storage = StorageService(str(self.root / "app-data"))
        self.assertEqual(self.storage.initialize().status, "ready")
        self.imports = DocumentImportService(self.storage)
        self.service = ReconciliationService(self.storage)
        result = self.imports.import_authorized_paths([str(self.sources / name) for name in NAMES])
        self.assertTrue(all(item["ok"] for item in result["documents"]))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _reread(self) -> dict[str, object]:
        return dict(self.service.reread_documents({"acknowledged": True}))

    def _count(self, query: str, values: tuple[object, ...] = ()) -> int:
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            return int(connection.execute(query, values).fetchone()[0])

    def test_reread_adds_no_duplicates_even_when_references_changed(self) -> None:
        before = self._count("SELECT COUNT(*) FROM reconciliation_candidates")
        self.assertEqual(self._reread(), {"documentsRead": 6, "documentsFailed": 0, "documentsWithNewCandidates": 0, "newCandidates": 0, "truncated": False})

        # Candidates staged by earlier rules cited every block on the page.
        assert self.storage.database is not None
        with self.storage.database.transaction() as connection:
            connection.execute(
                "UPDATE reconciliation_candidates SET source_reference = 'page 1 block 0; page 1 block 1; page 1 block 2; page 1 block 3' "
                "WHERE candidate_type = 'securities_transfer'"
            )
        self.assertEqual(self._reread()["newCandidates"], 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reconciliation_candidates"), before)
        self.assertEqual(self._count("SELECT COUNT(*) FROM audit_events WHERE event_type = 'reconciliation_documents_reread'"), 2)

    def test_document_imported_before_new_rules_gains_candidates_for_review_only(self) -> None:
        path = self.sources / "reworded-statement.pdf"
        document = pymupdf.open()
        document.new_page().insert_textbox(pymupdf.Rect(54, 54, 558, 738), "Issue date: 5 April 2025\n\nAs at 31-Mar-2025, Horizon Credit Fund A (SF-A) held 400 000 nominal of ISIN XS0123456789 in account CUST-ALPHA-01.", fontsize=11)
        document.save(path)
        document.close()
        imported = self.imports.import_authorized_paths([str(path)])["documents"][0]
        # Model an import made under earlier rules, which found nothing here.
        assert self.storage.database is not None
        with self.storage.database.transaction() as connection:
            connection.execute("DELETE FROM reconciliation_candidates WHERE source_document_id = ?", (imported["document"]["id"],))

        summary = self._reread()
        self.assertEqual((summary["newCandidates"], summary["documentsWithNewCandidates"]), (1, 1))
        self.assertEqual(self._count("SELECT COUNT(*) FROM reconciliation_candidates WHERE source_document_id = ? AND lifecycle_status = 'candidate'", (imported["document"]["id"],)), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM holding_positions"), 0)

    def test_rejected_candidates_stay_rejected(self) -> None:
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            candidate_id = connection.execute("SELECT id FROM reconciliation_candidates WHERE candidate_type = 'claim_assignment'").fetchone()["id"]
        self.service.reject({"candidateId": candidate_id, "reason": "Not relevant to this matter.", "acknowledged": True})
        self.assertEqual(self._reread()["newCandidates"], 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reconciliation_candidates WHERE id = ? AND lifecycle_status = 'rejected'", (candidate_id,)), 1)

    def test_requires_acknowledgement(self) -> None:
        with self.assertRaises(ReconciliationError) as raised:
            self.service.reread_documents({})
        self.assertEqual(raised.exception.code, "CONFIRMATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
