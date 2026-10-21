"""Regression tests for explicitly scoped Matter Review questions.

All fixtures are synthetic and every provider is a local mock: no test sends
content to OpenAI or Codex.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.matter_review import (
    MAX_QUESTION_CITATION_SOURCES,
    MatterReviewError,
    MatterReviewService,
)
from group_claims_backend.storage.service import StorageService

_ENVIRONMENT = (
    "OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER", "GROUP_CLAIMS_AI_MAX_ESTIMATED_COST_USD",
    "GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION", "GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION",
)


class _RecordingProvider:
    """Records every provider request and cites the first allowed key(s)."""

    def __init__(self, answer: str = "Synthetic cited answer.", citation_count: int = 1) -> None:
        self.answer = answer
        self.citation_count = citation_count
        self.contexts: list[str] = []
        self.questions: list[str] = []
        self.allowed_keys: list[tuple[str, ...]] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.questions.append(question)
        self.contexts.append(context)
        self.allowed_keys.append(tuple(allowed_citation_keys))
        return AnswerResult(
            self.answer, False,
            tuple({"sourceReference": key, "evidenceSnippet": "synthetic"} for key in allowed_citation_keys[:self.citation_count]),
            "mock-scope-provider", 10, 5, 15,
        )


class _ConflictProvider(_RecordingProvider):
    """Cites one excerpt from each of two documents, as the prompt requires for a conflict."""

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        super().answer_question(question, context, allowed_citation_keys)
        keys_by_document: dict[str, str] = {}
        for line in context.splitlines():
            if line.startswith("[E") and "DOCUMENT:" in line:
                key = line[1:line.index("]")]
                document = line.split("DOCUMENT: ", 1)[1].split(";", 1)[0]
                keys_by_document.setdefault(document, key)
        return AnswerResult(
            "The two custody statements disagree about the quantity held.", False,
            tuple({"sourceReference": key, "evidenceSnippet": "quantity"} for key in list(keys_by_document.values())[:2]),
            "mock-conflict-provider", 10, 5, 15,
        )


class MatterQuestionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self._previous_environment = {key: os.environ.get(key) for key in _ENVIRONMENT}
        os.environ["OPENAI_API_KEY"] = "synthetic-key"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        for key in _ENVIRONMENT[2:]:
            os.environ.pop(key, None)
        self.storage = StorageService(str(Path(self.temporary.name) / "app"))
        self.storage.initialize()
        self.provider = _RecordingProvider()
        self._use_provider(self.provider)

    def tearDown(self) -> None:
        for key, value in self._previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    # -- fixtures -------------------------------------------------------

    def _use_provider(self, provider: _RecordingProvider) -> None:
        self.provider = provider
        analysis = AIAnalysisService(self.storage, provider_factory=lambda: provider)  # type: ignore[arg-type]
        self.service = MatterReviewService(self.storage, analysis)

    def _authorize_cloud(self, authorized: bool = True) -> None:
        AIService(self.storage).set_authorization({"authorized": authorized, "acknowledged": True})

    def _add_document(
        self, filename: str, cells: list[str] | None = None, *, status: str = "parsed",
        pdf_blocks: list[str] | None = None, unavailable_pdf_page: bool = False,
    ) -> str:
        """Insert one synthetic parsed document with CSV cells or PDF blocks."""
        document_id = str(uuid.uuid4())
        extraction_id = str(uuid.uuid4())
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, "
                "file_size_bytes, imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, filename, f"documents/{document_id}", (document_id.replace("-", "") * 2)[:64], 1,
                 "2026-01-01T00:00:00+00:00", "pdf" if pdf_blocks is not None else "csv", status),
            )
            if status != "parsed":
                return document_id
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, 'synthetic', 'parsed', ?, ?)",
                (extraction_id, document_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            for row_number, value in enumerate(cells or [], start=1):
                connection.execute(
                    "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                    "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                    "VALUES (?, ?, 'csv_cell', 'CSV', ?, 1, 1, ?, 'text', NULL, 0, ?)",
                    (str(uuid.uuid4()), extraction_id, row_number, value, f"CSV!A{row_number}"),
                )
            if pdf_blocks is not None or unavailable_pdf_page:
                page_id = str(uuid.uuid4())
                text = "\n".join(pdf_blocks or [])
                connection.execute(
                    "INSERT INTO pdf_page_extractions (id, extraction_id, page_number, extraction_method, raw_text, "
                    "cleaned_text, block_count) VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (page_id, extraction_id, "unavailable" if not pdf_blocks else "native_text", text, text, len(pdf_blocks or [])),
                )
                for index, block in enumerate(pdf_blocks or []):
                    connection.execute(
                        "INSERT INTO pdf_text_blocks (id, pdf_page_extraction_id, block_index, text, is_table_candidate) "
                        "VALUES (?, ?, ?, ?, 0)",
                        (str(uuid.uuid4()), page_id, index, block),
                    )
                if unavailable_pdf_page and pdf_blocks:
                    connection.execute(
                        "INSERT INTO pdf_page_extractions (id, extraction_id, page_number, extraction_method, block_count) "
                        "VALUES (?, ?, 2, 'unavailable', 0)",
                        (str(uuid.uuid4()), extraction_id),
                    )
        return document_id

    @staticmethod
    def _request(question: str, *, mode: str = "all", document_ids: list[str] | None = None, **extra: object) -> dict[str, object]:
        scope: dict[str, object] = {"mode": mode}
        if document_ids is not None:
            scope["documentIds"] = document_ids
        return {"matterId": "workspace", "scope": scope, "question": question, **extra}

    def _ask(self, request: dict[str, object], consented: list[str] | None = None) -> dict[str, object]:
        if consented is None:
            preview = self.service.question_preview(request)
            consented = [item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]]
        started = self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": consented})
        for _ in range(50):
            status = self.service.question_status({"questionId": started["questionId"]})
            if status["status"] in {"completed", "failed"}:
                return dict(status)
            time.sleep(0.02)
        self.fail("The mocked question did not finish.")

    # -- 1-4: scope modes -----------------------------------------------

    def test_all_documents_scope_searches_every_current_matter_document_without_prior_analysis(self) -> None:
        first = self._add_document("custody-a.csv", ["Fund A joined the litigation on 2023-01-05."])
        second = self._add_document("custody-b.csv", ["Fund B withdrew from the litigation on 2023-06-30."])
        self._add_document("unrelated.csv", ["Quarterly newsletter"])
        self._authorize_cloud()
        preview = self.service.question_preview(self._request("Which subfunds participate in the litigation?"))
        scope = preview["documentScope"]
        self.assertEqual(scope["mode"], "all")
        self.assertEqual(scope["matterId"], "workspace")
        self.assertEqual(scope["documentsInScope"], 3)
        self.assertEqual(scope["searchableDocuments"], 3)
        self.assertEqual({item["documentId"] for item in scope["transmittedDocuments"]}, {first, second})
        self.assertEqual(scope["documentsWithEvidence"], 2)
        status = self._ask(self._request("Which subfunds participate in the litigation?"))
        self.assertEqual(status["status"], "completed")
        self.assertIn("Fund A joined the litigation", self.provider.contexts[-1])
        self.assertIn("Fund B withdrew from the litigation", self.provider.contexts[-1])
        self.assertNotIn("Quarterly newsletter", self.provider.contexts[-1])
        self.assertEqual(status["documentScope"]["mode"], "all")

    def test_one_selected_document_restricts_evidence_to_that_file(self) -> None:
        first = self._add_document("custody-a.csv", ["Fund A joined the litigation."])
        self._add_document("custody-b.csv", ["Fund B withdrew from the litigation."])
        self._authorize_cloud()
        status = self._ask(self._request("Who is in the litigation?", mode="selected", document_ids=[first]))
        self.assertEqual(status["status"], "completed")
        context = self.provider.contexts[-1]
        self.assertIn("Fund A joined", context)
        self.assertNotIn("Fund B", context)
        self.assertNotIn("custody-b.csv", context)
        self.assertTrue(all(citation["documentId"] == first for citation in status["citations"]))
        self.assertEqual(status["documentScope"]["documentsInScope"], 1)

    def test_multiple_selected_documents_restrict_evidence_to_those_files(self) -> None:
        first = self._add_document("a.csv", ["Fund A joined the litigation."])
        second = self._add_document("b.csv", ["Fund B joined the litigation."])
        self._add_document("c.csv", ["Fund C joined the litigation."])
        self._authorize_cloud()
        request = self._request("Who joined the litigation?", mode="selected", document_ids=[first, second])
        preview = self.service.question_preview(request)
        self.assertEqual({item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]}, {first, second})
        status = self._ask(request)
        self.assertEqual(status["status"], "completed")
        self.assertIn("Fund A", self.provider.contexts[-1])
        self.assertIn("Fund B", self.provider.contexts[-1])
        self.assertNotIn("Fund C", self.provider.contexts[-1])

    def test_all_document_question_interleaves_evidence_from_multiple_relevant_files(self) -> None:
        # The first document has many matches; interleaving must still let
        # the second document's single match into the bounded prompt.
        busy = self._add_document("busy.csv", [f"Litigation note {index}" for index in range(30)])
        quiet = self._add_document("quiet.csv", ["Fund Q settled its litigation claim."])
        preview = self.service.question_preview(self._request("What litigation records exist?"))
        transmitted = {item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]}
        self.assertEqual(transmitted, {busy, quiet})

    # -- 5-7: boundary validation ----------------------------------------

    def test_documents_outside_the_matter_and_unknown_matters_are_rejected(self) -> None:
        self._add_document("a.csv", ["Fund A joined the litigation."])
        with self.assertRaisesRegex(MatterReviewError, "do not belong to this matter"):
            self.service.question_preview(self._request("Who?", mode="selected", document_ids=[str(uuid.uuid4())]))
        with self.assertRaisesRegex(MatterReviewError, "matter is unavailable"):
            self.service.question_preview({**self._request("Who joined the litigation?"), "matterId": "another-matter"})

    def test_removed_document_is_rejected_and_its_index_entries_are_deleted(self) -> None:
        source = Path(self.temporary.name) / "removable.csv"
        source.write_text("Party,Status\nFund R,withdrew from the litigation\n", encoding="utf-8")
        imported = DocumentImportService(self.storage).import_authorized_paths([str(source)])
        document_id = str(imported["documents"][0]["document"]["id"])
        preview = self.service.question_preview(self._request("Who withdrew from the litigation?"))
        self.assertEqual(preview["documentScope"]["searchableDocuments"], 1)
        DocumentImportService(self.storage).remove_document(document_id)
        assert self.storage.database
        with self.storage.database.connection() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM document_search_fts WHERE source_document_id = ?", (document_id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        with self.assertRaisesRegex(MatterReviewError, "do not belong to this matter"):
            self.service.question_preview(self._request("Who?", mode="selected", document_ids=[document_id]))

    def test_invalid_scope_requests_are_rejected(self) -> None:
        document_id = self._add_document("a.csv", ["Fund A"])
        invalid = [
            self._request("Who?", mode="selected", document_ids=["not-a-uuid"]),
            self._request("Who?", mode="selected", document_ids=[document_id, document_id]),
            self._request("Who?", mode="everything"),
            # An all-document request must not smuggle in a document list.
            self._request("Who?", mode="all", document_ids=[document_id]),
            {"documentIds": [document_id], "question": "Legacy unscoped request"},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(MatterReviewError):
                self.service.question_preview(request)

    def test_empty_selected_scope_is_rejected_rather_than_expanded(self) -> None:
        self._add_document("a.csv", ["Fund A joined the litigation."])
        self._authorize_cloud()
        request = self._request("Who joined the litigation?", mode="selected", document_ids=[])
        with self.assertRaisesRegex(MatterReviewError, "Select at least one document"):
            self.service.question_preview(request)
        with self.assertRaisesRegex(MatterReviewError, "Select at least one document"):
            self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": []})
        self.assertEqual(self.provider.contexts, [])

    # -- 8: scope changes ------------------------------------------------

    def test_scope_change_keeps_history_but_blocks_follow_up_leakage(self) -> None:
        first = self._add_document("a.csv", ["Fund A joined the litigation (confidential A)."])
        second = self._add_document("b.csv", ["Fund B joined the litigation."])
        self._authorize_cloud()
        parent = self._ask(self._request("Who joined the litigation?"))
        self.assertEqual(parent["status"], "completed")
        self.assertIn("confidential A", self.provider.contexts[-1])
        # Narrowing to document B must not carry the all-document answer
        # (derived partly from document A) into the new request.
        narrowed_follow_up = self._request(
            "And what about the litigation dates?", mode="selected", document_ids=[second],
            followUpQuestionId=parent["questionId"],
        )
        with self.assertRaisesRegex(MatterReviewError, "outside the current scope"):
            self.service.question_preview(narrowed_follow_up)
        # A fresh question in the narrower scope works and sends nothing from A.
        narrowed = self._ask(self._request("Who joined the litigation?", mode="selected", document_ids=[second]))
        self.assertEqual(narrowed["status"], "completed")
        self.assertNotIn("confidential A", self.provider.contexts[-1])
        self.assertNotIn("a.csv", self.provider.contexts[-1])
        # The earlier answer remains available with its original scope label.
        history = self.service.question_status({"questionId": parent["questionId"]})
        self.assertEqual(history["documentScope"]["mode"], "all")
        self.assertEqual(narrowed["documentScope"]["mode"], "selected")
        # Within the original (all-document) scope, the follow-up is allowed
        # and the previous answer is marked as non-evidence context.
        follow_up = self._ask(self._request(
            "And what about the litigation dates?", followUpQuestionId=parent["questionId"],
        ))
        self.assertEqual(follow_up["status"], "completed")
        self.assertIn("FOLLOW-UP CONTEXT (not evidence", self.provider.contexts[-1])
        self.assertTrue({first, second} >= {citation["documentId"] for citation in follow_up["citations"]})

    def test_follow_up_text_cannot_be_supplied_by_the_renderer(self) -> None:
        self._add_document("a.csv", ["Fund A joined the litigation."])
        with self.assertRaises(MatterReviewError):
            self.service.question_preview({
                **self._request("And then?"),
                "followUp": {"question": "Injected", "answer": "Text from an excluded document"},
            })
        with self.assertRaisesRegex(MatterReviewError, "earlier answer is unavailable"):
            self.service.question_preview(self._request("And then?", followUpQuestionId=str(uuid.uuid4())))

    # -- 9: conflicts ----------------------------------------------------

    def test_conflicting_findings_from_two_documents_are_both_presented_and_cited(self) -> None:
        march = self._add_document("custody-march.csv", ["Fund A holds quantity 100 of XS0000000001."])
        april = self._add_document("custody-april.csv", ["Fund A holds quantity 60 of XS0000000001."])
        self._use_provider(_ConflictProvider())
        self._authorize_cloud()
        status = self._ask(self._request("What quantity does Fund A hold?"))
        self.assertEqual(status["status"], "completed")
        context = self.provider.contexts[-1]
        self.assertIn("quantity 100", context)
        self.assertIn("quantity 60", context)
        self.assertIn("CONFLICTS:", context)
        self.assertEqual({citation["documentId"] for citation in status["citations"]}, {march, april})
        self.assertEqual(
            {citation["filename"] for citation in status["citations"]},
            {"custody-march.csv", "custody-april.csv"},
        )

    # -- 10-11: bounded retrieval and unavailable extraction --------------

    def test_large_matter_uses_bounded_retrieval_and_reports_partial_coverage(self) -> None:
        for index in range(60):
            self._add_document(f"statement-{index:02d}.csv", [f"Litigation entry {index}-{row}" for row in range(50)])
        preview = self.service.question_preview(self._request("Compare the litigation entries across all files."))
        scope = preview["scope"]
        document_scope = preview["documentScope"]
        self.assertEqual(document_scope["documentsInScope"], 60)
        self.assertEqual(document_scope["searchableDocuments"], 60)
        self.assertTrue(document_scope["searchTruncated"])
        self.assertLessEqual(scope["sourceItems"], MAX_QUESTION_CITATION_SOURCES)
        self.assertLess(document_scope["documentsWithEvidence"], 60)
        self.assertFalse(scope["comparisonCoverageComplete"])
        self.assertTrue(scope["truncated"])
        self.assertLessEqual(preview["cost"]["estimatedInputTokens"], 1_500)

    def test_missing_or_incomplete_extraction_is_reported_not_ignored(self) -> None:
        parsed = self._add_document("parsed.csv", ["Fund A joined the litigation."])
        imported = self._add_document("pending.pdf", status="imported")
        failed = self._add_document("broken.xlsx", status="failed")
        unsupported = self._add_document("image.tiff", status="unsupported")
        empty_scan = self._add_document("scan.pdf", pdf_blocks=[], unavailable_pdf_page=True)
        partial = self._add_document("partial.pdf", pdf_blocks=["Fund P joined the litigation."], unavailable_pdf_page=True)
        preview = self.service.question_preview(self._request("Who joined the litigation?"))
        scope = preview["documentScope"]
        reasons = {item["documentId"]: item["reason"] for item in scope["unavailableDocuments"]}
        self.assertEqual(reasons, {
            imported: "not_parsed", failed: "extraction_failed", unsupported: "unsupported", empty_scan: "no_searchable_text",
        })
        self.assertEqual([item["documentId"] for item in scope["incompleteDocuments"]], [partial])
        self.assertEqual(scope["searchableDocuments"], 2)
        self.assertEqual({item["documentId"] for item in scope["transmittedDocuments"]}, {parsed, partial})
        with self.assertRaisesRegex(MatterReviewError, "No document in this scope has searchable"):
            self.service.question_preview(self._request("Who?", mode="selected", document_ids=[imported, failed]))

    def test_reparsed_document_searches_only_its_current_extraction(self) -> None:
        document_id = self._add_document("statement.csv", ["Fund OLD joined the litigation."])
        self.service.question_preview(self._request("Who joined the litigation?"))
        assert self.storage.database
        new_extraction = str(uuid.uuid4())
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, 'synthetic', 'parsed', '2026-02-01T00:00:00+00:00', '2026-02-01T00:00:00+00:00')",
                (new_extraction, document_id),
            )
            connection.execute(
                "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                "VALUES (?, ?, 'csv_cell', 'CSV', 1, 1, 1, 'Fund NEW joined the litigation.', 'text', NULL, 0, 'CSV!A1')",
                (str(uuid.uuid4()), new_extraction),
            )
        self._authorize_cloud()
        self._ask(self._request("Who joined the litigation?"))
        self.assertIn("Fund NEW", self.provider.contexts[-1])
        self.assertNotIn("Fund OLD", self.provider.contexts[-1])

    # -- 12: citations resolve to navigable stored locations --------------

    def test_citations_resolve_to_stored_locations_the_document_viewer_can_open(self) -> None:
        spreadsheet = self._add_document("custody.csv", ["Header", "Fund A joined the litigation."])
        pdf = self._add_document("letter.pdf", pdf_blocks=["Introduction", "Fund B joined the litigation."])
        self._use_provider(_RecordingProvider(citation_count=2))
        self._authorize_cloud()
        status = self._ask(self._request("Who joined the litigation?"))
        locations = {citation["documentId"]: citation["sourceReference"] for citation in status["citations"]}
        self.assertEqual(locations, {spreadsheet: "CSV!A2", pdf: "page 1 block 1"})
        documents = DocumentImportService(self.storage)
        grid = documents.content(spreadsheet, None, None, "CSV", 2, 1, 1, 1)
        self.assertEqual(grid["grid"]["cells"][0]["source_reference"], "CSV!A2")
        page = documents.content(pdf, None, None, page_number=1)
        self.assertEqual(page["references"][0]["blocks"][1]["text"], "Fund B joined the litigation.")

    # -- 13-14: consent and cost controls --------------------------------

    def test_consent_must_cover_every_transmitted_document(self) -> None:
        first = self._add_document("a.csv", ["Fund A joined the litigation."])
        second = self._add_document("b.csv", ["Fund B joined the litigation."])
        self._authorize_cloud()
        request = self._request("Who joined the litigation?")
        preview = self.service.question_preview(request)
        self.assertEqual(
            {(item["documentId"], item["previouslyAuthorized"]) for item in preview["documentScope"]["transmittedDocuments"]},
            {(first, False), (second, False)},
        )
        with self.assertRaisesRegex(MatterReviewError, "changed after you reviewed"):
            self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": [first]})
        outside = str(uuid.uuid4())
        with self.assertRaisesRegex(MatterReviewError, "only cover documents in the current question scope"):
            self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": [first, second, outside]})
        with self.assertRaisesRegex(MatterReviewError, "only cover documents in the current question scope"):
            self.service.ask_question({
                **self._request("Who joined the litigation?", mode="selected", document_ids=[first]),
                "acknowledged": True, "consentedDocumentIds": [first, second],
            })
        self.assertEqual(self.provider.contexts, [])
        self._ask(request, consented=[first, second])
        assert self.storage.database
        with self.storage.database.connection() as connection:
            audited = {
                row["target_id"] for row in connection.execute(
                    "SELECT target_id FROM audit_events WHERE event_type = 'ai_matter_question_document_authorized'",
                ).fetchall()
            }
            payloads = [row["details_json"] for row in connection.execute("SELECT details_json FROM audit_events").fetchall()]
        self.assertEqual(audited, {first, second})
        self.assertFalse(any("Fund A" in str(payload) or "litigation" in str(payload) for payload in payloads))
        # Consent is remembered for this provider until cloud processing is re-enabled.
        again = self.service.question_preview(request)
        self.assertTrue(all(item["previouslyAuthorized"] for item in again["documentScope"]["transmittedDocuments"]))
        self._authorize_cloud(False)
        self._authorize_cloud(True)
        reset = self.service.question_preview(request)
        self.assertFalse(any(item["previouslyAuthorized"] for item in reset["documentScope"]["transmittedDocuments"]))

    def test_global_authorization_is_still_required(self) -> None:
        first = self._add_document("a.csv", ["Fund A joined the litigation."])
        with self.assertRaisesRegex(MatterReviewError, "Enable cloud-processing authorization"):
            self.service.ask_question({
                **self._request("Who joined the litigation?"), "acknowledged": True, "consentedDocumentIds": [first],
            })
        self.assertEqual(self.provider.contexts, [])

    def test_cost_limit_still_blocks_an_all_document_question(self) -> None:
        first = self._add_document("a.csv", ["Fund A joined the litigation."])
        self._authorize_cloud()
        os.environ["GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION"] = "1000"
        os.environ["GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION"] = "1000"
        request = self._request("Who joined the litigation?")
        preview = self.service.question_preview(request)
        self.assertFalse(preview["cost"]["withinLimit"])
        self.assertEqual(preview["cost"]["maxRequestAttempts"], 1)
        with self.assertRaisesRegex(MatterReviewError, "safety limit"):
            self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": [first]})
        self.assertEqual(self.provider.contexts, [])

    def test_existing_reviewed_summary_is_still_used_for_selected_documents(self) -> None:
        """Earlier AI review output for the current extraction still informs answers."""
        document_id = self._add_document("holdings.csv", ["Fund A"])
        assert self.storage.database
        with self.storage.database.connection() as connection:
            extraction_id = connection.execute(
                "SELECT id FROM document_extractions WHERE source_document_id = ?", (document_id,),
            ).fetchone()[0]
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO ai_processing_jobs (id, source_document_id, extraction_id, provider, model, prompt_version, "
                "schema_version, status, summary_text, summary_unknown, summary_citations_json, created_at, completed_at) "
                "VALUES (?, ?, ?, 'openai', 'test', 'v1', 'v1', 'completed', 'Fund A is recorded.', 0, ?, ?, ?)",
                (str(uuid.uuid4()), document_id, extraction_id, json.dumps([{"sourceReference": "CSV!A1"}]),
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        preview = self.service.question_preview(self._request("Which fund is named?", mode="selected", document_ids=[document_id]))
        self.assertEqual(preview["scope"]["sourceItems"], 1)


if __name__ == "__main__":
    unittest.main()
