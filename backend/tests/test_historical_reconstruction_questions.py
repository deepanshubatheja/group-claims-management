"""Regression coverage for the six-document historical reconstruction prompt.

The providers in this file are local mocks.  They verify ClaimFlow's scoped
retrieval, provenance validation, and fallback behavior without asserting that
a live Codex or OpenAI model has understood the scenario.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import InvalidOperation
from pathlib import Path
from unittest.mock import patch

from group_claims_backend.services.ai import AIService, AnswerResult, QuestionResponseContract
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.matter_review import MatterReviewService
from group_claims_backend.storage.service import StorageService


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_ROOT = REPOSITORY_ROOT / "scripts" / "validation-sprint"
SOURCE_NAMES = (
    "01_opening_holdings_2025-01-31.xlsx",
    "02_partial_transfer_effective_2025-02-15.pdf",
    "03_claim_right_assignment_effective_2025-02-18.pdf",
    "04_later_holdings_2025-02-28.xlsx",
    "05_contradictory_custodian_statement_2025-02-28.pdf",
    "06_approved_billing_instruction_2025-03-01.pdf",
)
HISTORICAL_QUESTION = (
    "Using all six synthetic source documents, reconstruct the holdings of Redwood Investors LP as of January 31, "
    "February 14, February 15, and February 28, 2025. For each date, report Fund A, Fund B and Fund C holdings. "
    "Identify the securities transfer's effective date, distinguish the legal claim-right assignment, and flag any "
    "contradictory evidence. Cite the source document supporting each finding. Clearly distinguish documented holdings "
    "from balances inferred through the transfer. Do not silently resolve contradictions. Also report the approved "
    "pro-rata billing allocation."
)

COMPOUND_CHRONOLOGY_QUESTION = (
    "Compare documents 01 through 05 chronologically. Identify every securities transfer, claim-right assignment, "
    "and change in holdings. For each event, provide its effective date, originating subfund, receiving subfund, "
    "securities or claim rights affected, amount transferred, and exact supporting evidence. Explain any contradictions "
    "and whether they affect the approved February 2025 billing allocation in document 06. Do not assume securities ownership changed "
    "merely because claim rights were assigned."
)

DOCUMENT_REFERENCE_CONTRADICTION_QUESTION = (
    "Compare the February 28 holdings in document 04 with the contradictory custodian statement in document 05. "
    "Identify every conflicting participant, security, account, and nominal amount, citing exact evidence. Then explain "
    "whether the approved February billing instruction in document 06 resolves those conflicts, why Fund C is or is not "
    "included in the $120,000 allocation, and what additional verification the lawyer should perform. Do not silently "
    "change any approved billing values."
)

CALENDAR_DATES_COMPOUND_QUESTION = (
    "Analyze the selected documents for Redwood Investors LP. Show the holdings of Horizon Credit Fund A, "
    "Horizon Credit Fund B, and Meridian Income Fund C on January 31 and February 28, 2025. Explain the "
    "securities transfer effective February 15 and the separate claim-right assignment effective February 18. "
    "Identify contradictions between the February 28 holdings statement and custodian evidence. Calculate February "
    "2025 pro-rata billing by subfund using the approved instruction, including the fee pool, formula, exclusions, "
    "and exact evidence. If billing citations are incomplete, mark billing unsupported while preserving valid holdings "
    "and contradiction findings. Present a timeline, holdings comparison, conflicts, and billing table with source references."
)


class _HistoricalProvider:
    def __init__(self, *, unknown: bool = False, partial: bool = False, citation_count: int | None = 2) -> None:
        self.unknown = unknown
        self.partial = partial
        self.citation_count = citation_count
        self.calls: list[tuple[str, str, tuple[str, ...], QuestionResponseContract | None]] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=(), *, response_contract=None) -> AnswerResult:
        self.calls.append((question, context, tuple(allowed_citation_keys), response_contract))
        if self.unknown:
            return AnswerResult(None, True, (), "mock-historical-provider", 10, 5, 15)
        if self.partial:
            return AnswerResult(
                "Holdings (documented): 31 January 2025 — Fund A 600,000, Fund B 800,000, Fund C 500,000.\n\n"
                "Contradictory evidence (unresolved): The 28 February Fund B statements conflict.\n\n"
                "Limitation (unsupported): No further balance is established.",
                False,
                tuple({"sourceReference": key, "evidenceSnippet": "synthetic"} for key in allowed_citation_keys[:3]),
                "mock-historical-provider", 10, 5, 15,
                historical_metadata={
                    "modelUnknown": False,
                    "responseStatus": "partial",
                    "validatedSectionKinds": ("holdings", "contradiction", "limitation"),
                    "validatedSectionCounts": {"holdings": 1, "contradiction": 1, "limitation": 1},
                    "validatedSectionStatusCounts": {"documented": 1, "inferred": 0, "unresolved": 1, "unsupported": 1},
                    "validatedCitationCount": 3,
                },
            )
        return AnswerResult(
            "Documented holdings: 31 January 2025 — Fund A 600,000, Fund B 800,000, Fund C 500,000. "
            "14 February is inferred unchanged from the 31 January snapshot and the 15 February effective transfer. "
            "On 15 February, the securities transfer records 200,000 nominal units from Fund A to Fund B; the "
            "post-transfer 400,000 / 1,000,000 / 500,000 balances are inferred until the 28 February snapshot. "
            "On 18 February, Redwood Investors LP assigned 100% of claim CR-2025-ALPHA to Northstar Claims SPV; "
            "that assignment does not move securities. The 28 February holdings statement records Fund A 400,000, "
            "Fund B 1,000,000 and Fund C 500,000, while a separate custodian statement records Fund B as 900,000, "
            "so the conflict remains unresolved. The approved USD 120,000 fee pool allocates USD 34,285.71 to Fund A, "
            "USD 85,714.29 to Fund B, and excludes Fund C.",
            False,
            # Match the production provider envelope, which keeps its model
            # response concise. Matter Review supplements this with one
            # locally revalidated coverage source per included document.
            tuple({"sourceReference": key, "evidenceSnippet": "synthetic"} for key in allowed_citation_keys[:self.citation_count]),
            "mock-historical-provider", 10, 5, 15,
        )


class HistoricalReconstructionQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-historical-question-")
        self.previous_environment = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        os.environ["OPENAI_API_KEY"] = "synthetic-key"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        self.source_dir = Path(self.temporary.name) / "sources"
        self.source_dir.mkdir()
        subprocess.run([sys.executable, str(GENERATOR_ROOT / "build_synthetic_pdfs.py"), str(self.source_dir)], check=True)
        subprocess.run([sys.executable, str(GENERATOR_ROOT / "build_synthetic_workbooks.py"), str(self.source_dir)], check=True)
        self.storage = StorageService(str(Path(self.temporary.name) / "app"))
        self.assertEqual(self.storage.initialize().status, "ready")
        imported = DocumentImportService(self.storage).import_authorized_paths([
            str(self.source_dir / filename) for filename in SOURCE_NAMES
        ])
        self.document_ids = [str(item["document"]["id"]) for item in imported["documents"]]
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})

    def tearDown(self) -> None:
        for key, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _service(self, provider: _HistoricalProvider) -> MatterReviewService:
        analysis = AIAnalysisService(self.storage, provider_factory=lambda: provider)  # type: ignore[arg-type]
        return MatterReviewService(self.storage, analysis)

    @staticmethod
    def _request() -> dict[str, object]:
        return {"matterId": "workspace", "scope": {"mode": "all"}, "question": HISTORICAL_QUESTION}

    def _ask(self, service: MatterReviewService) -> dict[str, object]:
        return self._ask_request(service, self._request())

    def _ask_request(self, service: MatterReviewService, request: dict[str, object]) -> dict[str, object]:
        preview = service.question_preview(request)
        consented = [item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]]
        started = service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": consented})
        for _ in range(100):
            status = service.question_status({"questionId": started["questionId"]})
            if status["status"] in {"completed", "failed"}:
                return dict(status)
            time.sleep(0.02)
        self.fail("historical reconstruction question did not finish")

    def test_composable_requirements_cover_all_six_documents_and_preserve_grounded_answer(self) -> None:
        requirements = MatterReviewService._analysis_requirements(HISTORICAL_QUESTION)
        self.assertTrue(requirements.historical_holdings)
        self.assertTrue(requirements.multiple_as_of_dates)
        self.assertTrue(requirements.securities_transfer)
        self.assertTrue(requirements.claim_assignment)
        self.assertTrue(requirements.contradictions)
        self.assertTrue(requirements.pro_rata_billing)

        provider = _HistoricalProvider()
        service = self._service(provider)
        preview = service.question_preview(self._request())
        self.assertFalse(preview["localOnly"])
        self.assertEqual(preview["scope"]["documentsSelected"], 6)
        self.assertEqual(preview["scope"]["documentsWithEvidence"], 6)
        self.assertTrue(preview["scope"]["comparisonCoverageComplete"])
        self.assertEqual(
            {item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]}, set(self.document_ids),
        )

        status = self._ask(service)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["intent"], "historical_reconstruction")
        self.assertEqual(len(provider.calls), 1)
        context = provider.calls[0][1]
        contract = provider.calls[0][3]
        self.assertIsInstance(contract, QuestionResponseContract)
        assert contract is not None
        self.assertTrue(contract.historical_reconstruction)
        self.assertGreaterEqual(contract.max_citations, 10)
        self.assertIn("Keep securities transfers separate", str(contract.instructions))
        for filename in SOURCE_NAMES:
            self.assertIn(filename, context)
        # These are the source facts that must be available to a real
        # provider. The mock does not establish model comprehension; it
        # verifies that bounded retrieval did not silently drop a required
        # document or fact before the provider is called.
        for expected in (
            "600,000", "800,000", "500,000", "400,000", "1,000,000",
            "200,000", "CR-2025-ALPHA", "900,000", "120,000",
        ):
            self.assertIn(expected, context)
        answer = str(status["answer"])
        for expected in ("600,000", "800,000", "500,000", "200,000", "CR-2025-ALPHA", "900,000", "34,285.71", "85,714.29"):
            self.assertIn(expected, answer)
        # All six quantified holdings facts are direct citation targets in
        # the provider context.  The mock deliberately returns only two
        # citations, so coverage sources must not be appended to its answer
        # and misrepresented as proof of every conclusion.
        for row in (8, 9, 10):
            self.assertIn(f"SOURCE: Holdings!G{row}", context)
        self.assertEqual(len(status["citations"]), 2)
        self.assertEqual(status["scope"]["directHoldingSourcesRequired"], 6)
        self.assertEqual(status["scope"]["directHoldingSourcesIncluded"], 6)
        execution = status["execution"]
        self.assertTrue(execution["providerCallAttempted"])
        self.assertEqual(execution["providerOutcome"], "completed")
        self.assertFalse(execution["fallbackUsed"])
        self.assertEqual(set(execution["documentsIncluded"]), set(self.document_ids))
        self.assertTrue(execution["requirements"]["historicalHoldings"])
        self.assertEqual(execution["responseStatus"], "complete")
        self.assertEqual(execution["evidenceItemCounts"]["historicalHoldingRows"]["included"], 6)

    def test_calendar_dates_in_a_compound_question_do_not_narrow_all_document_scope(self) -> None:
        """Business dates are analysis targets, not implicit filename selectors."""
        requirements = MatterReviewService._analysis_requirements(CALENDAR_DATES_COMPOUND_QUESTION)
        self.assertTrue(requirements.historical_holdings)
        self.assertTrue(requirements.multiple_as_of_dates)
        self.assertTrue(requirements.requires_cross_document_coverage)

        provider = _HistoricalProvider()
        service = self._service(provider)
        request = {"matterId": "workspace", "scope": {"mode": "all"}, "question": CALENDAR_DATES_COMPOUND_QUESTION}
        preview = service.question_preview(request)
        self.assertEqual(preview["scope"]["documentsSelected"], 6)
        self.assertEqual(preview["scope"]["documentsWithEvidence"], 6)
        self.assertEqual(preview["documentScope"]["resolvedDocuments"], [])
        self.assertEqual(
            {item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]}, set(self.document_ids),
        )

        status = self._ask_request(service, request)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(status["citations"]), 2)
        for filename in SOURCE_NAMES:
            self.assertIn(filename, provider.calls[0][1])

    def test_historical_billing_context_keeps_each_instruction_fact_and_derived_result_separate(self) -> None:
        """A fee-pool block must not stand in for every billing input."""
        provider = _HistoricalProvider(citation_count=None)
        service = self._service(provider)
        status = self._ask(service)
        self.assertEqual(status["status"], "completed")
        context = provider.calls[0][1]
        contract = provider.calls[0][3]
        assert contract is not None
        self.assertEqual(len(contract.required_billing_citation_keys), 5)
        self.assertIn("DERIVED LOCAL PRO-RATA CALCULATION", context)
        for expected in ("1,400,000", "USD 34,285.71", "USD 85,714.29"):
            self.assertIn(expected, context)
        # Document 06 is deliberately split into PDF blocks. Each material
        # fact must retain its own direct target rather than inheriting the
        # fee-pool block's reference.
        prepared = service._matter_question_params(self._request(), acknowledged=False)
        _scope, plan, _follow_up = service._prepare_question(prepared)
        source_to_key = {
            (target[0], target[2]): key
            for key, target in plan["citationTargets"].items()  # type: ignore[index,union-attr]
        }
        billing_document = self.document_ids[5]
        billing_keys = [source_to_key[(billing_document, f"page 1 block {block}")] for block in range(2, 7)]
        self.assertEqual(set(contract.required_billing_citation_keys), set(billing_keys))
        self.assertEqual(len(set(billing_keys)), 5)
        for block in range(2, 7):
            self.assertIn(f"SOURCE: page 1 block {block}", context)
        self.assertIn(
            "Meridian Income Fund C (SF-C) is excluded because it is not participating in the relevant litigation",
            context,
        )

    def test_historical_billing_context_reports_a_coverage_limit_instead_of_a_partial_allocation(self) -> None:
        provider = _HistoricalProvider()
        service = self._service(provider)
        # Keep the request deliberately bounded below the known complete
        # six-document fixture size. The planner must drop the derived
        # allocation, rather than attaching it to incomplete source facts.
        with patch("group_claims_backend.services.matter_review.MAX_HISTORICAL_QUESTION_CHARACTERS", 3_200):
            prepared = service._matter_question_params(self._request(), acknowledged=False)
            _scope, plan, _follow_up = service._prepare_question(prepared)
        self.assertIn("BILLING COVERAGE LIMITATION", str(plan["context"]))
        contract = plan["responseContract"]
        assert isinstance(contract, QuestionResponseContract)
        self.assertEqual(contract.required_billing_citation_keys, ())
        self.assertNotIn("DERIVED LOCAL PRO-RATA CALCULATION", str(plan["context"]))

    def test_incomplete_numbered_participant_basis_does_not_parse_as_an_allocation_instruction(self) -> None:
        source = """Approved billing instruction
Billing period
1 February 2025 to 28 February 2025
Fee pool
USD 120,000.00
Allocation rule
eligible nominal amount / total eligible nominal amount
Eligible participant 1
Fund A (SF-A): 400,000
Eligible participant 2
Fund B (SF-B): amount pending confirmation
"""
        self.assertIsNone(MatterReviewService._parse_document_billing_instruction(source))

    def test_partial_provider_result_is_preserved_with_its_supported_findings(self) -> None:
        status = self._ask(self._service(_HistoricalProvider(partial=True)))
        self.assertEqual(status["status"], "completed")
        self.assertIn("Fund A 600,000", str(status["answer"]))
        self.assertIn("Contradictory evidence", str(status["answer"]))
        self.assertFalse(status["unknown"])
        self.assertFalse(status["execution"]["fallbackUsed"])
        self.assertEqual(status["execution"]["responseStatus"], "partial")
        self.assertEqual(status["execution"]["validatedSectionCounts"]["holdings"], 1)

    def test_scope_wide_billing_question_uses_all_six_documents_without_inventing_fee_pools(self) -> None:
        question = "Can you give me pro rata calculation for each of the document below?"
        request = {"matterId": "workspace", "scope": {"mode": "all"}, "question": question}
        provider = _HistoricalProvider()
        service = self._service(provider)
        preview = service.question_preview(request)
        self.assertTrue(preview["localOnly"])
        self.assertEqual(preview["scope"]["documentsSelected"], 6)
        self.assertEqual(preview["scope"]["documentsWithEvidence"], 6)
        started = service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": []})
        status = service.question_status({"questionId": started["questionId"]})
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["intent"], "calculation")
        self.assertEqual(len(provider.calls), 0)
        self.assertEqual({citation["documentId"] for citation in status["citations"]}, set(self.document_ids))
        answer = str(status["answer"])
        self.assertIn("USD 120,000.00", answer)
        self.assertIn("USD 34,285.71", answer)
        self.assertIn("USD 85,714.29", answer)
        self.assertIn("not an invoice", answer)
        self.assertIn("claim-right assignment", answer)
        self.assertIn("securities-movement evidence", answer)

    def test_compound_chronology_and_billing_question_uses_historical_path_and_all_selected_documents(self) -> None:
        requirements = MatterReviewService._analysis_requirements(COMPOUND_CHRONOLOGY_QUESTION)
        self.assertTrue(requirements.historical_chronology)
        self.assertTrue(requirements.holdings_comparison)
        self.assertTrue(requirements.securities_transfer)
        self.assertTrue(requirements.claim_assignment)
        self.assertTrue(requirements.contradictions)
        self.assertTrue(requirements.pro_rata_billing)
        self.assertTrue(requirements.billing_impact)
        self.assertTrue(requirements.requires_cross_document_coverage)

        provider = _HistoricalProvider(citation_count=None)
        service = self._service(provider)
        request = {"matterId": "workspace", "scope": {"mode": "all"}, "question": COMPOUND_CHRONOLOGY_QUESTION}
        preview = service.question_preview(request)
        self.assertFalse(preview["localOnly"])
        self.assertEqual(preview["intent"], "historical_reconstruction")
        self.assertEqual(preview["scope"]["documentsSelected"], 6)
        self.assertEqual(preview["scope"]["documentsWithEvidence"], 6)
        status = self._ask_request(service, request)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["intent"], "historical_reconstruction")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual({citation["documentId"] for citation in status["citations"]}, set(self.document_ids))
        answer = str(status["answer"])
        for expected in ("200,000", "Fund A", "Fund B", "CR-2025-ALPHA", "Northstar Claims SPV", "900,000", "120,000", "34,285.71", "85,714.29"):
            self.assertIn(expected, answer)
        self.assertIn("does not move securities", answer)
        context = provider.calls[0][1]
        contract = provider.calls[0][3]
        assert contract is not None
        self.assertIn("billing basis", str(contract.instructions))
        # Both live provider adapters reject an oversized trusted instruction
        # contract before making a request.  Keep this compound contract
        # inside that shared bounded limit.
        self.assertLessEqual(len(contract.instructions or ""), 2_400)
        for filename in SOURCE_NAMES:
            self.assertIn(filename, context)

    def test_numbered_document_references_preserve_conflict_and_billing_analysis(self) -> None:
        requirements = MatterReviewService._analysis_requirements(DOCUMENT_REFERENCE_CONTRADICTION_QUESTION)
        self.assertTrue(requirements.holdings_comparison)
        self.assertTrue(requirements.contradictions)
        self.assertTrue(requirements.pro_rata_billing)
        self.assertTrue(requirements.billing_impact)
        self.assertTrue(requirements.requires_cross_document_coverage)

        provider = _HistoricalProvider(citation_count=None)
        service = self._service(provider)
        request = {
            "matterId": "workspace", "scope": {"mode": "all"},
            "question": DOCUMENT_REFERENCE_CONTRADICTION_QUESTION,
        }
        preview = service.question_preview(request)
        resolved = preview["documentScope"]["resolvedDocuments"]
        expected_ids = self.document_ids[3:6]
        self.assertEqual([item["documentId"] for item in resolved], expected_ids)
        self.assertFalse(preview["localOnly"])
        self.assertEqual(preview["intent"], "historical_reconstruction")
        self.assertTrue(preview["scope"]["comparisonCoverageComplete"])
        self.assertEqual(
            {item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]}, set(expected_ids),
        )

        status = self._ask_request(service, request)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["intent"], "historical_reconstruction")
        self.assertFalse(status["execution"]["fallbackUsed"])
        self.assertEqual({citation["documentId"] for citation in status["citations"]}, set(expected_ids))
        self.assertEqual(len(provider.calls), 1)
        context = provider.calls[0][1]
        self.assertIn(SOURCE_NAMES[3], context)
        self.assertIn(SOURCE_NAMES[4], context)
        self.assertIn(SOURCE_NAMES[5], context)
        self.assertNotIn(SOURCE_NAMES[0], context)
        self.assertNotIn(SOURCE_NAMES[1], context)
        self.assertNotIn(SOURCE_NAMES[2], context)
        # The contradiction analysis receives the actual 28 February Fund
        # A/B/C rows, not a generic workbook title, plus the competing
        # custodian statement and approved billing instruction.
        for source in ("Holdings!G8", "Holdings!G9", "Holdings!G10"):
            self.assertIn(source, context)
        for expected in ("400,000", "1,000,000", "500,000", "900,000", "120,000", "CUST-ALPHA-02", "XS0123456789"):
            self.assertIn(expected, context)

    def test_chronology_scope_and_billing_only_scope_remain_distinct_when_cloud_ai_is_disabled(self) -> None:
        AIService(self.storage).set_authorization({"authorized": False, "acknowledged": True})
        service = self._service(_HistoricalProvider())
        chronology = {"matterId": "workspace", "scope": {"mode": "selected", "documentIds": [self.document_ids[0]]}, "question": "Build a chronology of holdings changes."}
        preview = service.question_preview(chronology)
        self.assertFalse(preview["localOnly"])
        self.assertEqual(preview["intent"], "historical_reconstruction")
        self.assertEqual(preview["scope"]["documentsSelected"], 1)
        billing = {"matterId": "workspace", "scope": {"mode": "all"}, "question": "Calculate the approved pro-rata billing allocation."}
        billing_preview = service.question_preview(billing)
        self.assertTrue(billing_preview["localOnly"])

    def test_composable_requirement_variants_do_not_promote_billing_over_evidence_tasks(self) -> None:
        cases = (
            ("Build a chronology of the documents.", {"historical_chronology": True, "requires_cross_document_coverage": True}),
            ("Calculate the approved pro-rata billing allocation.", {"pro_rata_billing": True, "requires_cross_document_coverage": False}),
            ("Identify the claim-right assignment; no securities transfer occurred.", {"claim_assignment": True, "securities_transfer": False}),
            ("Identify the confirmed securities transfer and its effective date.", {"securities_transfer": True}),
            ("Explain the conflicting custodian statements and the disputed balance.", {"contradictions": True}),
            ("Identify the securities transfer even if its effective date is missing.", {"securities_transfer": True}),
            ("What issued invoices remain outstanding?", {"invoice_status": True}),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                requirements = MatterReviewService._analysis_requirements(question)
                for field, value in expected.items():
                    self.assertEqual(getattr(requirements, field), value)

    def test_unknown_provider_result_reports_historical_limit_instead_of_legal_template(self) -> None:
        provider = _HistoricalProvider(unknown=True)
        status = self._ask(self._service(provider))
        self.assertEqual(status["status"], "completed")
        self.assertIn("historical reconstruction", str(status["answer"]))
        self.assertNotIn("contains express litigation", str(status["answer"]))
        self.assertTrue(status["execution"]["fallbackUsed"])
        self.assertEqual(status["execution"]["fallbackReason"], "provider_unsupported_answer")
        self.assertEqual(status["execution"]["responseStatus"], "fallback")
        self.assertTrue(status["execution"]["modelUnknown"])
        self.assertTrue(all(citation.get("origin") == "local_fallback" for citation in status["citations"]))

    def test_question_submission_reports_a_busy_local_database_without_leaving_a_queued_job(self) -> None:
        service = self._service(_HistoricalProvider())

        def locked_audit(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        service._record_question_consent = locked_audit  # type: ignore[method-assign]
        preview = service.question_preview(self._request())
        consented = [item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]]
        with self.assertRaisesRegex(
            ValueError, "local database is busy",
        ):
            service.ask_question({**self._request(), "acknowledged": True, "consentedDocumentIds": consented})
        self.assertEqual(service._questions, {})

    def test_malformed_legacy_holding_rows_do_not_abort_historical_question_planning(self) -> None:
        service = self._service(_HistoricalProvider())

        def malformed_holdings(*_args: object, **_kwargs: object) -> None:
            raise InvalidOperation("synthetic malformed legacy quantity")

        service._historical_holdings_evidence = malformed_holdings  # type: ignore[method-assign]
        preview = service.question_preview(self._request())
        self.assertFalse(preview["localOnly"])
        self.assertTrue(any(
            "could not normalize one or more holdings rows" in note
            for note in preview["documentScope"]["notes"]
        ))


if __name__ == "__main__":
    unittest.main()
