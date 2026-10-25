"""Tests for canonical identities, aliases, and "same as?" questions."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf

from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.identity import (
    IdentityResolver,
    core_name,
    normalize_account_reference,
    normalize_name,
)
from group_claims_backend.services.reconciliation import ReconciliationError, ReconciliationService
from group_claims_backend.storage.service import StorageService


ROOT = Path(__file__).resolve().parents[2]
GENERATOR_ROOT = ROOT / "scripts" / "validation-sprint"
NAMES = (
    "01_opening_holdings_2025-01-31.xlsx",
    "02_partial_transfer_effective_2025-02-15.pdf",
    "03_claim_right_assignment_effective_2025-02-18.pdf",
    "04_later_holdings_2025-02-28.xlsx",
)
NOW = "2026-10-25T00:00:00+00:00"


class NormalizationTests(unittest.TestCase):
    def test_names_ignore_case_punctuation_and_equivalent_legal_forms(self) -> None:
        self.assertEqual(normalize_name("Alpha Capital Limited"), normalize_name("ALPHA CAPITAL LTD."))
        self.assertEqual(normalize_name("Beta S.A."), normalize_name("beta sa"))
        self.assertEqual(normalize_name("Gamma & Partners"), normalize_name("gamma and partners"))
        self.assertEqual(normalize_name("Delta Fund II"), normalize_name("Delta Fund 2"))
        self.assertNotEqual(normalize_name("Alpha Capital"), normalize_name("Alpha Capital Ltd"))
        self.assertEqual(core_name("Alpha Capital"), core_name("Alpha Capital Ltd"))

    def test_account_references_ignore_case_and_separators_only(self) -> None:
        self.assertEqual(normalize_account_reference("CUST-ALPHA-01"), normalize_account_reference("cust alpha 01"))
        self.assertNotEqual(normalize_account_reference("CUST-ALPHA-01"), normalize_account_reference("CUST-ALPHA-011"))


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-identity-")
        self.storage = StorageService(str(Path(self.temporary.name) / "app-data"))
        self.assertEqual(self.storage.initialize().status, "ready")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(self, resolver: IdentityResolver, target_type: str, target_id: str, value: str) -> None:
        resolver.record_alias(target_type, target_id, value, origin="observed", source_document_id=None, first_seen_on=None, now=NOW)  # type: ignore[arg-type]

    def test_resolution_tiers(self) -> None:
        assert self.storage.database is not None
        with self.storage.database.transaction() as connection:
            resolver = IdentityResolver(connection)
            self._record(resolver, "legal_entity", "entity-alpha", "Alpha Capital Limited")
            self._record(resolver, "investment_vehicle", "fund-2", "Delta Fund II")
            self._record(resolver, "account", "account-1", "CUST-ALPHA-01")

            matched = resolver.resolve("legal_entity", "ALPHA CAPITAL LTD.")
            self.assertEqual((matched.status, matched.target_id), ("matched", "entity-alpha"))
            ask = resolver.resolve("legal_entity", "Alpha Capital")
            self.assertEqual((ask.status, ask.target_id, ask.basis), ("ask", "entity-alpha", "same_name_without_legal_form"))
            typo = resolver.resolve("legal_entity", "Alpha Capitol Limited")
            self.assertEqual((typo.status, typo.basis), ("ask", "similar_spelling"))
            self.assertEqual(resolver.resolve("legal_entity", "Beta Partners").status, "new")

            self.assertEqual(resolver.resolve("investment_vehicle", "Delta Fund 2").target_id, "fund-2")
            # A different series marker is a different fund, never a suggestion.
            self.assertEqual(resolver.resolve("investment_vehicle", "Delta Fund III").status, "new")

            self.assertEqual(resolver.resolve("account", "cust alpha 01").target_id, "account-1")
            self.assertEqual(resolver.resolve("account", "CUST-ALPHA-011").status, "new")

            self._record(resolver, "legal_entity", "entity-other", "Alpha Capital Ltd")
            self.assertEqual(resolver.resolve("legal_entity", "Alpha Capital Limited").status, "ambiguous")


class IdentityPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-identity-pipeline-")
        self.root = Path(self.temporary.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for script in ("build_synthetic_pdfs.py", "build_synthetic_workbooks.py"):
            subprocess.run([sys.executable, str(GENERATOR_ROOT / script), str(self.sources)], check=True)
        self.storage = StorageService(str(self.root / "app-data"))
        self.assertEqual(self.storage.initialize().status, "ready")
        self.imports = DocumentImportService(self.storage)
        self.service = ReconciliationService(self.storage)
        self._import(*(self.sources / name for name in NAMES))
        rows = self._candidate_rows()
        self.service.apply_policy({"candidateIds": [str(row["id"]) for row in rows], "policy": "reviewed_batch", "acknowledged": True})
        assignment = next(row for row in rows if row["candidate_type"] == "claim_assignment")
        self.service.confirm({"candidateId": assignment["id"], "acknowledged": True})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self, *paths: Path) -> None:
        result = self.imports.import_authorized_paths([str(path) for path in paths])
        self.assertTrue(all(item["ok"] for item in result["documents"]))

    def _candidate_rows(self):
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            return connection.execute(
                "SELECT c.*, d.original_filename FROM reconciliation_candidates c "
                "JOIN source_documents d ON d.id = c.source_document_id ORDER BY c.created_at, c.id"
            ).fetchall()

    def _candidate_for(self, filename: str):
        return next(row for row in self._candidate_rows() if row["original_filename"] == filename)

    def _count(self, query: str) -> int:
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            return int(connection.execute(query).fetchone()[0])

    def _statement(self, filename: str, account: str, subfund: str, quantity: str, day: str) -> Path:
        path = self.sources / filename
        self._write_pdf(path, f"""SYNTHETIC - CUSTODIAN POSITION STATEMENT

Issue date: 10 May 2025
Custodian: Atlas Custody Bank

For account {account}, {subfund}, the recorded holding of ISIN XS0123456789 is {quantity} nominal units as of {day}.""")
        return path

    def test_spelling_variants_of_known_account_and_subfund_link_automatically(self) -> None:
        accounts_before = self._count("SELECT COUNT(*) FROM accounts")
        self._import(self._statement("variant.pdf", "cust-alpha-01", "HORIZON CREDIT FUND A (SF-A)", "400,000", "31 March 2025"))
        candidate = self._candidate_for("variant.pdf")
        self.assertEqual(candidate["validation_status"], "supported")
        self.assertEqual(self.service.confirm({"candidateId": candidate["id"], "acknowledged": True})["type"], "holding_snapshot")

        positions = self.service.holdings_as_of({"asOfDate": "2025-03-31"})["positions"]
        self.assertIn(("CUST-ALPHA-01", "400000"), {(item["account"], item["quantity"]) for item in positions})
        self.assertEqual(self._count("SELECT COUNT(*) FROM accounts"), accounts_before)
        self.assertEqual(self._count("SELECT COUNT(*) FROM investment_vehicles"), 3)
        self.assertEqual(self._count("SELECT COUNT(*) FROM identity_link_questions"), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM identity_aliases WHERE alias_text = 'cust-alpha-01' AND target_type = 'account'"), 1)

    def test_different_subfund_name_on_known_account_asks_and_remembers_the_answer(self) -> None:
        self._import(self._statement("short-name.pdf", "CUST-ALPHA-01", "Horizon Credit Fund A", "400,000", "31 March 2025"))
        candidate = self._candidate_for("short-name.pdf")
        self.assertEqual(candidate["validation_status"], "review_required")
        with self.assertRaises(ReconciliationError) as raised:
            self.service.confirm({"candidateId": candidate["id"], "acknowledged": True})
        self.assertEqual(raised.exception.code, "IDENTITY_REVIEW_REQUIRED")

        questions = self.service.identity_questions({})["questions"]
        self.assertEqual(len(questions), 1)
        question = questions[0]
        self.assertEqual(question["targetType"], "investment_vehicle")
        self.assertEqual(question["observedText"], "Horizon Credit Fund A")
        self.assertEqual(question["suggested"]["label"], "Horizon Credit Fund A (SF-A)")
        self.assertEqual(question["matchBasis"], "same_account_reference")
        self.assertEqual(self._count("SELECT COUNT(*) FROM issues WHERE status = 'open' AND reason = 'possible same identity'"), 1)

        answered = self.service.resolve_identity_question({"questionId": question["id"], "decision": "same", "acknowledged": True})
        self.assertEqual(answered["status"], "linked")
        self.assertIn({"text": "Horizon Credit Fund A", "origin": "user_confirmed"}, [
            {"text": alias["text"], "origin": alias["origin"]} for alias in answered["suggested"]["aliases"]
        ])
        self.assertEqual(self._count("SELECT COUNT(*) FROM issues WHERE status = 'open' AND reason = 'possible same identity'"), 0)
        self.assertEqual(self._candidate_for("short-name.pdf")["validation_status"], "supported")
        self.service.confirm({"candidateId": candidate["id"], "acknowledged": True})

        # The next document using the same spelling resolves without asking.
        self._import(self._statement("short-name-april.pdf", "CUST-ALPHA-01", "Horizon Credit Fund A", "400,000", "30 April 2025"))
        april = self._candidate_for("short-name-april.pdf")
        batch = self.service.apply_policy({"candidateIds": [april["id"]], "policy": "reviewed_batch", "acknowledged": True})
        self.assertEqual(len(batch["confirmed"]), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM identity_link_questions"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM investment_vehicles"), 3)

    def test_distinct_answer_creates_a_separate_party_and_is_not_asked_again(self) -> None:
        path = self.sources / "near-name-assignment.pdf"
        self._write_pdf(path, """SYNTHETIC CLAIM-RIGHTS ASSIGNMENT

Issue date: 3 March 2025
Effective date: 1 March 2025
Claim reference: CR-2025-BETA
Redwood Investors LP assigns 50 percent of its legal claim rights associated with ISIN XS0123456789 to Northstar Claim SPV, effective 1 March 2025.""")
        self._import(path)
        candidate = self._candidate_for("near-name-assignment.pdf")
        entities_before = self._count("SELECT COUNT(*) FROM legal_entities")

        batch = self.service.apply_policy({"candidateIds": [candidate["id"]], "policy": "reviewed_batch", "acknowledged": True})
        self.assertEqual(batch["reviewRequired"], [{"candidateId": candidate["id"], "reason": "identity_link_requires_review"}])
        question = self.service.identity_questions({})["questions"][0]
        self.assertEqual((question["observedText"], question["suggested"]["label"]), ("Northstar Claim SPV", "Northstar Claims SPV"))

        self.service.resolve_identity_question({"questionId": question["id"], "decision": "distinct", "acknowledged": True})
        self.service.confirm({"candidateId": candidate["id"], "acknowledged": True})
        self.assertEqual(self._count("SELECT COUNT(*) FROM legal_entities"), entities_before + 1)
        self.assertEqual(self.service.identity_questions({})["questions"], [])
        self.assertEqual(len(self.service.identity_questions({"status": "distinct"})["questions"]), 1)
        with self.assertRaises(ReconciliationError) as raised:
            self.service.resolve_identity_question({"questionId": question["id"], "decision": "same", "acknowledged": True})
        self.assertEqual(raised.exception.code, "IDENTITY_QUESTION_RESOLVED")

    def test_answer_requires_explicit_acknowledgement(self) -> None:
        with self.assertRaises(ReconciliationError) as raised:
            self.service.resolve_identity_question({"questionId": "missing", "decision": "same", "acknowledged": False})
        self.assertEqual(raised.exception.code, "CONFIRMATION_REQUIRED")

    @staticmethod
    def _write_pdf(path: Path, text: str) -> None:
        document = pymupdf.open()
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(54, 54, 558, 738), text, fontsize=11)
        document.save(path)
        document.close()


if __name__ == "__main__":
    unittest.main()
