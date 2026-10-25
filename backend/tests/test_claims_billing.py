"""Regression tests for claim assignments, approved billing, scenarios and invoice recipients.

The matter follows the shape of acceptance Case 3 (opening holdings, a
claim-rights assignment, a securities transfer, closing holdings, an approved
fee allocation) with different names, references and amounts, so the tests
cannot pass by recognising the pack's literal content.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import pymupdf
from openpyxl import Workbook

from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.billing import BillingService
from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.matter_review import MatterReviewService
from group_claims_backend.services.question_intent import is_contextual_phrase
from group_claims_backend.services.reconciliation import ReconciliationService
from group_claims_backend.storage.service import StorageService


INVESTOR = "Juniper Investments LP"
FUND_A, FUND_B, FUND_C = "Juniper Credit Opportunities A", "Juniper Credit Opportunities B", "Juniper Special Situations C"
ASSIGNMENT_QUESTION = "Who is the documented assignee of claim rights after 5 June 2026, and does it own the securities as a result?"
RECONSTRUCTION_QUESTION = "Reconstruct all three subfund positions at 31 May and 30 June 2026. Distinguish transfer effective date from document issue date."
BILLING_QUESTION = "How should the USD 90,000 approved fee pool be allocated? Who is excluded and why?"
INVOICE_QUESTION = "Who should receive the actual invoices?"
SCENARIO_QUESTION = (
    "Hypothetically, if Fund B's eligible basis were 500,000 rather than 600,000, recalculate allocations. "
    "Clearly mark NOT APPROVED; don't alter the approved records."
)


def _workbook(path: Path, day: str, quantities: tuple[int, int, int]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Holdings"
    sheet.append(("As of", "Investor", "Reported fund", "Custody account", "ISIN", "Nominal", "Custodian"))
    for fund, account, quantity in zip((FUND_A, FUND_B, FUND_C), ("JUN-201", "JUN-202", "JUN-203"), quantities):
        sheet.append((day, INVESTOR, fund, account, "XS6666666666", quantity, "Lakeside Trust"))
    workbook.save(path)


def _pdf(path: Path, *paragraphs: str) -> None:
    document = pymupdf.open()
    document.new_page().insert_textbox(pymupdf.Rect(54, 54, 558, 788), "\n\n".join(paragraphs), fontsize=10)
    document.save(path)
    document.close()


class _Provider:
    def __init__(self) -> None:
        self.contexts: list[str] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=(), *, response_contract=None) -> AnswerResult:
        self.contexts.append(context)
        return AnswerResult("Reconstruction provided.", False, tuple({"sourceReference": key, "evidenceSnippet": "synthetic"} for key in allowed_citation_keys[:2]), "mock", 10, 5, 15)


class ClaimsAndBillingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-claims-billing-")
        root = Path(self.temporary.name)
        sources = root / "sources"
        sources.mkdir()
        self.previous_environment = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        os.environ["OPENAI_API_KEY"] = "synthetic-key"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        _workbook(sources / "01_holdings_2026-05-31.xlsx", "2026-05-31", (400000, 500000, 150000))
        _pdf(sources / "02_claim_assignment.pdf", "Assignment of legal claim rights",
             "Issue date: 2026-06-09\nEffective date: 2026-06-05\nReference: CLAIM-JUN-27",
             "Juniper Investments LP assigns 100 percent of its legal claim rights connected with ISIN XS6666666666 to Harbor Claims SPV Ltd, effective 5 June 2026.",
             "This assignment transfers claim rights only. It does not transfer securities, change custody accounts, or modify the dated balances of any Juniper subfund.")
        _pdf(sources / "03_securities_transfer.pdf", "Securities transfer",
             "Issue date: 2026-06-16\nEffective date: 2026-06-12\nISIN: XS6666666666",
             "Lakeside Trust confirms 100,000 nominal securities transferred from Juniper Credit Opportunities A (account JUN-201) to Juniper Credit Opportunities B (account JUN-202).")
        _workbook(sources / "04_holdings_2026-06-30.xlsx", "2026-06-30", (300000, 600000, 150000))
        _pdf(sources / "05_fee_allocation.pdf", "Approved fee allocation instruction",
             "Issue date: 2026-07-03\nApproved by: Juniper Investments LP billing committee",
             "Billing period: 2026-06-01 through 2026-06-30",
             "Approved fee pool: USD 90,000.00. Allocate pro rata using each participating subfund's approved eligible nominal divided by the total approved eligible nominal.",
             "Participant A: 300,000 eligible nominal. Participant B: 600,000 eligible nominal. Juniper Special Situations C is excluded from this billing period because it is not participating in the relevant litigation.",
             "This instruction authorizes an allocation calculation only; it is not an invoice and does not identify an invoice recipient.")
        self.storage = StorageService(str(root / "app-data"))
        self.assertEqual(self.storage.initialize().status, "ready")
        result = DocumentImportService(self.storage).import_authorized_paths([str(path) for path in sorted(sources.iterdir())])
        self.assertTrue(all(item["ok"] for item in result["documents"]))
        self.document_ids = [str(item["document"]["id"]) for item in result["documents"]]

    def tearDown(self) -> None:
        for key, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def _service(self, provider: _Provider | None = None) -> MatterReviewService:
        if provider is None:
            return MatterReviewService(self.storage)
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
        return MatterReviewService(self.storage, AIAnalysisService(self.storage, provider_factory=lambda: provider))  # type: ignore[arg-type]

    def _ask(self, service: MatterReviewService, question: str, follow_up: str | None = None) -> dict[str, object]:
        request: dict[str, object] = {"matterId": "workspace", "scope": {"mode": "all"}, "question": question}
        if follow_up:
            request["followUpQuestionId"] = follow_up
        preview = service.question_preview(request)
        consented = [item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]]
        started = service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": consented})
        for _ in range(100):
            status = service.question_status({"questionId": started["questionId"]})
            if status["status"] in {"completed", "failed"}:
                return dict(status)
            time.sleep(0.02)
        self.fail("question did not finish")

    def _state(self) -> tuple[int, ...]:
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            return tuple(int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in (
                "reconciliation_candidates", "billing_rule_candidates", "billing_agreements", "billing_calculation_runs",
                "invoices", "invoice_recipients", "holding_positions", "claim_right_assignments", "audit_events",
            ))

    def test_assignment_answer_names_parties_dates_scope_and_securities(self) -> None:
        before = self._state()
        status = self._ask(self._service(), ASSIGNMENT_QUESTION)
        self.assertEqual((status["status"], status["intent"]), ("completed", "legal_status"))
        answer = str(status["answer"])
        self.assertRegex(answer, (
            r"^Juniper Investments LP assigned 100% of its legal claim rights under claim CLAIM-JUN-27 \(ISIN XS6666666666\) "
            r"to Harbor Claims SPV Ltd, effective 5 Jun 2026 \[02_claim_assignment\.pdf, page 1 block \d+\] \[02_claim_assignment\.pdf, page 1 block \d+\]\."
        ))
        self.assertRegex(answer, r"The assignment document is dated 9 Jun 2026 \[02_claim_assignment\.pdf, page 1 block \d+\]; that is its issue date, not the date the assignment took effect\.")
        self.assertRegex(answer, r"does not move securities or change custody balances \[02_claim_assignment\.pdf, page 1 block \d+\]")
        self.assertIn("The holdings records dated 30 Jun 2026 still show the securities in accounts held by Juniper Investments LP: JUN-201 300,000 [04_holdings_2026-06-30.xlsx, Holdings!F2]", answer)
        self.assertIn("Harbor Claims SPV Ltd did not receive securities as a result of the assignment.", answer)
        self.assertIn("It is not a view on the assignment's legal validity or enforceability.", answer)
        self.assertEqual(self._state()[:-1], before[:-1])  # only question bookkeeping may be audited

    def test_reconstruction_distinguishes_effective_and_issue_dates(self) -> None:
        provider = _Provider()
        status = self._ask(self._service(provider), RECONSTRUCTION_QUESTION)
        self.assertEqual(status["intent"], "historical_reconstruction")
        context = provider.contexts[0]
        self.assertIn("31 May 2026: 400,000 documented", context)
        self.assertIn("securities transfer of 100,000 out to Juniper Credit Opportunities B (account JUN-202), effective 12 Jun 2026 (notice dated 16 Jun 2026)", context)
        self.assertIn("→ 300,000 inferred", context)
        self.assertIn("30 Jun 2026: 600,000 documented", context)
        self.assertIn("Claim-right assignment effective 5 Jun 2026", context)

    def test_approved_allocation_uses_the_instruction_with_exact_citations(self) -> None:
        before = self._state()
        status = self._ask(self._service(), BILLING_QUESTION)
        self.assertEqual((status["status"], status["intent"], status["model"]), ("completed", "calculation", "local-billing-instruction"))
        answer = str(status["answer"])
        self.assertRegex(answer, r"Allocation of the approved USD 90,000\.00 fee pool for 1 Jun 2026 – 30 Jun 2026 \[05_fee_allocation\.pdf, page 1 block \d+\]")
        self.assertRegex(answer, rf"• Participant A \({FUND_A}, account JUN-201; its 30 Jun 2026 holding is the same 300,000 \[04_holdings_2026-06-30\.xlsx, Holdings!F2\]\): eligible nominal 300,000 → USD 30,000\.00 \[05_fee_allocation\.pdf, page 1 block \d+\]")
        self.assertRegex(answer, r"• Participant B \(.*\): eligible nominal 600,000 → USD 60,000\.00")
        self.assertRegex(answer, rf"• Excluded: {FUND_C} — not participating in the relevant litigation \[05_fee_allocation\.pdf, page 1 block \d+\]")
        self.assertIn("Total eligible basis 900,000; total allocated USD 90,000.00.", answer)
        self.assertRegex(answer, r"approved by Juniper Investments LP billing committee and is dated 3 Jul 2026")
        self.assertIn("has not been approved on the Billing screen yet", answer)
        for citation in status["citations"]:
            self.assertRegex(citation["sourceReference"], r"^(?:page \d+ block \d+|Holdings![A-Z]+\d+)$")
        self.assertEqual(self._state()[:-1], before[:-1])

    def test_invoice_recipients_are_not_established_and_the_answer_says_why(self) -> None:
        status = self._ask(self._service(), INVOICE_QUESTION)
        answer = str(status["answer"])
        self.assertTrue(answer.startswith("The documents used do not identify who should receive the invoices."))
        self.assertRegex(answer, r"05_fee_allocation\.pdf states: “This instruction authorizes an allocation calculation only; it is not an invoice and does not identify an invoice recipient\.” \[05_fee_allocation\.pdf, page 1 block \d+\]")
        self.assertIn("An assignment of claim rights does not by itself make Harbor Claims SPV Ltd responsible for, or the recipient of, invoices", answer)
        self.assertIn("ClaimFlow holds no issued invoices and no confirmed invoice recipients.", answer)

    def test_hypothetical_basis_is_read_only_and_marked_not_approved(self) -> None:
        service = self._service()
        before = self._state()
        fresh = self._ask(service, SCENARIO_QUESTION)
        allocation = self._ask(service, BILLING_QUESTION)
        follow_up = self._ask(service, SCENARIO_QUESTION, follow_up=str(allocation["questionId"]))
        for status in (fresh, follow_up):
            answer = str(status["answer"])
            self.assertTrue(answer.startswith("NOT APPROVED — HYPOTHETICAL SCENARIO."))
            self.assertIn("changes Participant B's eligible basis from 600,000 to 500,000", answer)
            self.assertIn("eligible nominal 300,000 → USD 33,750.00 (approved: USD 30,000.00)", answer)
            self.assertIn("eligible nominal 500,000 (hypothetical; approved basis 600,000) → USD 56,250.00 (approved: USD 60,000.00)", answer)
            self.assertIn(f"• Excluded: {FUND_C}", answer)
            self.assertIn("Total eligible basis 800,000; total allocated USD 90,000.00.", answer)
            self.assertNotIn("Not found in the documents used", answer)
        self.assertEqual(self._state()[:-1], before[:-1])

    def test_billing_instruction_is_staged_and_an_approved_calculation_links_the_funds(self) -> None:
        billing, reconciliation = BillingService(self.storage), ReconciliationService(self.storage)
        [candidate] = billing.candidates({})["candidates"]
        payload = candidate["payload"]
        self.assertEqual((candidate["status"], payload["currency"], payload["feePool"], payload["documentIssueDate"], payload["periodStart"], payload["periodEnd"]),
                         ("candidate", "USD", "90000.00", "2026-07-03", "2026-06-01", "2026-06-30"))
        self.assertEqual([(item["label"], item.get("eligibleBasis"), item["status"]) for item in payload["participants"]],
                         [("Participant A", "300000", "eligible"), ("Participant B", "600000", "eligible"), (FUND_C, None, "excluded")])
        holdings = [item["id"] for item in reconciliation.candidates({"status": "candidate"})["candidates"] if item["type"] == "holding_snapshot"]
        reconciliation.apply_policy({"candidateIds": holdings, "policy": "reviewed_batch", "acknowledged": True})
        agreement = billing.approve_rule({"candidateId": candidate["id"], "acknowledged": True})
        calculation = billing.preview({"billingAgreementId": agreement["id"]})
        self.assertEqual(calculation["status"], "approved_calculation")
        self.assertEqual([(item["participantLabel"], item.get("allocatedFee"), len(item["holdingRecordIds"])) for item in calculation["allocations"]],
                         [("Participant A", "30000.00", 1), ("Participant B", "60000.00", 1), (FUND_C, None, 1)])
        runs_before = self._state()[3]
        self.assertIn("NOT APPROVED", str(self._ask(self._service(), SCENARIO_QUESTION)["answer"]))
        self.assertEqual(self._state()[3], runs_before)
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            stored = json.loads(connection.execute("SELECT rule_json FROM billing_agreements").fetchone()[0])
        self.assertEqual([item.get("eligibleBasis") for item in stored["participants"]], ["300000", "600000", None])

    def test_instruction_phrases_and_contextual_labels_are_not_document_names(self) -> None:
        self.assertTrue(is_contextual_phrase("NOT APPROVED"))
        self.assertTrue(is_contextual_phrase("Fund B"))
        self.assertTrue(is_contextual_phrase("Participant A"))
        self.assertFalse(is_contextual_phrase("Harbor Claims"))
        self.assertFalse(is_contextual_phrase("SPV"))


if __name__ == "__main__":
    unittest.main()
