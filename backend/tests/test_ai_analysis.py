from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from group_claims_backend.services.ai import AIService, AnalysisSummary, AnswerResult, ProviderResult
from group_claims_backend.services.ai_analysis import AIAnalysisError, AIAnalysisService
from group_claims_backend.services.document_import import DeterministicDocumentParser
from group_claims_backend.storage.service import StorageService


class SyntheticProvider:
    def __init__(self, proposals, summary=None):
        self.proposals = tuple(proposals)
        self.summary = summary
        self.calls: list[str] = []

    def interpret(self, text: str) -> ProviderResult:
        self.calls.append(text)
        return ProviderResult(self.proposals, "gpt-4.1-mini", 4, 2, 6, self.summary)


class BlockingSyntheticProvider(SyntheticProvider):
    def __init__(self, proposals):
        super().__init__(proposals)
        self.started = threading.Event()
        self.release = threading.Event()

    def interpret(self, text: str) -> ProviderResult:
        self.calls.append(text)
        self.started.set()
        self.release.wait(timeout=2)
        return ProviderResult(self.proposals, "gpt-4.1-mini", 4, 2, 6)


class WithdrawingProvider(SyntheticProvider):
    def __init__(self, proposals, withdraw):
        super().__init__(proposals)
        self.withdraw = withdraw

    def interpret(self, text: str) -> ProviderResult:
        result = super().interpret(text)
        if len(self.calls) == 1:
            self.withdraw()
        return result


class QuestionProvider(SyntheticProvider):
    def __init__(self, proposals, answer="The document supports ISIN US0378331005.", citations=None):
        super().__init__(proposals)
        self.answer = answer
        self.citations = tuple(citations or ({"sourceReference": "CSV!A1", "evidenceSnippet": "US0378331005"},))
        self.questions: list[tuple[str, str]] = []
        self.citation_key_sets: list[tuple[str, ...]] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.questions.append((question, context))
        self.citation_key_sets.append(tuple(allowed_citation_keys))
        return AnswerResult(self.answer, False, self.citations, "gpt-4.1-mini", 9, 4, 13)


class ChangeSnapshotQuestionProvider(QuestionProvider):
    def __init__(self, proposals) -> None:
        super().__init__(proposals)
        self.saw_later_snapshot = False

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.saw_later_snapshot = (
            "[CSV!A300] Subfund" in context
            and "[CSV!B301] 100" in context
            and "[CSV!C301] 2023-03-01" in context
            and "[CSV!B302] 60" in context
            and "[CSV!C302] 2023-03-31" in context
        )
        assert self.saw_later_snapshot
        return AnswerResult(
            "Fund A is recorded with quantity 100 on 2023-03-01 and quantity 60 on 2023-03-31; this is a documented snapshot difference, not a conclusion about its legal cause.",
            False,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "Subfund"},),
            "gpt-4.1-mini", 9, 4, 13,
        )


class UnknownChangeSnapshotQuestionProvider(ChangeSnapshotQuestionProvider):
    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        assert "[CSV!A2] Subfund" in context
        assert "[CSV!B3] 100" in context
        return AnswerResult(
            None, True,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "Subfund"},),
            "gpt-4.1-mini", 9, 4, 13,
        )


class UnknownAssignmentQuestionProvider(QuestionProvider):
    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        assert "Does this document contain evidence of a legal-right assignment?" == question
        return AnswerResult(
            None, True,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "synthetic"},),
            "gpt-4.1-mini", 9, 4, 13,
        )


class UnknownGenericChangeQuestionProvider(QuestionProvider):
    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        return AnswerResult(
            None, True,
            ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "synthetic"},),
            "gpt-4.1-mini", 9, 4, 13,
        )


class EmptyChangeQuestionProvider(QuestionProvider):
    """Models a malformed-but-successful provider envelope with no answer."""

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        return AnswerResult(
            None, False,
            ( {"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "synthetic"}, ),
            "gpt-4.1-mini", 9, 4, 13,
        )


class AIAnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = StorageService(str(Path(self.temporary.name) / "app"))
        self.storage.initialize()
        self.previous = {key: os.environ.get(key) for key in (
            "OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER", "GROUP_CLAIMS_AI_MODEL",
            "GROUP_CLAIMS_AI_MAX_CHUNKS", "GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS",
            "GROUP_CLAIMS_AI_MAX_INPUT_TOKENS", "GROUP_CLAIMS_AI_MAX_OUTPUT_TOKENS",
            "GROUP_CLAIMS_AI_MAX_ESTIMATED_COST_USD", "GROUP_CLAIMS_AI_MAX_REQUEST_ATTEMPTS",
            "GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION", "GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION",
        )}
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        self.document_id = str(uuid.uuid4())
        self.extraction_id = str(uuid.uuid4())
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, "
                "file_size_bytes, imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.document_id, "synthetic.csv", f"documents/{self.document_id}", "a" * 64, 10, "2026-01-01T00:00:00+00:00", "csv", "parsed"),
            )
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, ?, 'parsed', ?, ?)",
                (self.extraction_id, self.document_id, "synthetic", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
            )
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'csv_cell', 'CSV', 1, 1, 1, ?, 'text', NULL, 0, 'row 1, column 1')",
                (str(uuid.uuid4()), self.extraction_id, "US0378331005"),
            )

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def proposal(self, **overrides):
        result = {
            "proposalType": "security", "fieldName": "isin", "value": "US0378331005",
            "unknown": False, "confidence": 0.8, "sourceDocumentId": self.document_id,
            "extractionId": self.extraction_id, "sourceReference": "CSV!A1",
            "evidenceSnippet": "US0378331005", "modelVersion": "gpt-4.1-mini",
        }
        result.update(overrides)
        return result

    def service(self, proposals, summary=None):
        self.provider = SyntheticProvider(proposals, summary)
        return AIAnalysisService(self.storage, provider_factory=lambda: self.provider)

    def authorize(self):
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})

    def add_csv_cell(self, reference: str, value: str, row: int, column: int) -> None:
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, "
                "column_number, worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'csv_cell', 'CSV', ?, ?, 1, ?, 'text', NULL, 0, ?)",
                (str(uuid.uuid4()), self.extraction_id, row, column, value, reference),
            )

    def add_document_extraction(self, filename: str, file_format: str) -> tuple[str, str]:
        document_id, extraction_id = str(uuid.uuid4()), str(uuid.uuid4())
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, "
                "file_size_bytes, imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed')",
                (document_id, filename, f"documents/{document_id}", uuid.uuid4().hex + uuid.uuid4().hex,
                 10, "2026-01-01T00:00:00+00:00", file_format),
            )
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, 'synthetic', 'parsed', ?, ?)",
                (extraction_id, document_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
            )
        return document_id, extraction_id

    def wait_for(self, service, job_id):
        for _ in range(100):
            status = service.job_status({"jobId": job_id})
            if status["status"] not in ("queued", "running"):
                return status
            time.sleep(0.01)
        self.fail("analysis job did not finish")

    def wait_for_question(self, service, question_id):
        for _ in range(100):
            status = service.question_status({"questionId": question_id})
            if status["status"] not in ("queued", "running"):
                return status
            time.sleep(0.01)
        self.fail("question did not finish")

    def test_question_requires_global_and_per_question_authorization(self):
        provider = QuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        with self.assertRaisesRegex(AIAnalysisError, "Enable cloud-processing"):
            service.ask_question({"documentId": self.document_id, "question": "What is the ISIN?", "acknowledged": True})
        self.authorize()
        with self.assertRaisesRegex(AIAnalysisError, "Confirm external processing"):
            service.ask_question({"documentId": self.document_id, "question": "What is the ISIN?"})
        self.assertEqual(provider.questions, [])

    def test_question_is_bounded_cited_and_not_persisted_as_domain_data(self):
        self.authorize()
        provider = QuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        preview = service.question_preview({"documentId": self.document_id, "question": "What is the ISIN?"})
        self.assertEqual(preview["scope"]["sourceItems"], 1)
        self.assertTrue(preview["cost"]["withinLimit"])
        self.assertEqual(preview["cost"]["maxOutputTokens"], 800)
        started = service.ask_question({"documentId": self.document_id, "question": "What is the ISIN?", "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["citations"][0]["sourceReference"], "CSV!A1")
        self.assertIn("US0378331005", provider.questions[0][1])
        self.assertNotIn("[analysis ", provider.questions[0][1])
        assert self.storage.database
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 0)

    def test_question_follow_up_uses_previous_turn_as_non_evidentiary_context(self):
        self.authorize()
        provider = QuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        follow_up = {
            "question": "Which ISIN is recorded?",
            "answer": "The document records ISIN US0378331005.",
        }
        preview = service.question_preview({
            "documentId": self.document_id,
            "question": "What source supports that?",
            "followUp": follow_up,
        })
        self.assertTrue(preview["cost"]["withinLimit"])
        started = service.ask_question({
            "documentId": self.document_id,
            "question": "What source supports that?",
            "followUp": follow_up,
            "acknowledged": True,
        })
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        context = provider.questions[0][1]
        self.assertIn("PREVIOUS CONVERSATION (context only; it is not evidence and cannot be cited)", context)
        self.assertIn("PREVIOUS QUESTION: Which ISIN is recorded?", context)
        self.assertIn("PREVIOUS ANSWER: The document records ISIN US0378331005.", context)
        self.assertEqual(status["citations"][0]["sourceReference"], "CSV!A1")

    def test_question_prompt_treats_billing_inputs_as_documented_or_missing(self):
        prompt = AIAnalysisService._question_prompt(
            "How should the pro-rata billing work after a transfer?", "[E1] refers to source [CSV!A1]", "", "CSV!A1 Fund A",
        )
        self.assertIn("approved fee pool", prompt)
        self.assertIn("entitlement or assignment evidence", prompt)
        self.assertIn("Do not calculate, allocate, or select a billing participant", prompt)

    def test_question_output_budget_respects_a_tighter_developer_limit(self):
        self.authorize()
        os.environ["GROUP_CLAIMS_AI_MAX_OUTPUT_TOKENS"] = "320"
        service = AIAnalysisService(self.storage, provider_factory=lambda: QuestionProvider([]))
        preview = service.question_preview({"documentId": self.document_id, "question": "What is the ISIN?"})
        self.assertEqual(preview["cost"]["maxOutputTokens"], 320)

    def test_change_question_retrieves_later_snapshot_rows_not_only_first_analysis_chunk(self):
        """Question retrieval must not lose a later holdings table to chunk order."""
        self.authorize()
        # Put the useful table after enough irrelevant cells to ensure that a
        # first analysis chunk alone cannot reliably carry it.
        for row in range(2, 142):
            self.add_csv_cell(f"CSV!A{row}", f"unrelated note {row}", row, 1)
        for reference, value, row, column in (
            ("CSV!A300", "Subfund", 300, 1),
            ("CSV!B300", "Quantity", 300, 2),
            ("CSV!C300", "As of date", 300, 3),
            ("CSV!A301", "Fund A", 301, 1),
            ("CSV!B301", "100", 301, 2),
            ("CSV!C301", "2023-03-01", 301, 3),
            ("CSV!A302", "Fund A", 302, 1),
            ("CSV!B302", "60", 302, 2),
            ("CSV!C302", "2023-03-31", 302, 3),
        ):
            self.add_csv_cell(reference, value, row, column)
        provider = ChangeSnapshotQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Are there any changes in the subfunds here?"
        preview = service.question_preview({"documentId": self.document_id, "question": question})
        self.assertGreaterEqual(preview["scope"]["sourceItems"], 9)
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertIn("100", str(status["answer"]))
        self.assertIn("60", str(status["answer"]))
        self.assertTrue(provider.saw_later_snapshot)

    def test_change_question_replaces_a_generic_unknown_with_local_snapshot_limitation(self):
        self.authorize()
        for reference, value, row, column in (
            ("CSV!A2", "Subfund", 2, 1),
            ("CSV!B2", "Quantity", 2, 2),
            ("CSV!A3", "Fund A", 3, 1),
            ("CSV!B3", "100", 3, 2),
        ):
            self.add_csv_cell(reference, value, row, column)
        provider = UnknownChangeSnapshotQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Tell me if there are any changes in the subfund."
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("does not itself show an explicit change event", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_change_event_question_replaces_a_generic_unknown_with_a_cited_no_event_result(self):
        self.authorize()
        # This source has no recognised change/event wording. The result must
        # still be a precise scoped negative answer, not the generic unknown
        # transcript message.
        provider = UnknownGenericChangeQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Does this document explicitly record a transfer, liquidation, redemption, or other change event?"
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("No explicit transfer, liquidation, redemption", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_change_event_question_replaces_an_empty_provider_answer_with_cited_local_result(self):
        """An empty answer marked non-unknown must never reach the generic UI branch."""
        self.authorize()
        provider = EmptyChangeQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Does this document explicitly record a transfer, liquidation, redemption, or other change event?"
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("No explicit transfer, liquidation, redemption", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_custodian_account_question_returns_a_cited_absence_when_not_extracted(self):
        """An absent custody field is a scoped, citable result—not a generic unknown."""
        self.authorize()
        for reference, value, row, column in (
            ("CSV!A2", "Client Name", 2, 1),
            ("CSV!B2", "Fee entity", 2, 2),
            ("CSV!A3", "Example claimant", 3, 1),
            ("CSV!B3", "Example fee entity", 3, 2),
        ):
            self.add_csv_cell(reference, value, row, column)
        provider = UnknownGenericChangeQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({
            "documentId": self.document_id,
            "question": "Which custodian accounts are mentioned?",
            "acknowledged": True,
        })
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("does not explicitly identify a custodian", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_structured_lookup_retrieves_later_spreadsheet_header_and_values(self):
        """A lookup must retrieve the matching table, not only analysis chunk one."""
        self.authorize()
        for row in range(2, 142):
            self.add_csv_cell(f"CSV!A{row}", f"unrelated note {row}", row, 1)
        for reference, value, row, column in (
            ("CSV!A300", "Account", 300, 1),
            ("CSV!B300", "ISIN", 300, 2),
            ("CSV!A301", "Custody account 123", 301, 1),
            ("CSV!B301", "US0378331005", 301, 2),
        ):
            self.add_csv_cell(reference, value, row, column)
        provider = QuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Which accounts and ISINs are recorded?"
        preview = service.question_preview({"documentId": self.document_id, "question": question})
        self.assertGreaterEqual(preview["scope"]["sourceItems"], 4)
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertIn("US0378331005", provider.questions[0][1])

    def test_german_transaction_terms_are_retrieved_for_change_questions(self):
        self.assertTrue(AIAnalysisService._is_change_source("Transaktionsdaten und Zuteilung"))
        self.assertTrue(AIAnalysisService._is_change_source("Rückzahlung und Abschreibung"))

    def test_assignment_question_returns_a_cited_negative_when_only_holdings_are_present(self):
        self.authorize()
        self.add_csv_cell("CSV!A2", "Account", 2, 1)
        self.add_csv_cell("CSV!B2", "Fund A holding", 2, 2)
        provider = UnknownAssignmentQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Does this document contain evidence of a legal-right assignment?"
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("No explicit assignment of legal claims or rights", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_assignment_question_returns_a_cited_possible_assignment_when_explicit_language_exists(self):
        self.authorize()
        self.add_csv_cell("CSV!A2", "Abtretung der Ansprüche an Fund B", 2, 1)
        provider = UnknownAssignmentQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        question = "Does this document contain evidence of a legal-right assignment?"
        started = service.ask_question({"documentId": self.document_id, "question": question, "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("contains express assignment-related language", str(status["answer"]))
        self.assertIn("possible rights-assignment observation", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_question_rejects_fabricated_citations_without_showing_an_answer(self):
        self.authorize()
        provider = QuestionProvider([], citations=({"sourceReference": "CSV!Z999", "evidenceSnippet": "fabricated"},))
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({"documentId": self.document_id, "question": "What is the ISIN?", "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["errorCode"], "AI_ANSWER_EVIDENCE_UNSUPPORTED")

    def test_question_accepts_only_the_legacy_internal_reference_marker(self):
        self.authorize()
        provider = QuestionProvider([], citations=({
            "sourceReference": "[analysis CSV!A1]",
            "evidenceSnippet": "US0378331005",
        },))
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({"documentId": self.document_id, "question": "What is the ISIN?", "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["citations"][0]["sourceReference"], "CSV!A1")

    def test_question_maps_a_schema_restricted_citation_key_to_local_evidence(self):
        self.authorize()
        provider = QuestionProvider([], citations=({
            "sourceReference": "E1", "evidenceSnippet": "US0378331005",
        },))
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({"documentId": self.document_id, "question": "What is the ISIN?", "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["citations"][0]["sourceReference"], "row 1, column 1")
        self.assertIn("E1", provider.citation_key_sets[0])
        self.assertIn("CITATION KEYS", provider.questions[0][1])

    def test_question_canonicalises_harmless_pdf_citation_formatting(self):
        self.authorize()
        document_id, extraction_id = self.add_document_extraction("synthetic.pdf", "pdf")
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            page_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO pdf_page_extractions (id, extraction_id, page_number, extraction_method, raw_text, cleaned_text, block_count) "
                "VALUES (?, ?, 1, 'native_text', ?, ?, 1)",
                (page_id, extraction_id, "Account 000123", "Account 000123"),
            )
            connection.execute(
                "INSERT INTO pdf_text_blocks (id, pdf_page_extraction_id, block_index, text, is_table_candidate) "
                "VALUES (?, ?, 3, 'Account 000123', 0)",
                (str(uuid.uuid4()), page_id),
            )
        provider = QuestionProvider([], citations=({
            "sourceReference": "`Page 1, Block 3`", "evidenceSnippet": "Account 000123",
        },))
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({"documentId": document_id, "question": "What account is identified?", "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["citations"][0]["sourceReference"], "page 1 block 3")

    def test_question_preserves_stored_spreadsheet_reference_casing(self):
        self.authorize()
        workbook_id, workbook_extraction = self.add_document_extraction("synthetic.xlsx", "xlsx")
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'spreadsheet_cell', 'Holdings', 2, 2, 1, 'US0378331005', 'text', NULL, 0, 'Holdings!B2')",
                (str(uuid.uuid4()), workbook_extraction),
            )
        provider = QuestionProvider([], citations=({
            "sourceReference": "holdings!b2", "evidenceSnippet": "US0378331005",
        },))
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({"documentId": workbook_id, "question": "What security is identified?", "acknowledged": True})
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["citations"][0]["sourceReference"], "Holdings!B2")

    def test_structured_spreadsheet_lookup_has_a_cited_local_fallback(self):
        """A declined model answer must not erase a plainly stored table row."""
        self.authorize()
        workbook_id, workbook_extraction = self.add_document_extraction("custody.xlsx", "xlsx")
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            for reference, row_number, column_number, value in (
                ("Holdings!A1", 1, 1, "Custodian account"),
                ("Holdings!B2", 2, 2, "CUST-001"),
            ):
                connection.execute(
                    "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                    "VALUES (?, ?, 'spreadsheet_cell', 'Holdings', ?, ?, 1, ?, 'text', NULL, 0, ?)",
                    (str(uuid.uuid4()), workbook_extraction, row_number, column_number, value, reference),
                )
        provider = UnknownGenericChangeQuestionProvider([])
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        started = service.ask_question({
            "documentId": workbook_id,
            "question": "Which custodian accounts are mentioned?",
            "acknowledged": True,
        })
        status = self.wait_for_question(service, started["questionId"])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["unknown"])
        self.assertIn("Custodian account", str(status["answer"]))
        self.assertTrue(status["citations"])

    def test_rejects_document_analysis_without_global_authorization(self):
        service = self.service([self.proposal()])
        with self.assertRaisesRegex(AIAnalysisError, "Enable cloud-processing"):
            service.start({"documentId": self.document_id, "acknowledged": True})
        self.assertEqual(self.provider.calls, [])

    def test_authorized_analysis_persists_only_evidence_validated_proposals(self):
        self.authorize()
        service = self.service([self.proposal()])
        preview = service.preview({"documentId": self.document_id})
        self.assertEqual(preview["scope"]["chunks"], 1)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["usage"]["inputTokens"], 4)
        self.assertEqual(status["usage"]["outputTokens"], 2)
        self.assertIsNotNone(status["usage"]["actualCostEstimateUsd"])
        findings = service.findings({"jobId": job["jobId"]})
        self.assertEqual(len(findings["findings"]), 1)
        self.assertEqual(findings["findings"][0]["validationStatus"], "validated")
        validation = status["coverage"]["validation"]
        self.assertEqual(validation["proposalsReturned"], 1)
        self.assertEqual(validation["schemaValid"], 1)
        self.assertEqual(validation["evidenceValidated"], 1)
        self.assertEqual(validation["persisted"], 1)
        self.assertEqual(validation["rejected"], 0)
        self.assertEqual(status["coverage"]["providerRequestStatus"], "completed")
        assert self.storage.database
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM holding_positions").fetchone()[0], 0)

    def test_completed_analysis_persists_only_an_evidence_linked_summary(self):
        self.authorize()
        summary = AnalysisSummary(
            "The analyzed source identifies ISIN US0378331005.",
            False,
            ({"sourceReference": "CSV!A1", "evidenceSnippet": "US0378331005"},),
        )
        service = self.service([self.proposal()], summary)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["summary"]["text"], "The analyzed source identifies ISIN US0378331005.")
        self.assertFalse(status["summary"]["unknown"])
        self.assertEqual(status["summary"]["citations"][0]["sourceReference"], "CSV!A1")

    def test_csv_a1_evidence_works_with_legacy_csv_provenance(self):
        self.authorize()
        service = self.service([self.proposal(sourceReference="CSV!A1")])
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["coverage"]["validation"]["persisted"], 1)

    def test_prompt_label_brackets_are_canonicalised_before_evidence_lookup(self):
        self.authorize()
        service = self.service([self.proposal(sourceReference=" [CSV!A1] ")])
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["coverage"]["validation"]["evidenceValidated"], 1)
        findings = service.findings({"jobId": job["jobId"]})["findings"]
        self.assertEqual(findings[0]["sourceReference"], "CSV!A1")

    def test_bracketed_fabricated_reference_remains_rejected(self):
        self.authorize()
        service = self.service([self.proposal(sourceReference="[CSV!Z999]")])
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["coverage"]["validation"]["persisted"], 0)
        self.assertEqual(
            status["coverage"]["validation"]["rejectedByReason"],
            {"source_reference_not_found": 1},
        )

    def test_validation_summary_distinguishes_accepted_and_rejected_proposals(self):
        self.authorize()
        self.add_csv_cell("CSV!B1", "US0000000000", 1, 2)
        proposals = [
            self.proposal(),
            self.proposal(sourceReference="CSV!Z999"),
            self.proposal(evidenceSnippet="not in this source"),
            self.proposal(value="US0000000000", sourceReference="CSV!B1", evidenceSnippet="US0000000000"),
            {"proposalType": "security"},
        ]
        service = self.service(proposals)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        validation = status["coverage"]["validation"]
        self.assertEqual(status["status"], "completed")
        self.assertEqual(validation["proposalsReturned"], 5)
        self.assertEqual(validation["schemaValid"], 4)
        self.assertEqual(validation["evidenceValidated"], 1)
        self.assertEqual(validation["persisted"], 1)
        self.assertEqual(validation["rejectedByReason"], {
            "evidence_snippet_mismatch": 1,
            "invalid_isin": 1,
            "schema_invalid": 1,
            "source_reference_not_found": 1,
        })

    def test_zero_proposal_response_is_distinct_from_rejected_proposals(self):
        self.authorize()
        service = self.service([])
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["coverage"]["validation"], {
            "proposalsReturned": 0, "schemaValid": 0, "evidenceValidated": 0,
            "persisted": 0, "rejected": 0, "rejectedByReason": {},
        })

    def test_multiple_field_level_evidence_references_are_persisted(self):
        self.authorize()
        self.add_csv_cell("CSV!B1", "Example Investor Ltd", 1, 2)
        self.add_csv_cell("CSV!C1", "Example Subfund", 1, 3)
        self.add_csv_cell("CSV!D1", "Account 000123", 1, 4)
        self.add_csv_cell("CSV!E1", "1,000", 1, 5)
        self.add_csv_cell("CSV!F1", "2024-01-31", 1, 6)
        proposals = [
            self.proposal(proposalType="investor_legal_entity", fieldName="name", value="Example Investor Ltd", sourceReference="CSV!B1", evidenceSnippet="Example Investor Ltd"),
            self.proposal(proposalType="investment_vehicle_subfund", fieldName="name", value="Example Subfund", sourceReference="CSV!C1", evidenceSnippet="Example Subfund"),
            self.proposal(proposalType="account", fieldName="account_identifier", value="000123", sourceReference="CSV!D1", evidenceSnippet="Account 000123"),
            self.proposal(proposalType="holding_position", fieldName="quantity", value=1000, sourceReference="CSV!E1", evidenceSnippet="1,000"),
            self.proposal(proposalType="holding_position", fieldName="as_of_date", value="2024-01-31", sourceReference="CSV!F1", evidenceSnippet="2024-01-31"),
        ]
        service = self.service(proposals)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["coverage"]["validation"]["persisted"], 5)
        self.assertEqual(len(service.findings({"jobId": job["jobId"]})["findings"]), 5)

    def test_csv_evidence_supports_facts_and_explicit_unknowns_without_domain_writes(self):
        self.authorize()
        self.add_csv_cell("CSV!B1", "Example Investor Ltd", 1, 2)
        self.add_csv_cell("CSV!C1", "Example Subfund", 1, 3)
        self.add_csv_cell("CSV!D1", "Account 000123", 1, 4)
        self.add_csv_cell("CSV!E1", "1,000", 1, 5)
        self.add_csv_cell("CSV!F1", "2024-01-31", 1, 6)
        self.add_csv_cell("CSV!G1", "Claimant not stated", 1, 7)
        self.add_csv_cell("CSV!H1", "Invoice recipient not stated", 1, 8)
        proposals = [
            self.proposal(proposalType="investor_legal_entity", fieldName="name", value="Example Investor Ltd", sourceReference="CSV!B1", evidenceSnippet="Example Investor Ltd"),
            self.proposal(proposalType="investment_vehicle_subfund", fieldName="name", value="Example Subfund", sourceReference="CSV!C1", evidenceSnippet="Example Subfund"),
            self.proposal(proposalType="account", fieldName="account_identifier", value="000123", sourceReference="CSV!D1", evidenceSnippet="Account 000123"),
            self.proposal(proposalType="holding_position", fieldName="quantity", value=1000, sourceReference="CSV!E1", evidenceSnippet="1,000"),
            self.proposal(proposalType="holding_position", fieldName="as_of_date", value="2024-01-31", sourceReference="CSV!F1", evidenceSnippet="2024-01-31"),
            self.proposal(proposalType="legal_claim_right", fieldName="claimant", value=None, unknown=True, confidence=None, sourceReference="CSV!G1", evidenceSnippet=None),
            self.proposal(proposalType="possible_invoice_recipient", fieldName="recipient", value=None, unknown=True, confidence=None, sourceReference="CSV!H1", evidenceSnippet=None),
        ]
        service = self.service(proposals)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["coverage"]["validation"]["persisted"], 7)
        assert self.storage.database
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM legal_entities").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoice_recipients").fetchone()[0], 0)

    def test_xlsx_and_pdf_source_references_validate_against_their_stored_provenance(self):
        self.authorize()
        workbook_id, workbook_extraction = self.add_document_extraction("synthetic.xlsx", "xlsx")
        pdf_id, pdf_extraction = self.add_document_extraction("synthetic.pdf", "pdf")
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'spreadsheet_cell', 'Holdings', 2, 2, 1, 'US0378331005', 'text', NULL, 0, 'Holdings!B2')",
                (str(uuid.uuid4()), workbook_extraction),
            )
            page_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO pdf_page_extractions (id, extraction_id, page_number, extraction_method, raw_text, cleaned_text, block_count) VALUES (?, ?, 1, 'native_text', ?, ?, 1)",
                (page_id, pdf_extraction, "Account 000123", "Account 000123"),
            )
            connection.execute(
                "INSERT INTO pdf_text_blocks (id, pdf_page_extraction_id, block_index, text, is_table_candidate) VALUES (?, ?, 0, 'Account 000123', 0)",
                (str(uuid.uuid4()), page_id),
            )
        workbook_service = self.service([self.proposal(sourceDocumentId=workbook_id, extractionId=workbook_extraction, sourceReference="[Holdings!B2]", evidenceSnippet="US0378331005")])
        workbook_job = workbook_service.start({"documentId": workbook_id, "acknowledged": True})
        self.assertEqual(self.wait_for(workbook_service, workbook_job["jobId"])["coverage"]["validation"]["persisted"], 1)
        pdf_service = self.service([self.proposal(proposalType="account", fieldName="account_identifier", value="000123", sourceDocumentId=pdf_id, extractionId=pdf_extraction, sourceReference="[page 1 block 0]", evidenceSnippet="Account 000123")])
        pdf_job = pdf_service.start({"documentId": pdf_id, "acknowledged": True})
        self.assertEqual(self.wait_for(pdf_service, pdf_job["jobId"])["coverage"]["validation"]["persisted"], 1)

    def test_cost_limit_blocks_transmission_before_a_job_is_created(self):
        self.authorize()
        os.environ["GROUP_CLAIMS_AI_MAX_ESTIMATED_COST_USD"] = "0.0001"
        service = self.service([self.proposal()])
        preview = service.preview({"documentId": self.document_id})
        self.assertFalse(preview["cost"]["withinLimit"])
        with self.assertRaisesRegex(AIAnalysisError, "estimated API cost"):
            service.start({"documentId": self.document_id, "acknowledged": True})
        self.assertEqual(self.provider.calls, [])
        assert self.storage.database
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_processing_jobs").fetchone()[0], 0)

    def test_unknown_model_requires_explicit_pricing_before_transmission(self):
        self.authorize()
        os.environ["GROUP_CLAIMS_AI_MODEL"] = "unpriced-test-model"
        service = self.service([self.proposal()])
        preview = service.preview({"documentId": self.document_id})
        self.assertIsNone(preview["cost"]["estimatedCostUsd"])
        self.assertFalse(preview["cost"]["withinLimit"])
        with self.assertRaisesRegex(AIAnalysisError, "pricing profile"):
            service.start({"documentId": self.document_id, "acknowledged": True})
        self.assertEqual(self.provider.calls, [])

    def test_default_budget_uses_one_request_and_reports_excluded_coverage(self):
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'csv_cell', 'CSV', 2, 1, 1, ?, 'text', NULL, 0, 'CSV!A2')",
                (str(uuid.uuid4()), self.extraction_id, "x" * 4_000),
            )
        service = self.service([self.proposal()])
        preview = service.preview({"documentId": self.document_id})
        self.assertEqual(preview["scope"]["chunks"], 1)
        self.assertGreater(preview["scope"]["excludedSourceItems"], 0)
        self.assertTrue(preview["scope"]["truncated"])
        self.assertLessEqual(preview["cost"]["estimatedInputTokens"], 1_500)
        self.assertEqual(preview["cost"]["maxOutputTokens"], 1_000)
        self.assertEqual(preview["cost"]["maxRequestAttempts"], 1)

    def test_completed_results_reopen_without_another_provider_call(self):
        self.authorize()
        service = self.service([self.proposal()])
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        self.assertEqual(self.wait_for(service, job["jobId"])["status"], "completed")
        self.assertEqual(len(self.provider.calls), 1)
        reopened = service.preview({"documentId": self.document_id})
        self.assertEqual(reopened["latestJob"]["jobId"], job["jobId"])
        self.assertEqual(reopened["latestJob"]["status"], "completed")
        self.assertEqual(len(self.provider.calls), 1)

    def test_fabricated_evidence_and_invalid_isins_are_not_persisted(self):
        self.authorize()
        service = self.service([
            self.proposal(sourceReference="CSV!Z999"),
            self.proposal(value="US0000000000"),
        ])
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(service.findings({"jobId": job["jobId"]})["findings"], [])
        self.assertEqual(status["coverage"]["discardedUnsupportedEvidence"], 2)

    def test_document_authorization_notice_must_be_acknowledged(self):
        self.authorize()
        service = self.service([self.proposal()])
        with self.assertRaisesRegex(AIAnalysisError, "Confirm"):
            service.start({"documentId": self.document_id})

    def test_prevents_duplicate_concurrent_jobs_for_the_same_extraction(self):
        self.authorize()
        self.provider = BlockingSyntheticProvider([self.proposal()])
        service = AIAnalysisService(self.storage, provider_factory=lambda: self.provider)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        self.assertTrue(self.provider.started.wait(timeout=1))
        with self.assertRaisesRegex(AIAnalysisError, "already has an analysis"):
            service.start({"documentId": self.document_id, "acknowledged": True})
        self.provider.release.set()
        self.assertEqual(self.wait_for(service, job["jobId"])["status"], "completed")

    def test_withdrawal_blocks_the_next_chunk_and_records_a_partial_failure(self):
        self.authorize()
        os.environ["GROUP_CLAIMS_AI_MAX_CHUNKS"] = "2"
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'csv_cell', 'CSV', 2, 1, 1, ?, 'text', NULL, 0, 'CSV!A2')",
                (str(uuid.uuid4()), self.extraction_id, "x" * 5_000),
            )
        provider = WithdrawingProvider(
            [self.proposal()],
            lambda: AIService(self.storage).set_authorization({"authorized": False, "acknowledged": True}),
        )
        service = AIAnalysisService(self.storage, provider_factory=lambda: provider)
        job = service.start({"documentId": self.document_id, "acknowledged": True})
        status = self.wait_for(service, job["jobId"])
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["errorCode"], "AI_AUTHORIZATION_WITHDRAWN")
        self.assertEqual(status["coverage"]["providerRequestStatus"], "failed")
        self.assertEqual(status["coverage"]["chunksProcessed"], 1)
        self.assertEqual(len(provider.calls), 1)

    def test_coverage_is_not_truncated_merely_for_reaching_the_chunk_cap(self):
        lines = ["x" * 4_999 for _ in range(8)]
        os.environ["GROUP_CLAIMS_AI_MAX_CHUNKS"] = "8"
        service = self.service([self.proposal()])
        plan = service._split_chunks(
            lines,
            total_source_items=8,
            content_character_limit=4_999,
            document_id=self.document_id,
            extraction_id=self.extraction_id,
        )
        self.assertEqual(len(plan.chunks), 8)
        self.assertFalse(plan.truncated)


class CsvProvenanceTests(unittest.TestCase):
    def test_csv_parser_uses_grid_compatible_a1_references(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.csv"
            source.write_text("Account,Quantity\n000123,1000\n", encoding="utf-8")
            result = DeterministicDocumentParser().parse(source, "csv")
        self.assertEqual([item.source_reference for item in result.items], [
            "CSV!A1", "CSV!B1", "CSV!A2", "CSV!B2",
        ])
        self.assertTrue(all(item.sheet_name == "CSV" for item in result.items))
