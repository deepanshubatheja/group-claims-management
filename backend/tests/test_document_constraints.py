"""Explicit document constraints, relationship mapping and dated holdings comparison.

Reproduces two live failures with synthetic fixtures: questions naming two dated
holdings workbooks were answered from an unrelated email and a PDF address
block.  Mock providers only; no live AI requests.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.document_references import CandidateDocument, filename_dates, question_dates, resolve_document_references
from group_claims_backend.services.matter_review import MatterReviewService
from group_claims_backend.services.question_intent import (
    INTENT_CHANGE, INTENT_HOLDINGS_COMPARISON, INTENT_LEGAL_STATUS, INTENT_LOOKUP, INTENT_RELATIONSHIP,
)
from group_claims_backend.storage.service import StorageService

RELATIONSHIP_QUESTION = (
    "Using only the two holdings spreadsheets dated March 28 and April 30, identify which investor each subfund is "
    "associated with. Show exact cells for both names and distinguish a documented relationship from merely "
    "appearing in the same row."
)
COMPARISON_QUESTION = (
    "Compare Horizon Credit Fund A, Horizon Credit Fund B, and Meridian Income Fund C between the March 28 and "
    "April 30 holdings spreadsheets. Show dated holding quantities for each ISIN, calculate differences, and cite "
    "both source rows."
)
HEADER = ["Investor", "Subfund", "Account", "ISIN", "Quantity", "Unit"]
_ENVIRONMENT = ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")


class _RecordingProvider:
    def __init__(self) -> None:
        self.contexts: list[str] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.contexts.append(context)
        return AnswerResult(
            "Synthetic cited answer.", False, ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "x"},),
            "mock", 1, 1, 2,
        )


def _row(*values: object) -> list[tuple[str, str]]:
    """Cells as (text, openpyxl data type): numbers 'n', formulas 'f', text 's'."""
    cells: list[tuple[str, str]] = []
    for value in values:
        if isinstance(value, tuple):
            cells.append(value)  # type: ignore[arg-type]
        elif isinstance(value, (int, float)):
            cells.append((str(value), "n"))
        else:
            cells.append((str(value), "s"))
    return cells


class DocumentConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self._previous_environment = {key: os.environ.get(key) for key in _ENVIRONMENT}
        os.environ["OPENAI_API_KEY"] = "synthetic-key"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        self.storage = StorageService(str(Path(self.temporary.name) / "app"))
        self.storage.initialize()
        self.provider = _RecordingProvider()
        analysis = AIAnalysisService(self.storage, provider_factory=lambda: self.provider)  # type: ignore[arg-type]
        self.service = MatterReviewService(self.storage, analysis)
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
        self._sequence = 0
        self.march = self._workbook("01_Holdings_2023-03-28.xlsx", [
            _row(*HEADER),
            _row("Horizon Capital Partners", "Horizon Credit Fund A", "AC-1001", "XS1111111111", 600000, "EUR"),
            _row("Horizon Capital Partners", "Horizon Credit Fund B", "AC-2001", "XS2222222222", 1000000, "EUR"),
            _row("Meridian Pension Trust", "Meridian Income Fund C", "AC-3001", "XS3333333333", 250000, "EUR"),
        ])
        self.april = self._workbook("02_Holdings_2023-04-30.xlsx", [
            _row(*HEADER),
            _row("Horizon Capital Partners", "Horizon Credit Fund A", "AC-1001", "XS1111111111", 450000, "EUR"),
            _row("Horizon Capital Partners", "Horizon Credit Fund B", "AC-2001", "XS2222222222", 1000000.0, "EUR"),
            _row("Meridian Pension Trust", "Meridian Income Fund C", "AC-3001", "XS3333333333", 200000, "EUR"),
        ])
        # Decoys with heavy keyword overlap (transfer, trade, fund, account,
        # holdings, change) but none of the requested evidence.
        self.email = self._pdf("Jefferies_email_Denise_2023-04-30.pdf", [
            "From: Denise (Jefferies). Re: transfer of holdings and change of account for the fund.",
            "Please confirm the trade, the account transfer, and the subfund holdings change before month end.",
        ])
        self.statement = self._pdf("GoldmanSachs_custody_statement.pdf", [
            "Courtney Master Fund, 200 West Street, New York — holdings statement, account transfer and quantity.",
        ])

    def tearDown(self) -> None:
        for key, value in self._previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    # -- fixtures ---------------------------------------------------------

    def _document(self, filename: str, file_format: str) -> tuple[str, str]:
        self._sequence += 1
        document_id, extraction_id = str(uuid.uuid4()), str(uuid.uuid4())
        stamp = f"2026-01-01T00:00:{self._sequence:02d}+00:00"
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, file_size_bytes, "
                "imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, 1, ?, ?, 'parsed')",
                (document_id, filename, f"documents/{document_id}", (document_id.replace("-", "") * 2)[:64], stamp, file_format),
            )
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, 'synthetic', 'parsed', ?, ?)", (extraction_id, document_id, stamp, stamp),
            )
        return document_id, extraction_id

    def _workbook(self, filename: str, rows: list[list[tuple[str, str]]], sheet: str = "Holdings") -> str:
        document_id, extraction_id = self._document(filename, filename.rsplit(".", 1)[-1])
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            for row_number, row in enumerate(rows, start=1):
                for column_number, (text, value_type) in enumerate(row, start=1):
                    if text == "":
                        continue
                    connection.execute(
                        "INSERT INTO extracted_content (id, extraction_id, content_kind, sheet_name, row_number, column_number, "
                        "worksheet_index, value_text, value_type, formula_text, is_empty, source_reference) "
                        "VALUES (?, ?, 'spreadsheet_cell', ?, ?, ?, 1, ?, ?, ?, 0, ?)",
                        (str(uuid.uuid4()), extraction_id, sheet, row_number, column_number, text, value_type,
                         text if value_type == "f" else None, f"{sheet}!{chr(64 + column_number)}{row_number}"),
                    )
        return document_id

    def _pdf(self, filename: str, blocks: list[str]) -> str:
        document_id, extraction_id = self._document(filename, "pdf")
        page_id = str(uuid.uuid4())
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO pdf_page_extractions (id, extraction_id, page_number, extraction_method, raw_text, cleaned_text, "
                "block_count) VALUES (?, ?, 1, 'native_text', ?, ?, ?)",
                (page_id, extraction_id, "\n".join(blocks), "\n".join(blocks), len(blocks)),
            )
            for index, block in enumerate(blocks):
                connection.execute(
                    "INSERT INTO pdf_text_blocks (id, pdf_page_extraction_id, block_index, text, is_table_candidate) VALUES (?, ?, ?, ?, 0)",
                    (str(uuid.uuid4()), page_id, index, block),
                )
        return document_id

    @staticmethod
    def _request(question: str, *, mode: str = "all", document_ids: list[str] | None = None, **extra: object) -> dict[str, object]:
        scope: dict[str, object] = {"mode": mode}
        if document_ids is not None:
            scope["documentIds"] = document_ids
        return {"matterId": "workspace", "scope": scope, "question": question, **extra}

    def _ask(self, request: dict[str, object]) -> dict[str, object]:
        preview = self.service.question_preview(request)
        consented = [item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]]
        started = self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": consented})
        for _ in range(50):
            status = self.service.question_status({"questionId": started["questionId"]})
            if status["status"] in {"completed", "failed"}:
                return dict(status)
            time.sleep(0.02)
        self.fail("question did not finish")

    def _comparison_rows(self, status: dict[str, object]) -> dict[str, dict[str, object]]:
        return {
            next(item["name"] for item in row["entities"] if item["kind"] == "subfund") + "|" + str(row["account"]): row
            for row in status["holdingsComparison"]["rows"]  # type: ignore[index]
        }

    # -- 1, 5, 11: failure 1 ---------------------------------------------

    def test_failure_1_relationship_uses_only_the_two_named_workbooks(self) -> None:
        preview = self.service.question_preview(self._request(RELATIONSHIP_QUESTION))
        self.assertEqual(preview["intent"], INTENT_RELATIONSHIP)
        self.assertTrue(preview["localOnly"])
        self.assertEqual(
            {item["filename"] for item in preview["documentScope"]["resolvedDocuments"]},
            {"01_Holdings_2023-03-28.xlsx", "02_Holdings_2023-04-30.xlsx"},
        )
        status = self._ask(self._request(RELATIONSHIP_QUESTION))
        self.assertEqual(self.provider.contexts, [])
        relationships = status["inventory"]["relationships"]  # type: ignore[index]
        fund_a = [item for item in relationships if item["subject"] == "Horizon Credit Fund A"]
        self.assertEqual({item["documentId"] for item in fund_a}, {self.march, self.april})
        for item in fund_a:
            self.assertEqual((item["related"], item["relatedRole"]), ("Horizon Capital Partners", "investor"))
            self.assertEqual((item["subjectReference"], item["relatedReference"]), ("Holdings!B2", "Holdings!A2"))
            self.assertEqual(item["kind"], "same_row")
        self.assertIn("row-level associations only", str(status["answer"]))
        self.assertTrue({citation["documentId"] for citation in status["citations"]} <= {self.march, self.april})

    def test_explicit_relationship_wording_is_distinguished_from_row_association(self) -> None:
        letter = self._pdf("Fund_structure_2023-03-28.pdf", ["Horizon Credit Fund A, a sub-fund of Horizon Umbrella SICAV, is managed."])
        status = self._ask(self._request(
            "Which umbrella fund is each subfund associated with, and which investor?", mode="selected",
            document_ids=[self.march, letter],
        ))
        kinds = {(item["subject"], item["related"], item["kind"]) for item in status["inventory"]["relationships"]}  # type: ignore[index]
        self.assertIn(("Horizon Credit Fund A", "Horizon Umbrella SICAV", "explicit_statement"), kinds)
        self.assertIn(("Horizon Credit Fund A", "Horizon Capital Partners", "same_row"), kinds)

    # -- 2, 7, 14, 15, 16: failure 2 -------------------------------------

    def test_failure_2_dated_holdings_comparison_uses_both_workbooks_and_decimal(self) -> None:
        preview = self.service.question_preview(self._request(COMPARISON_QUESTION))
        self.assertEqual(preview["intent"], INTENT_HOLDINGS_COMPARISON)
        self.assertTrue(preview["localOnly"])
        status = self._ask(self._request(COMPARISON_QUESTION))
        self.assertEqual(self.provider.contexts, [])
        comparison = status["holdingsComparison"]
        self.assertEqual([item["date"] for item in comparison["documents"]], ["2023-03-28", "2023-04-30"])  # type: ignore[index]
        rows = self._comparison_rows(status)
        self.assertEqual(rows["Horizon Credit Fund A|AC-1001"]["difference"], "-150000")
        self.assertEqual(rows["Horizon Credit Fund B|AC-2001"]["difference"], "0.0")
        self.assertEqual(rows["Meridian Income Fund C|AC-3001"]["difference"], "-50000")
        fund_a = rows["Horizon Credit Fund A|AC-1001"]
        self.assertEqual(fund_a["isin"], "XS1111111111")
        for side, document_id in (("earlier", self.march), ("later", self.april)):
            details = fund_a[side]
            self.assertEqual(details["documentId"], document_id)
            self.assertEqual(
                (details["row"], details["quantityReference"], details["isinReference"], details["unitReference"]),
                (2, "Holdings!E2", "Holdings!D2", "Holdings!F2"),
            )
            self.assertEqual(sorted(details["entityReferences"]), ["Holdings!A2", "Holdings!B2"])
        cited = {(citation["documentId"], citation["sourceReference"]) for citation in status["citations"]}
        self.assertTrue({(self.march, "Holdings!E2"), (self.april, "Holdings!E2")} <= cited)
        self.assertNotIn(self.email, {citation["documentId"] for citation in status["citations"]})
        self.assertNotIn(self.statement, {citation["documentId"] for citation in status["citations"]})
        self.assertIn("does not by itself show a trade, settlement, transfer, or any change in legal claim rights", str(status["answer"]))

    def test_exact_decimal_differences_formulas_units_and_accounts(self) -> None:
        march = self._workbook("10_Holdings_2023-05-31.xlsx", [
            _row(*HEADER),
            _row("Inv P", "Sub P", "AC-1", "XS4444444444", "100.10", "EUR"),
            _row("Inv Q", "Sub Q", "AC-2", "XS5555555555", 500, "EUR"),
            _row("Inv R", "Sub R", "AC-3", "XS6666666666", 700, "EUR"),
            _row("Inv S", "Sub S", "AC-4", "XS7777777777", "1.000", "EUR"),
        ])
        june = self._workbook("11_Holdings_2023-06-30.xlsx", [
            _row(*HEADER),
            _row("Inv P", "Sub P", "AC-1", "XS4444444444", "100.35", "EUR"),
            _row("Inv Q", "Sub Q", "AC-2", "XS5555555555", ("=E3*2", "f"), "EUR"),
            _row("Inv R", "Sub R", "AC-3", "XS6666666666", 700, "USD"),
            _row("Inv S", "Sub S", "AC-4", "XS7777777777", 1000, "EUR"),
            _row("Inv P", "Sub P", "AC-9", "XS4444444444", 50, "EUR"),
        ])
        status = self._ask(self._request(
            "Compare holding quantities between the May 31 and June 30 holdings spreadsheets.",
            mode="selected", document_ids=[march, june],
        ))
        rows = self._comparison_rows(status)
        self.assertEqual(rows["Sub P|AC-1"]["difference"], "+0.25")  # binary float would give 0.25000000000000006
        self.assertEqual((rows["Sub Q|AC-2"]["status"], rows["Sub Q|AC-2"]["difference"]), ("unusable_quantity", None))
        self.assertIn("formula", str(rows["Sub Q|AC-2"]["note"]))
        self.assertEqual((rows["Sub R|AC-3"]["status"], rows["Sub R|AC-3"]["difference"]), ("unit_mismatch", None))
        self.assertIn("ambiguous", str(rows["Sub S|AC-4"]["note"]))
        # A second account for the same subfund and ISIN is never merged.
        self.assertEqual(rows["Sub P|AC-9"]["status"], "only_later")
        self.assertIn("not evidence of a sale or transfer", str(rows["Sub P|AC-9"]["note"]))

    def test_duplicate_rows_and_ambiguous_quantity_columns_are_reported(self) -> None:
        first = self._workbook("20_Holdings_2023-07-31.xlsx", [
            _row(*HEADER),
            _row("Inv T", "Sub T", "AC-5", "XS8888888888", 10, "EUR"),
            _row("Inv T", "Sub T", "AC-5", "XS8888888888", 15, "EUR"),
        ])
        second = self._workbook("21_Holdings_2023-08-31.xlsx", [
            _row("Investor", "Subfund", "ISIN", "Quantity", "Nominal"),
            _row("Inv T", "Sub T", "XS8888888888", 10, 10),
        ])
        status = self._ask(self._request(
            "Compare holding quantities between the July 31 and August 31 holdings spreadsheets.",
            mode="selected", document_ids=[first, second],
        ))
        issues = " ".join(status["holdingsComparison"]["issues"])  # type: ignore[index]
        self.assertIn("quantity column is ambiguous", issues)
        self.assertEqual(status["holdingsComparison"]["rows"][0]["status"], "duplicate_rows")  # type: ignore[index]

    def test_missing_named_entity_is_reported_not_substituted(self) -> None:
        status = self._ask(self._request(
            "Compare Horizon Credit Fund A and Zeta Growth Fund Z between the March 28 and April 30 holdings spreadsheets.",
        ))
        self.assertIn("“Zeta Growth Fund Z” does not appear", str(status["answer"]))
        self.assertEqual(list(self._comparison_rows(status)), ["Horizon Credit Fund A|AC-1001"])

    # -- 3: intent classification ----------------------------------------

    def test_precise_intent_priority(self) -> None:
        intent = MatterReviewService._question_intent
        self.assertEqual(intent(RELATIONSHIP_QUESTION).kind, INTENT_RELATIONSHIP)
        self.assertEqual(intent(COMPARISON_QUESTION).kind, INTENT_HOLDINGS_COMPARISON)
        self.assertEqual(intent("Which investor is associated with these subfunds?").kind, INTENT_RELATIONSHIP)
        self.assertEqual(intent("Compare quantities between March 28 and April 30").kind, INTENT_HOLDINGS_COMPARISON)
        self.assertEqual(intent("Did legal claim rights transfer?").kind, INTENT_LEGAL_STATUS)
        self.assertEqual(intent("Were there any transfers recorded on April 30?").kind, INTENT_CHANGE)
        self.assertEqual(intent("Which account holds XS1111111111?").kind, INTENT_LOOKUP)

    # -- 4: filename/date resolution -------------------------------------

    def test_filename_and_date_parsing(self) -> None:
        self.assertEqual(filename_dates("01_Holdings_2023-03-28.xlsx"), [(2023, 3, 28)])
        self.assertEqual(filename_dates("report_20230430.pdf"), [(2023, 4, 30)])
        self.assertEqual(filename_dates("stmt 28.03.2023.pdf"), [(2023, 3, 28)])
        self.assertEqual(
            [(item.year, item.month, item.day) for item in question_dates("between March 28 and 30 April 2023, or 2023-05-31")],
            [(None, 3, 28), (2023, 4, 30), (2023, 5, 31)],
        )
        documents = [
            CandidateDocument("a", "01_Holdings_2023-03-28.xlsx", "xlsx", True),
            CandidateDocument("b", "Jefferies_email_2023-03-28.pdf", "pdf", True),
        ]
        constraint = resolve_document_references("the spreadsheet dated March 28", documents)
        self.assertEqual([item.document_id for item in constraint.resolved], ["a"])
        # Without a document noun, a date is content, not a document reference.
        self.assertFalse(resolve_document_references("What was traded on March 28?", documents).requested)

    def test_numbered_documents_take_precedence_over_a_repeated_date(self) -> None:
        documents = [
            CandidateDocument("one", "01_opening_holdings_2025-01-31.xlsx", "xlsx", True),
            CandidateDocument("four", "04_later_holdings_2025-02-28.xlsx", "xlsx", True),
            CandidateDocument("five", "05_contradictory_custodian_statement_2025-02-28.pdf", "pdf", True),
            CandidateDocument("six", "06_approved_billing_instruction_2025-03-01.pdf", "pdf", True),
        ]
        question = (
            "Compare the February 28 holdings in document 04 with the contradictory custodian statement in document 05. "
            "Does the approved billing instruction in document 06 resolve the conflict?"
        )
        constraint = resolve_document_references(question, documents)
        self.assertEqual([item.document_id for item in constraint.resolved], ["four", "five", "six"])
        self.assertEqual(constraint.problems, ())

    def test_multiple_document_numbers_ranges_and_nonexistent_number_are_explicit(self) -> None:
        documents = [
            CandidateDocument("one", "01_opening_holdings.xlsx", "xlsx", True),
            CandidateDocument("two", "02_transfer.pdf", "pdf", True),
            CandidateDocument("three", "03_assignment.pdf", "pdf", True),
        ]
        constraint = resolve_document_references("Compare documents 01 through 03.", documents)
        self.assertEqual([item.document_id for item in constraint.resolved], ["one", "two", "three"])
        missing = resolve_document_references("What is in document 09?", documents)
        self.assertEqual(missing.resolved, ())
        self.assertEqual(missing.problems, ("No document numbered 09 is in this matter.",))

    def test_numbered_prefix_and_descriptive_title_never_choose_duplicates(self) -> None:
        duplicated_prefix = [
            CandidateDocument("one", "04_later_holdings.xlsx", "xlsx", True),
            CandidateDocument("two", "04_other_holdings.pdf", "pdf", True),
        ]
        constraint = resolve_document_references("Compare document 04.", duplicated_prefix)
        self.assertEqual(constraint.resolved, ())
        self.assertIn("More than one document has the numbered file prefix 04", constraint.problems[0])
        duplicated_title = [
            CandidateDocument("one", "contradictory_custodian_statement_1.pdf", "pdf", True),
            CandidateDocument("two", "contradictory_custodian_statement_2.pdf", "pdf", True),
        ]
        constraint = resolve_document_references("Review the contradictory custodian statement.", duplicated_title)
        self.assertEqual(constraint.resolved, ())
        self.assertIn("More than one document matches the descriptive title", constraint.problems[0])

    def test_descriptive_titles_stable_ids_and_out_of_scope_duplicates_are_resolved_safely(self) -> None:
        documents = [
            CandidateDocument("a0f3b7c1-1111-2222-3333-444444444444", "05_contradictory_custodian_statement.pdf", "pdf", True),
            CandidateDocument("b0f3b7c1-1111-2222-3333-444444444444", "05_contradictory_custodian_statement.pdf", "pdf", False),
            CandidateDocument("billing", "06_approved_billing_instruction.pdf", "pdf", True),
        ]
        by_title = resolve_document_references("Review the approved billing instruction.", documents)
        self.assertEqual([item.document_id for item in by_title.resolved], ["billing"])
        by_id = resolve_document_references(
            "Review a0f3b7c1-1111-2222-3333-444444444444.", documents,
        )
        self.assertEqual([item.document_id for item in by_id.resolved], ["a0f3b7c1-1111-2222-3333-444444444444"])
        # The duplicate filename outside the currently permitted scope does
        # not make the in-scope file ambiguous.
        by_name = resolve_document_references("Use 05_contradictory_custodian_statement.pdf.", documents)
        self.assertEqual([item.document_id for item in by_name.resolved], ["a0f3b7c1-1111-2222-3333-444444444444"])

    def test_exact_filename_narrows_all_documents_scope(self) -> None:
        status = self._ask(self._request("Using 02_Holdings_2023-04-30.xlsx, list all subfunds."))
        self.assertEqual(status["intent"], "inventory")
        self.assertEqual({entity["name"] for entity in status["inventory"]["entities"]},  # type: ignore[index]
                         {"Horizon Credit Fund A", "Horizon Credit Fund B", "Meridian Income Fund C"})
        self.assertEqual({citation["documentId"] for citation in status["citations"]}, {self.april})

    def test_ambiguous_or_missing_dated_documents_ask_for_clarification(self) -> None:
        self._workbook("Holdings_backup_2023-03-28.xlsx", [_row(*HEADER)])
        ambiguous = self._ask(self._request(COMPARISON_QUESTION))
        self.assertEqual(ambiguous["model"], "local-document-resolution")
        self.assertIn("More than one spreadsheet carries the date March 28", str(ambiguous["answer"]))
        missing = self._ask(self._request("Compare holding quantities between the June 15 and July 15 holdings spreadsheets."))
        self.assertIn("No spreadsheet in this matter carries the date June 15", str(missing["answer"]))
        self.assertEqual(self.provider.contexts, [])

    def test_explicit_constraint_restricts_an_ai_answer_to_the_named_file(self) -> None:
        request = self._request("In the spreadsheet dated March 28, what is recorded for Horizon Credit Fund A?")
        preview = self.service.question_preview(request)
        self.assertEqual([item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]], [self.march])
        self._ask(request)
        context = self.provider.contexts[-1]
        self.assertIn("Horizon Credit Fund A", context)
        self.assertNotIn("Jefferies", context)
        self.assertNotIn("02_Holdings", context)

    # -- 6: selected scope ------------------------------------------------

    def test_selected_scope_reports_a_requested_file_it_excludes(self) -> None:
        status = self._ask(self._request(COMPARISON_QUESTION, mode="selected", document_ids=[self.march, self.email]))
        self.assertIn("“02_Holdings_2023-04-30.xlsx” matches the date April 30 in its file name but is not in the current document scope", str(status["answer"]))
        self.assertIsNone(status["holdingsComparison"])
        self.assertEqual(status["documentScope"]["transmittedDocuments"], [])
        self.assertEqual(self.provider.contexts, [])

    # -- 11: relevance gate ----------------------------------------------

    def test_unrelated_pdf_with_keyword_overlap_is_not_sent(self) -> None:
        request = self._request("What is recorded about the transfer for Horizon Credit Fund B?")
        preview = self.service.question_preview(request)
        transmitted = {item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]}
        self.assertTrue(transmitted <= {self.march, self.april})
        self._ask(request)
        self.assertNotIn("Courtney Master Fund", self.provider.contexts[-1])
        self.assertNotIn("Denise", self.provider.contexts[-1])

    def test_unknown_names_get_a_targeted_local_answer(self) -> None:
        status = self._ask(self._request("What happened to Omega Absolute Return Fund?"))
        self.assertEqual(status["model"], "local-relevance-check")
        self.assertIn("“Omega Absolute Return Fund”", str(status["answer"]))
        self.assertEqual(self.provider.contexts, [])

    # -- 12, 13: conversation isolation ----------------------------------

    def test_follow_up_after_unrelated_answer_does_not_reuse_stale_evidence(self) -> None:
        parent = self._ask(self._request("What does Denise ask Jefferies to confirm?"))
        self.assertEqual(parent["status"], "completed")
        self.assertIn("Denise", self.provider.contexts[-1])
        calls = len(self.provider.contexts)
        comparison = self._ask(self._request(COMPARISON_QUESTION, followUpQuestionId=parent["questionId"]))
        self.assertEqual(len(self.provider.contexts), calls)  # local; nothing sent
        self.assertIn("earlier answer was not reused", " ".join(comparison["documentScope"]["notes"]))
        self.assertTrue({citation["documentId"] for citation in comparison["citations"]} <= {self.march, self.april})
        follow_up = self._ask(self._request(
            "In the spreadsheet dated March 28, what is recorded for Horizon Credit Fund A?",
            followUpQuestionId=parent["questionId"],
        ))
        context = self.provider.contexts[-1]
        self.assertNotIn("FOLLOW-UP CONTEXT", context)
        self.assertNotIn("Denise", context)
        self.assertNotIn("Synthetic cited answer", context)  # the earlier answer text is not evidence
        self.assertTrue({citation["documentId"] for citation in follow_up["citations"]} <= {self.march})

    # -- 16: no inferred legal transfer ----------------------------------

    def test_legal_transfer_questions_are_not_answered_from_quantity_changes(self) -> None:
        self.assertEqual(
            MatterReviewService._question_intent("Did the legal claim rights of Horizon Credit Fund A transfer between March 28 and April 30?").kind,
            INTENT_LEGAL_STATUS,
        )
        status = self._ask(self._request(COMPARISON_QUESTION))
        self.assertNotIn("transferred", str(status["answer"]).replace("does not by itself show a trade, settlement, transfer", ""))


if __name__ == "__main__":
    unittest.main()
