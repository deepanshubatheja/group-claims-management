"""Inventory/listing questions and deterministic question routing.

Synthetic fixtures and mock providers only; no live AI requests.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from group_claims_backend.services import entity_inventory
from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.matter_review import MatterReviewService
from group_claims_backend.services.question_intent import (
    INTENT_CALCULATION, INTENT_COMPARISON, INTENT_GENERAL, INTENT_INVENTORY, INTENT_LEGAL_STATUS, INTENT_LOOKUP,
    ROLE_CUSTODIAN, ROLE_INVESTOR, ROLE_SUBFUND,
)
from group_claims_backend.storage.service import StorageService

ORIGINAL_QUESTION = "List all investors, investment vehicles, and subfunds mentioned across my uploaded documents."
_ENVIRONMENT = (
    "OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER",
    "GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION", "GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION",
)


class _RecordingProvider:
    def __init__(self) -> None:
        self.contexts: list[str] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.contexts.append(context)
        return AnswerResult(
            "Synthetic answer.", False, ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "x"},),
            "mock", 1, 1, 2,
        )


class QuestionIntentTests(unittest.TestCase):
    def test_routes_each_supported_intent(self) -> None:
        intent = MatterReviewService._question_intent
        self.assertEqual(intent(ORIGINAL_QUESTION).kind, INTENT_INVENTORY)
        self.assertEqual(intent("Which subfunds are mentioned?").kind, INTENT_INVENTORY)
        self.assertEqual(intent("Name all custodians").roles & {ROLE_CUSTODIAN}, {ROLE_CUSTODIAN})
        self.assertEqual(intent("Which fund is named?").kind, INTENT_GENERAL)  # singular: not a listing
        self.assertEqual(intent("Which document mentions XS0000000001?").kind, INTENT_LOOKUP)
        self.assertEqual(intent("Which document mentions XS0000000001?").identifiers, ("XS0000000001",))
        self.assertEqual(intent("Compare the March and April statements").kind, INTENT_COMPARISON)
        self.assertEqual(intent("Calculate the pro-rata allocation").kind, INTENT_CALCULATION)
        self.assertEqual(intent("Which subfund withdrew from litigation?").kind, INTENT_LEGAL_STATUS)
        self.assertEqual(intent("What can you tell me about these files?").kind, INTENT_GENERAL)

    def test_inventory_roles_follow_the_nouns_asked_for(self) -> None:
        roles = MatterReviewService._question_intent(ORIGINAL_QUESTION).roles
        self.assertTrue({ROLE_INVESTOR, ROLE_SUBFUND} <= roles)
        self.assertNotIn(ROLE_CUSTODIAN, roles)

    def test_general_terms_drop_filler_words_and_plurals(self) -> None:
        terms = MatterReviewService._question_terms("Which documents mention the investors of Alpha Partners?")
        self.assertIn("investor", terms)
        self.assertNotIn("documents", terms)
        self.assertNotIn("which", terms)


class EntityInventoryTests(unittest.TestCase):
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
        analysis = AIAnalysisService(self.storage, provider_factory=lambda: self.provider)  # type: ignore[arg-type]
        self.service = MatterReviewService(self.storage, analysis)
        self._sequence = 0

    def tearDown(self) -> None:
        for key, value in self._previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _add_document(
        self, filename: str, *, rows: list[list[str]] | None = None, blocks: list[str] | None = None,
        status: str = "parsed",
    ) -> tuple[str, str]:
        """Insert one synthetic document: a CSV grid (rows of cells) or PDF text blocks."""
        self._sequence += 1
        document_id, extraction_id = str(uuid.uuid4()), str(uuid.uuid4())
        imported_at = f"2026-01-01T00:00:{self._sequence:02d}+00:00"
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, "
                "file_size_bytes, imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (document_id, filename, f"documents/{document_id}", (document_id.replace("-", "") * 2)[:64],
                 imported_at, "pdf" if blocks is not None else "csv", status),
            )
            if status != "parsed":
                return document_id, extraction_id
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, 'synthetic', 'parsed', ?, ?)",
                (extraction_id, document_id, imported_at, imported_at),
            )
            for row_number, row in enumerate(rows or [], start=1):
                for column_number, value in enumerate(row, start=1):
                    if not value:
                        continue
                    reference = f"CSV!{chr(64 + column_number)}{row_number}"
                    connection.execute(
                        "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                        "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                        "VALUES (?, ?, 'csv_cell', 'CSV', ?, ?, 1, ?, 'text', NULL, 0, ?)",
                        (str(uuid.uuid4()), extraction_id, row_number, column_number, value, reference),
                    )
            if blocks is not None:
                page_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO pdf_page_extractions (id, extraction_id, page_number, extraction_method, raw_text, "
                    "cleaned_text, block_count) VALUES (?, ?, 1, 'native_text', ?, ?, ?)",
                    (page_id, extraction_id, "\n".join(blocks), "\n".join(blocks), len(blocks)),
                )
                for index, block in enumerate(blocks):
                    connection.execute(
                        "INSERT INTO pdf_text_blocks (id, pdf_page_extraction_id, block_index, text, is_table_candidate) "
                        "VALUES (?, ?, ?, ?, 0)",
                        (str(uuid.uuid4()), page_id, index, block),
                    )
        return document_id, extraction_id

    @staticmethod
    def _request(question: str = ORIGINAL_QUESTION, *, mode: str = "all", document_ids: list[str] | None = None) -> dict[str, object]:
        scope: dict[str, object] = {"mode": mode}
        if document_ids is not None:
            scope["documentIds"] = document_ids
        return {"matterId": "workspace", "scope": scope, "question": question}

    def _inventory(self, request: dict[str, object] | None = None) -> dict[str, object]:
        status = self.service.ask_question({**(request or self._request()), "acknowledged": True, "consentedDocumentIds": []})
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["intent"], "inventory")
        return dict(status)

    @staticmethod
    def _entity(status: dict[str, object], name: str) -> dict[str, object]:
        for entity in status["inventory"]["entities"]:  # type: ignore[index]
            if entity["name"].casefold() == name.casefold():
                return entity
        raise AssertionError(f"{name} is not listed")

    @staticmethod
    def _names(status: dict[str, object]) -> set[str]:
        return {entity["name"] for entity in status["inventory"]["entities"]}  # type: ignore[index]

    def test_original_question_is_answered_locally_as_an_inventory(self) -> None:
        self._add_document("register.csv", rows=[["Investor", "Subfund"], ["Alpha Pension Fund", "Alpha Sub I"]])
        self._add_document("notes.pdf", blocks=["Omega Securities Ltd is an investment firm regulated by the authority."])
        preview = self.service.question_preview(self._request())
        self.assertEqual(preview["intent"], "inventory")
        self.assertTrue(preview["localOnly"])
        self.assertEqual(preview["documentScope"]["transmittedDocuments"], [])
        status = self._inventory()
        self.assertEqual(status["model"], "local-entity-inventory")
        self.assertEqual(self.provider.contexts, [])  # no provider request at all
        self.assertEqual(self._names(status), {"Alpha Pension Fund", "Alpha Sub I"})
        # An unlabelled organisation in prose is not classified as an investor.
        self.assertNotIn("Omega Securities Ltd", self._names(status))
        self.assertEqual(status["inventory"]["coverage"]["searchedDocuments"], 2)

    def test_investor_in_several_documents_and_duplicates_merge(self) -> None:
        first, _ = self._add_document("q1.csv", rows=[["Investor"], ["Alpha Pension Fund"], ["Alpha Pension Fund"]])
        second, _ = self._add_document("q2.csv", rows=[["Investor name"], ["  alpha pension  fund "]])
        third, _ = self._add_document("letter.pdf", blocks=["Investor: Alpha Pension Fund."])
        entity = self._entity(self._inventory(), "Alpha Pension Fund")
        self.assertEqual(entity["roles"], [ROLE_INVESTOR])
        self.assertEqual(entity["documentCount"], 3)
        self.assertEqual({item["documentId"] for item in entity["evidence"]}, {first, second, third})
        self.assertEqual(len([name for name in self._names(self._inventory()) if "alpha" in name.casefold()]), 1)

    def test_subfund_under_investor_and_custodian_alongside_fund(self) -> None:
        self._add_document("positions.csv", rows=[
            ["Investor", "Subfund", "Custodian", "ISIN", "Quantity"],
            ["Alpha Pension Fund", "Alpha Sub I", "Example Custody Bank", "XS0000000001", "100"],
        ])
        self._add_document("umbrella.pdf", blocks=["Beta Income, a sub-fund of Beta Umbrella SICAV, holds the notes."])
        self._add_document("custody.pdf", blocks=["Fund: Gamma Growth Fund", "Custodian: Delta Depositary AG"])
        status = self._inventory()
        sub = self._entity(status, "Alpha Sub I")
        self.assertEqual(sub["roles"], [ROLE_SUBFUND])
        self.assertIn({"name": "Alpha Pension Fund", "role": ROLE_INVESTOR}, sub["related"])
        beta = self._entity(status, "Beta Income")
        self.assertEqual(beta["roles"], [ROLE_SUBFUND])
        self.assertIn({"name": "Beta Umbrella SICAV", "role": "investment_vehicle"}, beta["related"])
        self.assertIn("Gamma Growth Fund", self._names(status))
        # Custodians were not requested and are never listed as investors.
        self.assertNotIn("Example Custody Bank", self._names(status))
        self.assertNotIn("Delta Depositary AG", self._names(status))
        self.assertEqual(status["inventory"]["otherRoleCounts"], {ROLE_CUSTODIAN: 2})
        custodians = self._inventory(self._request("List all custodians."))
        self.assertEqual(self._names(custodians), {"Example Custody Bank", "Delta Depositary AG"})

    def test_late_documents_in_a_large_matter_are_still_covered(self) -> None:
        for index in range(22):
            self._add_document(
                f"filler-{index:02d}.pdf",
                blocks=[f"The investment firm reported documents uploaded on day {index}. " * 4],
            )
        late, _ = self._add_document("zz-register.csv", rows=[["Subfund"], ["Late Sub Fund Z"]])
        self._add_document("zz-letter.pdf", blocks=["Investor: Late Investor Y"])
        status = self._inventory()
        self.assertEqual(self._names(status), {"Late Sub Fund Z", "Late Investor Y"})
        coverage = status["inventory"]["coverage"]
        self.assertEqual((coverage["eligibleDocuments"], coverage["searchedDocuments"]), (24, 24))
        self.assertEqual(coverage["documentsWithListedEntities"], 2)
        self.assertFalse(coverage["partial"])
        self.assertIn(late, {item["documentId"] for item in self._entity(status, "Late Sub Fund Z")["evidence"]})

    def test_selected_scope_restricts_the_inventory(self) -> None:
        first, _ = self._add_document("a.csv", rows=[["Investor"], ["Alpha Pension Fund"]])
        self._add_document("b.csv", rows=[["Investor"], ["Bravo Family Office"]])
        status = self._inventory(self._request(mode="selected", document_ids=[first]))
        self.assertEqual(self._names(status), {"Alpha Pension Fund"})
        self.assertEqual(status["documentScope"]["documentsInScope"], 1)
        self.assertTrue(all(citation["documentId"] == first for citation in status["citations"]))

    def test_partial_and_missing_extraction_are_reported(self) -> None:
        self._add_document("a.csv", rows=[["Investor"], ["Alpha Pension Fund"]])
        self._add_document("broken.xlsx", status="failed")
        status = self._inventory()
        self.assertEqual(status["documentScope"]["unavailableDocuments"][0]["reason"], "extraction_failed")
        self.assertEqual(status["inventory"]["coverage"]["eligibleDocuments"], 1)
        with mock.patch.object(entity_inventory, "MAX_TABLE_CELLS", 0):
            limited = self._inventory()
        self.assertTrue(limited["inventory"]["coverage"]["partial"])
        self.assertEqual([item["filename"] for item in limited["inventory"]["coverage"]["partialDocuments"]], ["a.csv"])
        self.assertIn("Coverage is partial", str(limited["answer"]))
        self.assertTrue(limited["scope"]["truncated"])

    def test_conflicting_role_evidence_stays_unresolved(self) -> None:
        self._add_document("investors.csv", rows=[["Investor"], ["Delta Capital"]])
        self._add_document("custody.pdf", blocks=["Custodian: Delta Capital"])
        entity = self._entity(self._inventory(), "Delta Capital")
        self.assertEqual(entity["roles"], [ROLE_CUSTODIAN, ROLE_INVESTOR])
        self.assertIn("Conflicting role evidence", str(entity["uncertainty"]))

    def test_citations_point_to_the_exact_stored_source(self) -> None:
        document_id, _ = self._add_document("register.csv", rows=[["Name", "Investor"], ["x", "Alpha Pension Fund"]])
        pdf_id, _ = self._add_document("letter.pdf", blocks=["Introduction", "Investor: Bravo Family Office"])
        status = self._inventory()
        alpha = self._entity(status, "Alpha Pension Fund")["evidence"][0]
        self.assertEqual((alpha["documentId"], alpha["sourceReference"], alpha["evidenceSnippet"]), (document_id, "CSV!B2", "Alpha Pension Fund"))
        self.assertIn("column heading “Investor” (CSV!B1)", alpha["basis"])
        bravo = self._entity(status, "Bravo Family Office")["evidence"][0]
        self.assertEqual((bravo["documentId"], bravo["sourceReference"]), (pdf_id, "page 1 block 1"))
        self.assertTrue({(citation["documentId"], citation["sourceReference"]) for citation in status["citations"]}
                        >= {(document_id, "CSV!B2"), (pdf_id, "page 1 block 1")})

    def test_existing_ai_review_findings_are_reused_as_provisional(self) -> None:
        document_id, extraction_id = self._add_document("statement.csv", rows=[["Holder"], ["Echo Retirement Plan"]])
        assert self.storage.database
        job_id = str(uuid.uuid4())
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO ai_processing_jobs (id, source_document_id, extraction_id, provider, model, prompt_version, "
                "schema_version, status, created_at, completed_at) VALUES (?, ?, ?, 'openai', 'test', 'v1', 'v1', 'completed', ?, ?)",
                (job_id, document_id, extraction_id, "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO ai_proposals (id, job_id, proposal_type, field_name, value_json, is_unknown, confidence, "
                "validation_status, source_reference, evidence_snippet, created_at) "
                "VALUES (?, ?, 'investor_legal_entity', 'name', ?, 0, 0.8, 'validated', 'CSV!A2', 'Echo', ?)",
                (str(uuid.uuid4()), job_id, json.dumps("Echo Retirement Plan"), "2026-01-02T00:00:00+00:00"),
            )
        entity = self._entity(self._inventory(), "Echo Retirement Plan")
        self.assertEqual(entity["roles"], ["investor_or_legal_entity"])
        self.assertEqual(entity["evidence"][0]["status"], "provisional_ai_finding")
        self.assertIn("provisional AI review finding", str(entity["uncertainty"]))

    def test_no_labelled_entities_gives_a_qualified_answer_not_a_guess(self) -> None:
        self._add_document("prose.pdf", blocks=["Omega Securities Ltd and Sigma Bank AG exchanged letters."])
        status = self._inventory()
        self.assertEqual(status["inventory"]["entities"], [])
        self.assertIn("No explicitly labelled entities", str(status["answer"]))

    def test_specific_lookup_searches_its_identifier(self) -> None:
        target, _ = self._add_document("holdings.csv", rows=[["ISIN"], ["XS0000000001"]])
        self._add_document("other.csv", rows=[["ISIN"], ["DE0000000002"]])
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
        request = self._request("Which document mentions XS0000000001?")
        preview = self.service.question_preview(request)
        self.assertEqual(preview["intent"], "specific_lookup")
        self.assertEqual([item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]], [target])
        started = self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": [target]})
        for _ in range(50):
            status = self.service.question_status({"questionId": started["questionId"]})
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(status["status"], "completed")
        self.assertIn("XS0000000001", self.provider.contexts[-1])
        self.assertNotIn("DE0000000002", self.provider.contexts[-1])

    def test_inventory_needs_no_cloud_authorization_and_costs_nothing(self) -> None:
        self._add_document("a.csv", rows=[["Investor"], ["Alpha Pension Fund"]])
        os.environ["GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION"] = "1000"
        os.environ["GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION"] = "1000"
        preview = self.service.question_preview(self._request())
        self.assertEqual(preview["cost"]["estimatedCostUsd"], "0.000000")
        self.assertEqual(preview["cost"]["maxRequestAttempts"], 0)
        self.assertEqual(self._names(self._inventory()), {"Alpha Pension Fund"})
        self.assertEqual(self.provider.contexts, [])
        # A general (paid) question is still blocked by the unchanged limits.
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
        general = self.service.question_preview(self._request("What is Alpha Pension Fund's investor status?"))
        self.assertFalse(general["cost"]["withinLimit"])


if __name__ == "__main__":
    unittest.main()
