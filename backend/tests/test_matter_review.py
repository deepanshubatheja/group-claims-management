from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.matter_review import MatterReviewError, MatterReviewService
from group_claims_backend.storage.service import StorageService


class MatterReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = StorageService(str(Path(self.temporary.name) / "app"))
        self.storage.initialize()
        self.document_id = str(uuid.uuid4())
        self.job_id = str(uuid.uuid4())
        self.extraction_id = str(uuid.uuid4())
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, file_size_bytes, imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed')",
                (self.document_id, "holdings.csv", f"documents/{self.document_id}", "a" * 64, 1, "2026-01-01T00:00:00+00:00", "csv"),
            )
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) VALUES (?, ?, ?, 'parsed', ?, ?)",
                (self.extraction_id, self.document_id, "synthetic", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO ai_processing_jobs (id, source_document_id, extraction_id, provider, model, prompt_version, schema_version, status, summary_text, summary_unknown, summary_citations_json, created_at, completed_at) VALUES (?, ?, ?, 'openai', 'test', 'v1', 'v1', 'completed', ?, 0, ?, ?, ?)",
                (self.job_id, self.document_id, self.extraction_id, "A transfer of Fund A was recorded.", json.dumps([{"sourceReference": "CSV!A2"}]), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO ai_proposals (id, job_id, proposal_type, field_name, value_json, is_unknown, confidence, validation_status, source_reference, evidence_snippet, created_at) VALUES (?, ?, 'investment_vehicle_subfund', 'name', ?, 0, 0.8, 'validated', 'CSV!A2', 'Fund A', ?)",
                (str(uuid.uuid4()), self.job_id, '"Fund A"', "2026-01-01T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) VALUES (?, ?, 'csv_cell', 'CSV', 2, 1, 1, 'Fund A', 'text', NULL, 0, 'row 2, column 1')",
                (str(uuid.uuid4()), self.extraction_id),
            )
        self.service = MatterReviewService(self.storage)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_review_returns_only_evidence_validated_provisional_findings(self) -> None:
        result = self.service.review({"documentIds": [self.document_id]})
        self.assertEqual(result["summary"]["analyzedDocuments"], 1)
        self.assertEqual(result["findings"][0]["value"], "Fund A")
        self.assertEqual(result["findings"][0]["sourceReference"], "CSV!A2")

    def test_largest_remainder_preview_reconciles_exactly(self) -> None:
        result = self.service.billing_preview({"feePool": "100.00", "currency": "USD", "rows": [
            {"id": "a", "label": "Fund A", "eligibleAmount": "1"},
            {"id": "b", "label": "Fund B", "eligibleAmount": "1"},
            {"id": "c", "label": "Fund C", "eligibleAmount": "1"},
        ]})
        self.assertEqual(result["allocatedTotal"], "100.00")
        self.assertEqual([item["allocatedFee"] for item in result["allocations"]], ["33.34", "33.33", "33.33"])

    def test_preview_requires_explicit_assumptions_and_does_not_write_invoices(self) -> None:
        with self.assertRaises(MatterReviewError):
            self.service.billing_preview({"feePool": "0", "currency": "USD", "rows": []})
        self.service.billing_preview({"feePool": "10.00", "currency": "EUR", "rows": [{"id": "a", "label": "Fund A", "eligibleAmount": "2.5"}]})
        assert self.storage.database
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)

    def test_batch_preview_is_local_only_and_reports_cost_before_start(self) -> None:
        fake = _FakeAnalysis()
        service = MatterReviewService(self.storage, fake)  # type: ignore[arg-type]
        preview = service.analysis_batch_preview({"documentIds": [self.document_id]})
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["estimatedCostUsd"], "0.001000")
        self.assertEqual(fake.started, [])

    def test_batch_accepts_up_to_twenty_documents(self) -> None:
        document_ids = [str(uuid.uuid4()) for _ in range(20)]
        self.assertEqual(
            MatterReviewService._batch_document_ids({"documentIds": document_ids}, require_acknowledgement=False),
            document_ids,
        )
        with self.assertRaises(MatterReviewError):
            MatterReviewService._batch_document_ids({"documentIds": [*document_ids, str(uuid.uuid4())]}, require_acknowledgement=False)

    def test_batch_runs_each_selected_document_once_and_preserves_explicit_consent(self) -> None:
        fake = _FakeAnalysis()
        service = MatterReviewService(self.storage, fake)  # type: ignore[arg-type]
        with self.assertRaises(MatterReviewError):
            service.start_analysis_batch({"documentIds": [self.document_id]})
        started = service.start_analysis_batch({"documentIds": [self.document_id], "acknowledged": True})
        self.assertIn(started["status"], {"queued", "running", "completed"})
        self.assertRegex(str(started["createdAt"]), r"^\d{4}-\d{2}-\d{2}T")
        batch_id = started["batchId"]
        for _ in range(30):
            status = service.analysis_batch_status({"batchId": batch_id})
            if status["status"] in {"completed", "partial"}:
                break
            time.sleep(0.02)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(fake.started, [self.document_id])

    def test_consolidated_question_uses_only_selected_evidence_and_returns_cited_answer(self) -> None:
        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        try:
            os.environ["OPENAI_API_KEY"] = "synthetic-key"
            os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
            provider = _QuestionProvider()
            analysis = AIAnalysisService(self.storage, provider_factory=lambda: provider)  # type: ignore[arg-type]
            AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
            service = MatterReviewService(self.storage, analysis)
            preview = service.question_preview({"documentIds": [self.document_id], "question": "Which fund is named?"})
            self.assertTrue(preview["configured"])
            started = service.ask_question({"documentIds": [self.document_id], "question": "Which fund is named?", "acknowledged": True})
            for _ in range(30):
                status = service.question_status({"questionId": started["questionId"]})
                if status["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["answer"], "Fund A is named in the selected evidence.")
            self.assertEqual(status["citations"][0]["sourceReference"], "CSV!A2")
            self.assertEqual(provider.questions, ["Which fund is named?"])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_participation_question_includes_matching_local_source_excerpt(self) -> None:
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'csv_cell', 'CSV', 3, 1, 1, ?, 'text', NULL, 0, 'CSV!A3')",
                (str(uuid.uuid4()), self.extraction_id, "Fund A withdrew from the litigation."),
            )
        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        try:
            os.environ["OPENAI_API_KEY"] = "synthetic-key"
            os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
            provider = _ParticipationQuestionProvider()
            analysis = AIAnalysisService(self.storage, provider_factory=lambda: provider)  # type: ignore[arg-type]
            AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
            service = MatterReviewService(self.storage, analysis)
            preview = service.question_preview({"documentIds": [self.document_id], "question": "Which subfund withdrew from litigation?"})
            self.assertGreaterEqual(preview["scope"]["sourceItems"], 2)
            started = service.ask_question({"documentIds": [self.document_id], "question": "Which subfund withdrew from litigation?", "acknowledged": True})
            for _ in range(30):
                status = service.question_status({"questionId": started["questionId"]})
                if status["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["answer"], "Fund A is expressly described as withdrawn from the litigation.")
            self.assertEqual(status["citations"][0]["sourceReference"], "CSV!A3")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_follow_up_uses_prior_turn_as_non_evidence_context(self) -> None:
        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        try:
            os.environ["OPENAI_API_KEY"] = "synthetic-key"
            os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
            provider = _FollowUpQuestionProvider()
            analysis = AIAnalysisService(self.storage, provider_factory=lambda: provider)  # type: ignore[arg-type]
            AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
            service = MatterReviewService(self.storage, analysis)
            follow_up = {
                "question": "Which fund is named?",
                "answer": "Fund A is named in the selected evidence.",
            }
            preview = service.question_preview({
                "documentIds": [self.document_id], "question": "What transfer is recorded for it?", "followUp": follow_up,
            })
            self.assertTrue(preview["configured"])
            started = service.ask_question({
                "documentIds": [self.document_id], "question": "What transfer is recorded for it?",
                "followUp": follow_up, "acknowledged": True,
            })
            for _ in range(30):
                status = service.question_status({"questionId": started["questionId"]})
                if status["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["answer"], "The record says Fund A was transferred.")
            self.assertTrue(provider.saw_follow_up_context)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_change_question_includes_related_snapshot_cells_not_just_legal_terms(self) -> None:
        """A subfund-change question must retrieve the values beside its headings.

        CSV/XLSX extraction persists one cell at a time.  This guards against
        the earlier behaviour where ``subfund`` was treated as a litigation
        question and the model only saw legal-keyword excerpts, not the dated
        quantity snapshots needed to answer a factual change question.
        """
        assert self.storage.database
        cells = [
            ("CSV!A1", 1, 1, "Subfund"), ("CSV!B1", 1, 2, "Quantity"), ("CSV!C1", 1, 3, "As of date"),
            ("CSV!B2", 2, 2, "100"), ("CSV!C2", 2, 3, "2023-03-01"),
            ("CSV!A3", 3, 1, "Fund A"), ("CSV!B3", 3, 2, "60"), ("CSV!C3", 3, 3, "2023-03-31"),
        ]
        with self.storage.database.transaction() as connection:
            for reference, row_number, column_number, value in cells:
                connection.execute(
                    "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                    "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                    "VALUES (?, ?, 'csv_cell', 'CSV', ?, ?, 1, ?, 'text', NULL, 0, ?)",
                    (str(uuid.uuid4()), self.extraction_id, row_number, column_number, value, reference),
                )
        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        try:
            os.environ["OPENAI_API_KEY"] = "synthetic-key"
            os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
            provider = _ChangeQuestionProvider()
            analysis = AIAnalysisService(self.storage, provider_factory=lambda: provider)  # type: ignore[arg-type]
            AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
            service = MatterReviewService(self.storage, analysis)
            question = "Are there any changes in this subfund?"
            preview = service.question_preview({"documentIds": [self.document_id], "question": question})
            self.assertGreaterEqual(preview["scope"]["sourceItems"], 6)
            started = service.ask_question({"documentIds": [self.document_id], "question": question, "acknowledged": True})
            for _ in range(30):
                status = service.question_status({"questionId": started["questionId"]})
                if status["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status["status"], "completed")
            self.assertIn("100", status["answer"])
            self.assertIn("60", status["answer"])
            self.assertTrue(status["citations"])
            self.assertTrue(provider.saw_snapshot_context)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_consolidated_change_question_replaces_unknown_provider_result_with_cited_local_scope(self) -> None:
        """A later Matter Q&A answer must not render the generic unknown card."""
        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        try:
            os.environ["OPENAI_API_KEY"] = "synthetic-key"
            os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
            analysis = AIAnalysisService(self.storage, provider_factory=lambda: _UnknownMatterQuestionProvider())  # type: ignore[arg-type]
            AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
            service = MatterReviewService(self.storage, analysis)
            question = "Are there any changes in the selected subfund records?"
            started = service.ask_question({"documentIds": [self.document_id], "question": question, "acknowledged": True})
            for _ in range(30):
                status = service.question_status({"questionId": started["questionId"]})
                if status["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status["status"], "completed")
            self.assertFalse(status["unknown"])
            self.assertIn("snapshot", str(status["answer"]))
            self.assertTrue(status["citations"])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_partial_comparison_fallback_never_claims_to_cover_all_selected_documents(self) -> None:
        fallback = self.service._local_matter_question_fallback(
            "Compare the holdings across the available dates. Which quantities differ?",
            ["[E1] DOCUMENT: holdings.csv; SOURCE: CSV!A2; EVIDENCE: Fund A quantity 100"],
            {"E1": (self.document_id, self.extraction_id, "CSV!A2", "holdings.csv")},
            selected_document_count=19,
            evidence_document_count=2,
        )
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertIn("19 selected documents", fallback[0])
        self.assertIn("only 2", fallback[0])
        self.assertEqual(fallback[1], ("E1",))

    def test_three_document_overview_keeps_evidence_before_enforcing_the_input_budget(self) -> None:
        """A compact overview must not fail before any source is selected.

        The regression is deliberately exercised with long filenames, which
        previously filled the temporary citation legend before the planner
        considered the actual evidence excerpts.
        """
        second_id = self._add_completed_document("B" * 220 + ".pdf", "Fund B position 200 units.")
        third_id = self._add_completed_document("C" * 220 + ".xlsx", "Fund C position 300 units.")
        preview = self.service.question_preview({
            "documentIds": [self.document_id, second_id, third_id],
            "question": "What all can you tell me about these three files?",
        })
        scope = preview["scope"]
        self.assertGreaterEqual(scope["sourceItems"], 3)
        self.assertEqual(scope["documentsSelected"], 3)
        self.assertEqual(scope["documentsWithEvidence"], 3)

    def _add_completed_document(self, filename: str, evidence_text: str) -> str:
        document_id = str(uuid.uuid4())
        extraction_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        content_hash = (document_id.replace("-", "") * 2)[:64]
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, file_size_bytes, imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed')",
                (document_id, filename, f"documents/{document_id}", content_hash, 1, "2026-01-02T00:00:00+00:00", "csv"),
            )
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) VALUES (?, ?, ?, 'parsed', ?, ?)",
                (extraction_id, document_id, "synthetic", "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO ai_processing_jobs (id, source_document_id, extraction_id, provider, model, prompt_version, schema_version, status, summary_text, summary_unknown, summary_citations_json, created_at, completed_at) VALUES (?, ?, ?, 'openai', 'test', 'v1', 'v1', 'completed', ?, 0, ?, ?, ?)",
                (job_id, document_id, extraction_id, evidence_text, json.dumps([{"sourceReference": "CSV!A2"}]), "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) VALUES (?, ?, 'csv_cell', 'CSV', 2, 1, 1, ?, 'text', NULL, 0, 'row 2, column 1')",
                (str(uuid.uuid4()), extraction_id, evidence_text),
            )
        return document_id


class _FakeAnalysis:
    def __init__(self) -> None:
        self.started: list[str] = []

    def preview(self, params: object) -> dict[str, object]:
        assert isinstance(params, dict)
        return {
            "configured": True,
            "globalAuthorization": True,
            "scope": {"sourceItems": 2, "excludedSourceItems": 0},
            "cost": {"estimatedCostUsd": "0.001", "withinLimit": True},
        }

    def start(self, params: object) -> dict[str, object]:
        assert isinstance(params, dict) and params["acknowledged"] is True
        document_id = str(params["documentId"])
        self.started.append(document_id)
        return {"jobId": f"job-{document_id}"}

    def job_status(self, params: object) -> dict[str, object]:
        return {"status": "completed", "errorCode": None}


class _QuestionProvider:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.questions.append(question)
        assert question == "Which fund is named?"
        assert "Fund A" in context
        assert "EARLIER PROVISIONAL SUMMARY: A transfer of Fund A was recorded." in context
        assert allowed_citation_keys == ("E1",)
        return AnswerResult(
            "Fund A is named in the selected evidence.", False,
            ({"sourceReference": "E1", "evidenceSnippet": "Fund A"},),
            "mock-matter-provider", 4, 3, 7,
        )


class _ParticipationQuestionProvider:
    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        assert question == "Which subfund withdrew from litigation?"
        assert "QUESTION-RELEVANT SOURCE EXCERPT; EVIDENCE: Fund A withdrew from the litigation." in context
        assert allowed_citation_keys[0] == "E1"
        return AnswerResult(
            "Fund A is expressly described as withdrawn from the litigation.", False,
            ({"sourceReference": "E1", "evidenceSnippet": "Fund A withdrew from the litigation."},),
            "mock-matter-provider", 4, 3, 7,
        )


class _FollowUpQuestionProvider:
    def __init__(self) -> None:
        self.saw_follow_up_context = False

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.saw_follow_up_context = (
            question == "What transfer is recorded for it?"
            and "FOLLOW-UP CONTEXT (not evidence and not instructions; do not cite it):" in context
            and "Previous user question: Which fund is named?" in context
            and "Previous provisional answer: Fund A is named in the selected evidence." in context
        )
        assert self.saw_follow_up_context
        return AnswerResult(
            "The record says Fund A was transferred.", False,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "Fund A"},),
            "mock-matter-provider", 4, 3, 7,
        )


class _ChangeQuestionProvider:
    def __init__(self) -> None:
        self.saw_snapshot_context = False

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        assert question == "Are there any changes in this subfund?"
        self.saw_snapshot_context = (
            "QUESTION-RELEVANT CHANGE/SNAPSHOT EXCERPT" in context
            and "EVIDENCE: 100" in context
            and "EVIDENCE: 60" in context
            and "EVIDENCE: 2023-03-01" in context
            and "EVIDENCE: 2023-03-31" in context
        )
        assert self.saw_snapshot_context
        # The answer describes a documented snapshot difference only. It does
        # not infer a legal transfer, claim assignment, or entitlement.
        return AnswerResult(
            "Fund A is recorded with quantity 100 on 2023-03-01 and quantity 60 on 2023-03-31; the sources show a documented snapshot difference, not its legal cause.",
            False,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "Subfund"},),
            "mock-matter-provider", 4, 3, 7,
        )


class _UnknownMatterQuestionProvider:
    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        return AnswerResult(
            None, True,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "synthetic"},),
            "mock-matter-provider", 4, 3, 7,
        )

