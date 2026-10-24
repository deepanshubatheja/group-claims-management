"""Structured pro-rata billing answers in Matter Review.

A billing question used to return the allocation as one paragraph and
silently dropped a subfund the instruction excluded in a sentence.  These
tests build a synthetic matter (an approved instruction, dated holdings, a
contradictory custodian statement and a transfer notice) and check the typed
``billingAllocation`` payload.  The figures are test expectations only; the
mock provider records any call, and none is expected.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.matter_review import AllocationRow, MatterReviewService
from group_claims_backend.storage.service import StorageService

QUESTION = "How much pro-rata is billed for all the subfunds mentioned in these documents?"
INSTRUCTION_BLOCKS = [
    "SYNTHETIC - APPROVED PRO-RATA BILLING INSTRUCTION",
    "Issue date: 1 March 2025\nApproved by: Example Investors LP billing committee\nBilling period\n1 February 2025 to 28 February 2025",
    "Fee pool\nUSD 120,000.00",
    "Allocation rule\neligible nominal amount / total eligible nominal amount",
    "Eligible participant 1\nHorizon Credit Fund A (SF-A): 400,000",
    "Eligible participant 2\nHorizon Credit Fund B (SF-B): 1,000,000",
    "Meridian Income Fund C (SF-C) is excluded because it is not participating in the relevant litigation for\nthis billing period.",
    "Currency rounding must reconcile the allocations to the fee pool. This instruction is a calculation\n"
    "authorization only; it is not an invoice and does not determine legal entitlement.",
]
CUSTODIAN_BLOCKS = [
    "SYNTHETIC - CUSTODIAN POSITION STATEMENT",
    "Issue date: 10 March 2025\nStatement as-of date: 28 February 2025\nCustodian: Example Custody Bank",
    "For account CUST-02, Horizon Credit Fund B (SF-B), the recorded holding\nof ISIN XS0123456789 is 900,000 nominal units as of 28 February 2025.",
]
TRANSFER_BLOCKS = [
    "SYNTHETIC - PARTIAL SECURITIES TRANSFER NOTICE",
    "Effective date: 15 February 2025",
    "Example Custody Bank confirms a partial securities transfer of 200,000 nominal units of ISIN XS0123456789, from Horizon\n"
    "Credit Fund A (SF-A), account CUST-01, to Horizon Credit Fund B (SF-B), account CUST-02. The holding moved.",
]
_ENVIRONMENT = ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")


class _RecordingProvider:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=()) -> AnswerResult:
        self.questions.append(question)
        return AnswerResult("Synthetic.", False, ({"sourceReference": allowed_citation_keys[0], "evidenceSnippet": "x"},), "mock", 1, 1, 2)


def _holdings_workbook(path: Path, as_of: datetime, rows: list[tuple[str, str, int]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Holdings"
    sheet["A1"] = "Synthetic holdings statement"
    sheet["A3"], sheet["B3"] = "Statement date", as_of
    sheet["A5"], sheet["B5"] = "Investor", "Example Investors LP"
    for column, heading in zip("ABCDEFG", ("Account", "Subfund", "Security ISIN", "Currency", "Nominal amount", "As-of date", "Notes")):
        sheet[f"{column}7"] = heading
    for row, (account, subfund, quantity) in enumerate(rows, start=8):
        for column, value in zip("ABCDEFG", (account, subfund, "XS0123456789", "USD", quantity, as_of, "Synthetic")):
            sheet[f"{column}{row}"] = value
    workbook.save(path)


class BillingAllocationTests(unittest.TestCase):
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
        self.sources = Path(self.temporary.name) / "sources"
        self.sources.mkdir()

    def tearDown(self) -> None:
        for key, value in self._previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _workbook(self, filename: str, as_of: datetime, rows: list[tuple[str, str, int]]) -> str:
        path = self.sources / filename
        _holdings_workbook(path, as_of, rows)
        original = path.read_bytes()
        imported = DocumentImportService(self.storage).import_authorized_paths([str(path)])
        self.assertEqual(path.read_bytes(), original)
        return str(imported["documents"][0]["document"]["id"])

    def _pdf(self, filename: str, blocks: list[str]) -> str:
        document_id, extraction_id, page_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        stamp = "2026-01-01T00:00:00+00:00"
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO source_documents (id, original_filename, managed_relative_path, content_sha256, file_size_bytes, "
                "imported_at, file_format, processing_status) VALUES (?, ?, ?, ?, 1, ?, 'pdf', 'parsed')",
                (document_id, filename, f"documents/{document_id}", (document_id.replace("-", "") * 2)[:64], stamp),
            )
            connection.execute(
                "INSERT INTO document_extractions (id, source_document_id, parser_version, status, started_at, completed_at) "
                "VALUES (?, ?, 'synthetic', 'parsed', ?, ?)", (extraction_id, document_id, stamp, stamp),
            )
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

    def _matter(self, *, extra_subfund: bool = False) -> dict[str, str]:
        later = [("CUST-01", "Horizon Credit Fund A (SF-A)", 400_000), ("CUST-02", "Horizon Credit Fund B (SF-B)", 1_000_000),
                 ("CUST-03", "Meridian Income Fund C (SF-C)", 500_000)]
        if extra_subfund:
            later.append(("CUST-04", "Atlas Opportunity Fund D (SF-D)", 250_000))
        return {
            # A different-date record that disagrees with the instruction is not a conflict.
            "opening": self._workbook("01_opening_holdings_2025-01-31.xlsx", datetime(2025, 1, 31), [
                ("CUST-01", "Horizon Credit Fund A (SF-A)", 600_000), ("CUST-02", "Horizon Credit Fund B (SF-B)", 800_000),
                ("CUST-03", "Meridian Income Fund C (SF-C)", 500_000),
            ]),
            "transfer": self._pdf("02_partial_transfer_effective_2025-02-15.pdf", TRANSFER_BLOCKS),
            "later": self._workbook("04_later_holdings_2025-02-28.xlsx", datetime(2025, 2, 28), later),
            "custodian": self._pdf("05_contradictory_custodian_statement_2025-02-28.pdf", CUSTODIAN_BLOCKS),
            "instruction": self._pdf("06_approved_billing_instruction_2025-03-01.pdf", INSTRUCTION_BLOCKS),
        }

    def _ask(self, question: str = QUESTION, document_ids: list[str] | None = None) -> dict[str, object]:
        scope: dict[str, object] = {"mode": "all"} if document_ids is None else {"mode": "selected", "documentIds": document_ids}
        request = {"matterId": "workspace", "scope": scope, "question": question}
        preview = self.service.question_preview(request)
        self.assertTrue(preview["localOnly"])
        self.assertEqual(preview["documentScope"]["transmittedDocuments"], [])
        return self.service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": []})

    @staticmethod
    def _participant(billing: dict, identifier: str) -> dict:
        return next(item for item in billing["participants"] if item["identifier"] == identifier)

    def test_allocation_is_structured_with_exclusion_and_unresolved_conflict(self) -> None:
        ids = self._matter()
        result = self._ask()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model"], "local-document-calculation")
        self.assertEqual(self.provider.questions, [])
        billing = result["billingAllocation"]
        self.assertEqual(billing["calculationStatus"], "complete")
        self.assertEqual(billing["resultType"], "provisional_allocation")
        self.assertEqual(billing["invoiceStatus"], "not_issued")
        self.assertEqual(billing["currency"], "USD")
        self.assertEqual(billing["feePool"]["amount"], "120000.00")
        self.assertEqual(billing["feePool"]["source"]["reference"], "page 1 block 2")
        self.assertEqual(billing["period"]["text"], "1 February 2025 to 28 February 2025")
        self.assertEqual(billing["rule"]["source"]["reference"], "page 1 block 3")
        self.assertEqual(billing["approvedBy"]["text"], "Example Investors LP billing committee")
        # The instruction names no investor group, so none is invented.
        self.assertIsNone(billing["investorGroup"])
        self.assertEqual(billing["totals"], {"eligibleAmount": "1400000", "allocatedAmount": "120000.00", "remainder": "0.00", "sharePercent": "100.0000"})

        fund_a, fund_b, fund_c = (self._participant(billing, key) for key in ("SF-A", "SF-B", "SF-C"))
        self.assertEqual((fund_a["eligibleAmount"], fund_a["allocatedAmount"], fund_a["sharePercent"]), ("400000", "34285.71", "28.5714"))
        self.assertEqual((fund_b["eligibleAmount"], fund_b["allocatedAmount"], fund_b["sharePercent"]), ("1000000", "85714.29", "71.4286"))
        self.assertEqual(fund_a["share"], "0.2857142857")
        self.assertEqual(fund_b["unroundedAmount"], "85714.285714")
        self.assertEqual(fund_b["roundingAdjustment"], "0.01")
        self.assertEqual(fund_a["source"]["reference"], "page 1 block 4")
        # The sentence-form exclusion is listed, not silently omitted.
        self.assertEqual(fund_c["name"], "Meridian Income Fund C")
        self.assertEqual(fund_c["status"], "excluded")
        self.assertEqual(fund_c["allocatedAmount"], "0.00")
        self.assertIsNone(fund_c["eligibleAmount"])
        self.assertIn("not participating in the relevant litigation", fund_c["reason"])
        self.assertEqual(fund_c["source"]["reference"], "page 1 block 6")

        conflicts = [check for check in billing["crossChecks"] if check["status"] == "conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["identifier"], "SF-B")
        self.assertEqual((conflicts[0]["instructionAmount"], conflicts[0]["recordedAmount"]), ("1000000", "900000"))
        self.assertEqual(conflicts[0]["source"]["documentId"], ids["custodian"])
        self.assertEqual(conflicts[0]["asOfDate"], "2025-02-28")
        consistent = {(check["identifier"], check["source"]["reference"]) for check in billing["crossChecks"] if check["status"] == "consistent"}
        self.assertEqual(consistent, {("SF-A", "Holdings!E8"), ("SF-B", "Holdings!E9")})
        # The opening workbook (another date) and the transfer notice (two participants) are not conflicts.
        self.assertFalse(any(check["source"]["documentId"] in {ids["opening"], ids["transfer"]} for check in billing["crossChecks"]))
        self.assertTrue(billing["issuanceBlocked"])
        self.assertEqual(billing["crossCheck"]["comparisonDate"], "2025-02-28")

        cited = {(citation["documentId"], citation["sourceReference"]) for citation in result["citations"]}
        for reference in ("page 1 block 2", "page 1 block 3", "page 1 block 4", "page 1 block 5", "page 1 block 6"):
            self.assertIn((ids["instruction"], reference), cited)
        self.assertIn((ids["custodian"], "page 1 block 2"), cited)
        self.assertIn("Meridian Income Fund C (SF-C) is excluded", str(result["answer"]))
        self.assertIn("Unresolved discrepancy", str(result["answer"]))

    def test_amounts_are_the_existing_allocation_unchanged(self) -> None:
        self._matter()
        billing = self._ask()["billingAllocation"]
        expected = MatterReviewService._allocate_pro_rata(Decimal("120000.00"), [
            AllocationRow("document-1", "Horizon Credit Fund A (SF-A)", Decimal("400000")),
            AllocationRow("document-2", "Horizon Credit Fund B (SF-B)", Decimal("1000000")),
        ])
        eligible = [item for item in billing["participants"] if item["status"] == "eligible"]
        self.assertEqual([item["allocatedAmount"] for item in eligible], [format(fee, "f") for _row, fee in expected])

    def test_a_listed_subfund_not_named_in_the_instruction_is_undetermined(self) -> None:
        self._matter(extra_subfund=True)
        billing = self._ask()["billingAllocation"]
        fund_d = self._participant(billing, "SF-D")
        self.assertEqual(fund_d["status"], "undetermined")
        self.assertIsNone(fund_d["allocatedAmount"])
        self.assertIsNone(fund_d["sharePercent"])
        self.assertIn("not named in the billing instruction", fund_d["reason"])
        self.assertEqual(fund_d["source"]["reference"], "Holdings!B11")
        # The pool is still shared only among the instruction's eligible participants.
        self.assertEqual(billing["totals"]["allocatedAmount"], "120000.00")

    def test_conflicting_evidence_outside_the_selected_scope_is_not_read(self) -> None:
        ids = self._matter()
        billing = self._ask(document_ids=[ids["instruction"], ids["later"]])["billingAllocation"]
        self.assertEqual([check["status"] for check in billing["crossChecks"]], ["consistent", "consistent"])
        self.assertFalse(billing["issuanceBlocked"])
        self.assertNotIn(ids["custodian"], {check["source"]["documentId"] for check in billing["crossChecks"]})

    def test_an_instruction_without_a_fee_pool_is_incomplete_and_never_calculated(self) -> None:
        blocks = [block for block in INSTRUCTION_BLOCKS if not block.startswith("Fee pool")]
        document_id = self._pdf("06_approved_billing_instruction_2025-03-01.pdf", blocks)
        result = self._ask(document_ids=[document_id])
        self.assertEqual(self.provider.questions, [])
        billing = result["billingAllocation"]
        self.assertEqual(billing["calculationStatus"], "incomplete")
        self.assertIsNone(billing["totals"])
        self.assertIsNone(billing["feePool"])
        self.assertEqual(billing["participants"], [])
        self.assertIn("the fee pool amount and currency", billing["missing"])
        self.assertTrue(billing["issuanceBlocked"])
        self.assertTrue(str(result["answer"]).startswith("No allocation was calculated"))

    def test_two_approved_instructions_are_not_chosen_between(self) -> None:
        first = self._pdf("06_approved_billing_instruction_2025-03-01.pdf", INSTRUCTION_BLOCKS)
        second = self._pdf("07_approved_billing_instruction_2025-04-01.pdf", [
            block.replace("120,000.00", "90,000.00") for block in INSTRUCTION_BLOCKS
        ])
        billing = self._ask(document_ids=[first, second])["billingAllocation"]
        self.assertEqual(billing["calculationStatus"], "incomplete")
        self.assertIsNone(billing["totals"])
        self.assertIn("does not choose between billing instructions", billing["issues"][0])

    def test_rounding_reconciles_equal_shares_exactly(self) -> None:
        document_id = self._pdf("approved.pdf", [
            "Approved billing instruction\nBilling period\n1 January 2025 to 31 January 2025",
            "Fee pool\nEUR 100.00",
            "Allocation rule\neligible nominal amount / total eligible nominal amount",
            "Eligible participant 1\nFund One (F-1): 1",
            "Eligible participant 2\nFund Two (F-2): 1",
            "Eligible participant 3\nFund Three (F-3): 1",
        ])
        billing = self._ask("Calculate the pro-rata allocation.", document_ids=[document_id])["billingAllocation"]
        self.assertEqual(billing["currency"], "EUR")
        self.assertEqual([item["allocatedAmount"] for item in billing["participants"]], ["33.34", "33.33", "33.33"])
        self.assertEqual([item["roundingAdjustment"] for item in billing["participants"]], ["0.01", "0.00", "0.00"])
        self.assertEqual(billing["totals"]["remainder"], "0.00")
        self.assertEqual(billing["crossChecks"], [])
        self.assertIn("No other document in scope records holdings", billing["crossCheck"]["limitations"][0])

    def test_non_allocation_billing_questions_keep_their_existing_path(self) -> None:
        ids = self._matter()
        request = {"matterId": "workspace", "scope": {"mode": "selected", "documentIds": [ids["instruction"]]},
                   "question": "Do you give the same output when I mention pro rata?"}
        self.assertFalse(self.service.question_preview(request)["localOnly"])


if __name__ == "__main__":
    unittest.main()
