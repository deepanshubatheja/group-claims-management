"""Regression tests for alias resolution and historical reconstruction.

The fixtures follow the shape of the Entity Linkage acceptance pack (opening
positions, an alias confirmation, a transfer notice without opening balances,
closing positions) but use different names, accounts, amounts and wording, so
these tests cannot pass by recognising the pack's literal content.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path

import pymupdf
from openpyxl import Workbook

from group_claims_backend.services.ai import AIService, AnswerResult
from group_claims_backend.services.ai_analysis import AIAnalysisService
from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.matter_review import MatterReviewService
from group_claims_backend.services.pdf_fields import extract_page_aliases
from group_claims_backend.services.reconciliation import ReconciliationService
from group_claims_backend.storage.service import StorageService


ALIAS_QUESTION = "Which fund names refer to the same underlying fund? Include accounts and citations."
RECONSTRUCTION_QUESTION = (
    "Reconstruct the holdings of all three funds from 31 Jan through 28 Feb 2026; explain the transfer and cite the effective date."
)
HEADERS = ("As of", "Investor", "Reported fund", "Custody account", "ISIN", "Nominal", "Custodian")
INVESTOR = "Linden Holdings LP"
SERIES_ONE, SERIES_TWO, RESERVE = "Linden Global Bond Fund - Series 1", "Linden Global Bond Fund - Series 2", "Linden Cash Reserve"


def _workbook(path: Path, rows: list[tuple[object, ...]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Positions"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def _pdf(path: Path, *paragraphs: str) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(54, 54, 558, 788), "\n\n".join(paragraphs), fontsize=10)
    document.save(path)
    document.close()


class _Provider:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def answer_question(self, question: str, context: str, allowed_citation_keys=(), *, response_contract=None) -> AnswerResult:
        self.calls.append((question, context, tuple(allowed_citation_keys)))
        if self.unknown:
            return AnswerResult(None, True, (), "mock-provider", 10, 5, 15)
        return AnswerResult(
            "Reconstruction provided.", False,
            tuple({"sourceReference": key, "evidenceSnippet": "synthetic"} for key in allowed_citation_keys[:2]),
            "mock-provider", 10, 5, 15,
        )


class EntityLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-entity-linkage-")
        self.root = Path(self.temporary.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.previous_environment = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        os.environ["OPENAI_API_KEY"] = "synthetic-key"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        self.storage = StorageService(str(self.root / "app-data"))
        self.assertEqual(self.storage.initialize().status, "ready")
        self.imports = DocumentImportService(self.storage)
        self.reconciliation = ReconciliationService(self.storage)

    def tearDown(self) -> None:
        for key, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    # Fixtures ----------------------------------------------------------------

    def _opening(self) -> Path:
        path = self.sources / "01_positions_2026-01-31.xlsx"
        _workbook(path, [
            ("2026-01-31", INVESTOR, SERIES_ONE, "LIN-310", "XS4444444444", 700000, "Northgate Custody"),
            ("2026-01-31", INVESTOR, SERIES_TWO, "LIN-320", "XS4444444444", 200000, "Northgate Custody"),
            ("2026-01-31", INVESTOR, RESERVE, "LIN-330", "XS4444444444", 100000, "Northgate Custody"),
        ])
        return path

    def _aliases(self, series_one_account: str = "LIN-310") -> Path:
        path = self.sources / "02_alias_statement.pdf"
        _pdf(path, "Fund naming statement", "Issued: 2026-02-02\nInvestor: Linden Holdings LP",
             f"For reporting purposes, 'LGBF S1' and 'LG-1' refer to the same fund as '{SERIES_ONE}' in custody account {series_one_account}.",
             f"'LGBF S2' is an abbreviation for {SERIES_TWO} for custody account LIN-320. The two series are separate funds.")
        return path

    def _transfer(self) -> Path:
        path = self.sources / "03_transfer_notice.pdf"
        _pdf(path, "Securities transfer advice", "Issue date: 2026-02-13\nEffective date: 2026-02-10\nISIN: XS4444444444",
             "Northgate Custody confirms transfer of 150,000 nominal from LG-1, custody account LIN-310, to LGBF S2, custody account LIN-320.",
             "Opening balances are not restated in this notice. No legal claim rights are assigned by this notice.")
        return path

    def _closing(self) -> Path:
        path = self.sources / "04_positions_2026-02-28.xlsx"
        _workbook(path, [
            ("2026-02-28", INVESTOR, "LGBF S1", "LIN-310", "XS4444444444", 550000, "Northgate Custody"),
            ("2026-02-28", INVESTOR, "LGBF S2", "LIN-320", "XS4444444444", 350000, "Northgate Custody"),
            ("2026-02-28", INVESTOR, RESERVE, "LIN-330", "XS4444444444", 100000, "Northgate Custody"),
        ])
        return path

    def _import(self, *paths: Path) -> list[str]:
        result = self.imports.import_authorized_paths([str(path) for path in paths])
        self.assertTrue(all(item["ok"] for item in result["documents"]))
        return [str(item["document"]["id"]) for item in result["documents"]]

    def _batch(self) -> dict[str, object]:
        ids = [str(item["id"]) for item in self.reconciliation.candidates({"status": "candidate"})["candidates"]]
        return dict(self.reconciliation.apply_policy({"candidateIds": ids, "policy": "reviewed_batch", "acknowledged": True}))

    def _matter(self, provider: _Provider | None = None) -> MatterReviewService:
        if provider is None:
            return MatterReviewService(self.storage)
        AIService(self.storage).set_authorization({"authorized": True, "acknowledged": True})
        return MatterReviewService(self.storage, AIAnalysisService(self.storage, provider_factory=lambda: provider))  # type: ignore[arg-type]

    @staticmethod
    def _request(question: str) -> dict[str, object]:
        return {"matterId": "workspace", "scope": {"mode": "all"}, "question": question}

    def _ask(self, service: MatterReviewService, question: str) -> dict[str, object]:
        request = self._request(question)
        preview = service.question_preview(request)
        consented = [item["documentId"] for item in preview["documentScope"]["transmittedDocuments"]]
        started = service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": consented})
        for _ in range(100):
            status = service.question_status({"questionId": started["questionId"]})
            if status["status"] in {"completed", "failed"}:
                return dict(status)
            time.sleep(0.02)
        self.fail("question did not finish")

    def _count(self, query: str, values: tuple[object, ...] = ()) -> int:
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            return int(connection.execute(query, values).fetchone()[0])

    # Alias resolution ------------------------------------------------------------

    def test_alias_question_groups_names_by_account_with_documented_citations(self) -> None:
        document_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        status = self._ask(self._matter(), ALIAS_QUESTION)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["intent"], "identity_resolution")
        answer = str(status["answer"])
        series_one = answer[answer.index(f"{SERIES_ONE} — account LIN-310"):answer.index(f"{SERIES_TWO} — account LIN-320")]
        for name in ("LGBF S1", "LG-1"):
            self.assertIn(f"“{name}” refers to {SERIES_ONE} for account LIN-310 [02_alias_statement.pdf, page 1 block", series_one)
        self.assertNotIn("LGBF S2", series_one)  # the two series are never merged
        self.assertIn(f"“LGBF S2” refers to {SERIES_TWO} for account LIN-320", answer)
        self.assertIn(f"{RESERVE} — account LIN-330", answer)
        self.assertIn("[01_positions_2026-01-31.xlsx, Positions!C2]", answer)
        cited = {(citation["documentId"], citation["sourceReference"]) for citation in status["citations"]}
        self.assertIn(document_ids[1], {document for document, _reference in cited})
        self.assertTrue(all(reference.startswith(("page ", "Positions!")) for _document, reference in cited))

    def test_names_sharing_an_account_without_a_statement_are_not_merged(self) -> None:
        self._import(self._opening(), self._transfer(), self._closing())
        answer = str(self._ask(self._matter(), ALIAS_QUESTION)["answer"])
        self.assertIn(f"“LGBF S1” is recorded under this account [04_positions_2026-02-28.xlsx, Positions!C2], but no document used states it is the same fund as {SERIES_ONE}.", answer)
        self.assertNotIn("Documented as the same fund", answer)
        batch = self._batch()
        self.assertIn("identity_link_requires_review", {item["reason"] for item in batch["reviewRequired"]})
        self.assertEqual(self._count("SELECT COUNT(*) FROM identity_aliases WHERE alias_text = 'LGBF S1'"), 0)
        self.assertGreaterEqual(self._count("SELECT COUNT(*) FROM identity_link_questions WHERE status = 'open' AND observed_text = 'LGBF S1'"), 1)

    def test_alias_statement_for_a_different_account_is_not_applied(self) -> None:
        self._import(self._opening(), self._aliases(series_one_account="LIN-399"), self._closing())
        batch = self._batch()
        self.assertIn("identity_link_requires_review", {item["reason"] for item in batch["reviewRequired"]})
        self.assertEqual(self._count("SELECT COUNT(*) FROM identity_aliases WHERE alias_text = 'LGBF S1'"), 0)

    # Transaction linking and persistence ------------------------------------------

    def test_transfer_links_canonical_funds_with_provenance(self) -> None:
        document_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        batch = self._batch()
        self.assertEqual(batch["reviewRequired"], [])
        self.assertEqual(len(batch["confirmed"]), 7)
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM investment_vehicles").fetchone()[0], 3)
            transfer = connection.execute(
                "SELECT t.effective_date, t.quantity, t.source_document_id, source.external_reference AS source_account, destination.external_reference AS destination_account, "
                "source_fund.name AS source_fund, destination_fund.name AS destination_fund, s.isin FROM securities_transfers t "
                "JOIN accounts source ON source.id = t.from_account_id JOIN accounts destination ON destination.id = t.to_account_id "
                "JOIN investment_vehicles source_fund ON source_fund.id = source.investment_vehicle_id "
                "JOIN investment_vehicles destination_fund ON destination_fund.id = destination.investment_vehicle_id "
                "JOIN securities s ON s.id = t.security_id"
            ).fetchone()
            self.assertEqual(
                tuple(transfer),
                ("2026-02-10", "150000", document_ids[2], "LIN-310", "LIN-320", SERIES_ONE, SERIES_TWO, "XS4444444444"),
            )
            aliases = {
                (row["alias_text"], row["source_document_id"]) for row in connection.execute(
                    "SELECT a.alias_text, a.source_document_id FROM identity_aliases a JOIN investment_vehicles v ON v.id = a.target_id WHERE v.name = ?",
                    (SERIES_ONE,),
                )
            }
            self.assertIn(("LGBF S1", document_ids[3]), aliases)
            self.assertIn(("LG-1", document_ids[2]), aliases)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_right_assignments").fetchone()[0], 0)
            # Original names, accounts and dates are preserved on each record.
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM identity_alias_evidence WHERE source_reference LIKE 'page 1 block %' AND account_reference IN ('LIN-310', 'LIN-320')"
            ).fetchone()[0], 3)
        reconstructed = {item["account"]: (item["quantity"], item["reconstructionState"]) for item in self.reconciliation.holdings_as_of({"asOfDate": "2026-02-10"})["positions"]}
        self.assertEqual(reconstructed, {"LIN-310": ("550000", "reconstructed"), "LIN-320": ("350000", "reconstructed"), "LIN-330": ("100000", "inferred")})

    # Historical reconstruction ---------------------------------------------------

    def test_reconstruction_question_uses_timeline_workflow_across_all_documents(self) -> None:
        document_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        provider = _Provider()
        service = self._matter(provider)
        preview = service.question_preview(self._request(RECONSTRUCTION_QUESTION))
        self.assertFalse(preview["localOnly"])
        self.assertEqual(preview["scope"]["documentsWithEvidence"], 4)
        self.assertTrue(preview["scope"]["comparisonCoverageComplete"])
        self.assertEqual(preview["scope"]["directHoldingSourcesIncluded"], 6)
        status = self._ask(service, RECONSTRUCTION_QUESTION)
        self.assertEqual(status["intent"], "historical_reconstruction")
        context = provider.calls[0][1]
        self.assertIn("DERIVED LOCAL RECONSTRUCTION", context)
        for expected in (
            f"{SERIES_ONE} — account LIN-310", "31 Jan 2026: 700,000 documented",
            "securities transfer of 150,000 out to", "effective 10 Feb 2026 (notice dated 13 Feb 2026)",
            "→ 550,000 inferred", "28 Feb 2026: 550,000 documented", "consistent with the inferred balance",
            "→ 350,000 inferred", f"{RESERVE} — account LIN-330", "unchanged from the previous documented balance",
            "“LGBF S1” and “LG-1” refer to " + SERIES_ONE,
        ):
            self.assertIn(expected, context)
        self.assertNotIn("Fund A, Fund B", context)
        self.assertEqual(set(status["execution"]["documentsIncluded"]), set(document_ids))

    def test_reconstruction_fallback_is_cited_including_the_effective_date(self) -> None:
        document_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        status = self._ask(self._matter(_Provider(unknown=True)), RECONSTRUCTION_QUESTION)
        self.assertEqual(status["status"], "completed")
        self.assertTrue(status["execution"]["fallbackUsed"])
        answer = str(status["answer"])
        self.assertIn("calculated locally from the cited extracted records", answer)
        self.assertIn("→ 550,000 inferred", answer)
        self.assertRegex(answer, r"effective 10 Feb 2026 \(notice dated 13 Feb 2026\) \[03_transfer_notice\.pdf \(page 1 block \d+\)\] \[03_transfer_notice\.pdf \(page 1 block \d+\)\]")
        cited_documents = {citation["documentId"] for citation in status["citations"]}
        self.assertTrue({document_ids[0], document_ids[2], document_ids[3]} <= cited_documents)
        transfer_blocks = {citation["sourceReference"] for citation in status["citations"] if citation["documentId"] == document_ids[2]}
        self.assertEqual(len(transfer_blocks), 2)  # the movement sentence and the effective-date line

    # Ambiguous names and account changes -----------------------------------------

    def test_same_fund_name_for_different_investors_stays_two_funds(self) -> None:
        path = self.sources / "positions.xlsx"
        _workbook(path, [
            ("2026-03-31", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-500", "XS5555555555", 400000, "Eastbank Custody"),
            ("2026-03-31", "Sable Asset Management Ltd", "Rowan Growth Fund", "SAB-900", "XS5555555555", 250000, "Eastbank Custody"),
        ])
        self._import(path)
        self.assertEqual(self._batch()["reviewRequired"], [])
        self.assertEqual(self._count("SELECT COUNT(*) FROM investment_vehicles WHERE name = 'Rowan Growth Fund'"), 2)
        answer = str(self._ask(self._matter(), "Are both records called Rowan Growth Fund the same fund?")["answer"])
        self.assertIn("The name “Rowan Growth Fund” is used by separate funds: account ROW-500 held by Rowan Family Office Ltd [positions.xlsx, Positions!B2]; account SAB-900 held by Sable Asset Management Ltd [positions.xlsx, Positions!B3].", answer)
        self.assertIn("They belong to different investors and no document used links them, so they are kept separate.", answer)

    def test_account_change_is_not_a_transfer_and_moves_no_holdings(self) -> None:
        opening = self.sources / "positions_march.xlsx"
        _workbook(opening, [("2026-03-31", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-500", "XS5555555555", 400000, "Eastbank Custody")])
        notice = self.sources / "account_change.pdf"
        _pdf(notice, "Account change notice", "Issue date: 2026-04-06\nEffective date: 2026-04-05",
             "Custody account ROW-500 was replaced by ROW-501 for Rowan Family Office Ltd's Rowan Growth Fund. No securities were transferred.")
        closing = self.sources / "positions_april.xlsx"
        _workbook(closing, [("2026-04-30", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-501", "XS5555555555", 400000, "Eastbank Custody")])
        notice_id = self._import(opening, notice, closing)[1]
        self.assertEqual(self._count("SELECT COUNT(*) FROM reconciliation_candidates WHERE source_document_id = ?", (notice_id,)), 0)
        self.assertEqual(self._batch()["reviewRequired"], [])
        self.assertEqual(self._count("SELECT COUNT(*) FROM securities_transfers"), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM accounts"), 2)
        self.assertEqual(self._count("SELECT COUNT(DISTINCT investment_vehicle_id) FROM accounts"), 1)  # same investor, same fund name
        positions = {item["account"]: item["quantity"] for item in self.reconciliation.holdings_as_of({"asOfDate": "2026-04-30"})["positions"]}
        self.assertEqual(positions, {"ROW-500": "400000", "ROW-501": "400000"})  # the old account is not emptied by inference

    # PDF aliases and XLSX holdings ------------------------------------------------

    def test_pdf_alias_statements_in_both_phrasings_are_extracted(self) -> None:
        statements = extract_page_aliases([
            (0, f"For reporting purposes, 'LGBF S1' and 'LG-1' refer to the same fund as '{SERIES_ONE}' in custody account LIN-310."),
            (1, f"'LGBF S2' is an abbreviation for {SERIES_TWO} for custody account LIN-320."),
        ])
        self.assertEqual(
            [(item.aliases, item.canonical, item.account, item.block_indexes) for item in statements],
            [(("LGBF S1", "LG-1"), SERIES_ONE, "LIN-310", (0,)), (("LGBF S2",), SERIES_TWO, "LIN-320", (1,))],
        )

    def test_xlsx_holdings_without_issue_date_keep_investor_column_and_row_provenance(self) -> None:
        document_id = self._import(self._opening())[0]
        rows = self.reconciliation.candidates({"status": "candidate"})["candidates"]
        self.assertEqual(len(rows), 3)
        first = next(row for row in rows if row["payload"]["account"] == "LIN-310")
        self.assertEqual(first["payload"], {
            "investor": INVESTOR, "subfund": SERIES_ONE, "account": "LIN-310", "isin": "XS4444444444", "quantity": "700000", "positionDate": "2026-01-31",
        })
        self.assertIsNone(first["documentIssueDate"])  # not stated, never substituted
        self.assertEqual((first["sourceDocumentId"], first["sourceReference"]), (document_id, "Positions!A2:F2"))

    # Multi-matter isolation and date scope -----------------------------------------

    def _other_matters(self) -> list[str]:
        later = self.sources / "other_positions_2026-04-30.xlsx"
        _workbook(later, [
            ("2026-04-30", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-500", "XS5555555555", 400000, "Eastbank Custody"),
            ("2026-04-30", "Rowan Family Office Ltd", "Rowan Value Fund", "ROW-600", "XS5555555555", 100000, "Eastbank Custody"),
        ])
        notice = self.sources / "other_transfer.pdf"
        _pdf(notice, "Securities transfer advice", "Issue date: 2026-04-15\nEffective date: 2026-04-12\nISIN: XS5555555555",
             "Eastbank Custody confirms transfer of 50,000 nominal from Rowan Growth Fund, account ROW-500, to Rowan Value Fund, account ROW-600.")
        return self._import(later, notice)

    def test_reconstruction_excludes_other_matters_outside_the_question_dates(self) -> None:
        case_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        other_ids = self._other_matters()
        provider = _Provider()
        service = self._matter(provider)
        for question in (RECONSTRUCTION_QUESTION, RECONSTRUCTION_QUESTION.replace("all three funds", "the three Linden funds")):
            preview = service.question_preview(self._request(question))
            self.assertEqual(preview["scope"]["documentsSelected"], 4)
            self.assertTrue(preview["scope"]["comparisonCoverageComplete"])
            self.assertTrue(any(note.startswith("Not used: 2 selected documents") for note in preview["documentScope"]["notes"]))
        status = self._ask(service, RECONSTRUCTION_QUESTION)
        context = provider.calls[0][1]
        self.assertNotIn("Rowan", context)
        self.assertNotIn("ROW-500", context)
        self.assertIn("→ 550,000 inferred", context)
        self.assertEqual(set(status["execution"]["documentsIncluded"]), set(case_ids))
        self.assertFalse({citation["documentId"] for citation in status["citations"]} & set(other_ids))

    def test_identity_question_naming_a_fund_family_excludes_other_matters(self) -> None:
        self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        self._other_matters()
        answer = str(self._ask(self._matter(), "Which Linden fund names refer to the same underlying fund? Include accounts and citations.")["answer"])
        self.assertIn(SERIES_ONE, answer)
        self.assertNotIn("Rowan", answer)
        self.assertIn("Records for other funds or dates in the selected documents were left out.", answer)
        self.assertNotIn("No holdings, transfer, assignment or alias record was extracted from", answer)

    def test_window_starting_after_the_opening_uses_the_earlier_snapshot_and_transfer(self) -> None:
        self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        provider = _Provider()
        self._ask(self._matter(provider), "Reconstruct the Linden holdings from 15 Feb through 28 Feb 2026, including the transfer and its effective date.")
        context = provider.calls[0][1]
        self.assertIn("31 Jan 2026: 700,000 documented", context)  # opening balance before the window
        self.assertIn("→ 550,000 inferred", context)

    # Missing evidence --------------------------------------------------------------

    def test_without_an_opening_balance_no_balance_is_inferred(self) -> None:
        self._import(self._aliases(), self._transfer(), self._closing())
        status = self._ask(self._matter(_Provider(unknown=True)), RECONSTRUCTION_QUESTION)
        answer = str(status["answer"])
        self.assertIn("no earlier documented balance, so no balance is inferred", answer)
        self.assertNotIn("700,000", answer)
        self.assertIn("28 Feb 2026: 550,000 documented", answer)

    # Citations ----------------------------------------------------------------------

    def test_answers_never_show_a_missing_source_placeholder_and_every_citation_resolves(self) -> None:
        document_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        other_ids = self._other_matters()
        for question, provider in ((RECONSTRUCTION_QUESTION, _Provider(unknown=True)), (ALIAS_QUESTION, None)):
            status = self._ask(self._matter(provider), question)
            self.assertNotIn("source not included", str(status["answer"]))
            self.assertTrue(status["citations"])
            for citation in status["citations"]:
                self.assertIn(citation["documentId"], set(document_ids) | set(other_ids))
                self.assertRegex(citation["sourceReference"], r"^(?:page \d+ block \d+|Positions![A-Z]+\d+)$")
        # The reconstruction cites only this matter's four documents.
        self.assertTrue({citation["documentId"] for citation in self._ask(self._matter(_Provider(unknown=True)), RECONSTRUCTION_QUESTION)["citations"]} <= set(document_ids))

    def test_render_leaves_out_an_uncitable_step_and_says_so(self) -> None:
        from group_claims_backend.services.structured_evidence import Line, render
        cited = ("doc", "extraction", "Positions!F2", "file.xlsx")
        text, used, complete = render([Line("Opening {0}", (cited,)), Line("Transfer {0}", (("doc", "extraction", "page 1 block 9", "file.pdf"),))],
                                      lambda citation: "[E1]" if citation == cited else None)
        self.assertEqual(text.splitlines()[0], "Opening [E1]")
        self.assertIn("1 step was left out", text)
        self.assertNotIn("source not included", text)
        self.assertEqual((used, complete), ([cited], False))

    # Re-reading after the reading rules change -------------------------------------

    def test_stale_readings_are_reread_and_superseded_without_double_counting(self) -> None:
        document_ids = self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        assert self.storage.database is not None
        with self.storage.database.transaction() as connection:
            # Model documents read by earlier rules: no holdings from the
            # workbooks, and a transfer reading with a truncated fund name.
            connection.execute("DELETE FROM reconciliation_candidates WHERE source_document_id IN (?, ?)", (document_ids[0], document_ids[3]))
            row = connection.execute("SELECT id, payload_json FROM reconciliation_candidates WHERE candidate_type = 'securities_transfer'").fetchone()
            payload = {**__import__("json").loads(row["payload_json"]), "toSubfund": "LGBF"}
            encoded = __import__("json").dumps(payload, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "UPDATE reconciliation_candidates SET payload_json = ?, payload_sha256 = ? WHERE id = ?",
                (encoded, __import__("hashlib").sha256(encoded.encode("utf-8")).hexdigest(), row["id"]),
            )
            connection.execute("DELETE FROM reconciliation_document_reads")
            stale_transfer = str(row["id"])
        self.assertEqual(self.reconciliation.reread_stale()["documentsRead"], 4)
        self.assertEqual(self.reconciliation.reread_stale()["documentsRead"], 0)
        queue = self.reconciliation.candidates({"status": "candidate"})["candidates"]
        self.assertNotIn(stale_transfer, {item["id"] for item in queue})
        self.assertEqual(sum(item["type"] == "securities_transfer" for item in queue), 1)
        self.assertEqual(sum(item["type"] == "holding_snapshot" for item in queue), 6)
        with self.assertRaises(Exception) as raised:
            self.reconciliation.confirm({"candidateId": stale_transfer, "acknowledged": True})
        self.assertEqual(getattr(raised.exception, "code", None), "CANDIDATE_SUPERSEDED")
        context_provider = _Provider()
        self._ask(self._matter(context_provider), RECONSTRUCTION_QUESTION)
        context = context_provider.calls[0][1]
        self.assertEqual(context.count("→ 550,000 inferred"), 1)
        self.assertNotIn("→ 400,000 inferred", context)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reconciliation_candidates WHERE id = ?", (stale_transfer,)), 1)  # kept, not deleted

    # Account migrations -------------------------------------------------------------

    def _migration_matter(self, *, notice_investor: str = "Rowan Family Office Ltd", with_notice: bool = True) -> list[str]:
        opening = self.sources / "rowan_positions_2026-03-31.xlsx"
        _workbook(opening, [
            ("2026-03-31", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-500", "XS5555555555", 400000, "Eastbank Custody"),
            ("2026-03-31", "Sable Asset Management Ltd", "Rowan Growth Fund", "SAB-900", "XS5555555555", 250000, "Eastbank Custody"),
        ])
        paths = [opening]
        if with_notice:
            notice = self.sources / "rowan_account_change.pdf"
            _pdf(notice, "Account change notice", f"Issue date: 2026-04-08\nEffective date: 2026-04-05\nInvestor: {notice_investor}",
                 f"Custody account ROW-500 was replaced by ROW-501 for {notice_investor}'s Rowan Growth Fund. This is an account identifier change, not a securities transfer.",
                 "This notice does not relate to Sable Asset Management Ltd's similarly named fund at account SAB-900.")
            paths.append(notice)
        transfer = self.sources / "rowan_transfer.pdf"
        _pdf(transfer, "Securities transfer advice", "Issue date: 2026-04-16\nEffective date: 2026-04-14\nISIN: XS5555555555",
             "Eastbank Custody confirms transfer of 40,000 nominal from Rowan Growth Fund, account ROW-501, to Rowan Growth Fund, account SAB-900.")
        closing = self.sources / "rowan_positions_2026-04-30.xlsx"
        _workbook(closing, [
            ("2026-04-30", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-501", "XS5555555555", 360000, "Eastbank Custody"),
            ("2026-04-30", "Sable Asset Management Ltd", "Rowan Growth Fund", "SAB-900", "XS5555555555", 290000, "Eastbank Custody"),
        ])
        return self._import(*paths, transfer, closing)

    def test_documented_account_migration_keeps_one_fund_and_both_account_identifiers(self) -> None:
        document_ids = self._migration_matter()
        batch = self._batch()
        self.assertEqual(batch["reviewRequired"], [])
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            funds = {
                row["external_reference"]: row["investment_vehicle_id"]
                for row in connection.execute("SELECT external_reference, investment_vehicle_id FROM accounts")
            }
            self.assertEqual(funds["ROW-500"], funds["ROW-501"])  # same canonical fund id across the account change
            self.assertNotEqual(funds["ROW-500"], funds["SAB-900"])  # same name, different investor: separate fund
            relationship = connection.execute(
                "SELECT f.external_reference, t.external_reference, r.effective_date, e.source_document_id, e.source_reference "
                "FROM account_relationships r JOIN accounts f ON f.id = r.from_account_id JOIN accounts t ON t.id = r.to_account_id "
                "JOIN account_migration_evidence e ON e.id = r.evidence_id"
            ).fetchone()
            self.assertEqual(tuple(relationship)[:4], ("ROW-500", "ROW-501", "2026-04-05", document_ids[1]))
            self.assertRegex(relationship[4], r"page 1 block \d+")
            # The migration moved no securities; the later transfer drew on the predecessor's balance.
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM holding_positions p JOIN accounts a ON a.id = p.account_id WHERE a.external_reference = 'ROW-500'"
            ).fetchone()[0], 1)

    def test_identity_answer_explains_migration_and_separate_fund_with_citations(self) -> None:
        self._migration_matter()
        status = self._ask(self._matter(), (
            "Which Rowan Growth Fund accounts belong to the same underlying fund? Explain the migration from ROW-500 to ROW-501, "
            "identify the effective date, and explain why SAB-900 must remain a separate fund. Cite the source documents."
        ))
        answer = str(status["answer"])
        self.assertIn("Rowan Growth Fund — accounts ROW-500 → ROW-501 (investor Rowan Family Office Ltd)", answer)
        self.assertRegex(answer, r"account ROW-500 was replaced by ROW-501, effective 5 Apr 2026 \[rowan_account_change\.pdf, page 1 block \d+\]")
        self.assertIn("Rowan Growth Fund — account SAB-900 (investor Sable Asset Management Ltd)", answer)
        self.assertIn(
            "account ROW-500 / ROW-501 held by Rowan Family Office Ltd [rowan_positions_2026-03-31.xlsx, Positions!B2]; "
            "account SAB-900 held by Sable Asset Management Ltd [rowan_positions_2026-03-31.xlsx, Positions!B3]. "
            "They belong to different investors", answer,
        )
        self.assertRegex(answer, r"does not relate to the other fund \[rowan_account_change\.pdf, page 1 block \d+\]")
        self.assertTrue(all(re.fullmatch(r"page \d+ block \d+|Positions![A-Z]+\d+", citation["sourceReference"]) for citation in status["citations"]))

    def test_new_account_with_the_same_name_and_investor_is_asked_not_merged(self) -> None:
        self._migration_matter(with_notice=False)
        batch = self._batch()
        self.assertIn("identity_link_requires_review", {item["reason"] for item in batch["reviewRequired"]})
        question = next(item for item in self.reconciliation.identity_questions({})["questions"] if item["targetType"] == "account")
        self.assertEqual((question["observedText"], question["suggested"]["label"], question["matchBasis"]), ("ROW-501", "ROW-500", "existing_fund_new_account"))
        self.assertEqual(self._count("SELECT COUNT(*) FROM accounts WHERE external_reference = 'ROW-501'"), 0)
        self.reconciliation.resolve_identity_question({"questionId": question["id"], "decision": "same", "acknowledged": True})
        self._batch()
        self.assertEqual(self._count(
            "SELECT COUNT(DISTINCT investment_vehicle_id) FROM accounts WHERE external_reference IN ('ROW-500', 'ROW-501')"
        ), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM account_relationships"), 0)  # a review decision, not a documented migration
        self.assertEqual(self._count("SELECT COUNT(*) FROM identity_aliases WHERE target_type = 'account' AND alias_text = 'ROW-501' AND origin = 'user_confirmed'"), 0)

    def test_distinct_answer_gives_the_new_account_its_own_fund(self) -> None:
        self._migration_matter(with_notice=False)
        self._batch()
        question = next(item for item in self.reconciliation.identity_questions({})["questions"] if item["targetType"] == "account")
        self.reconciliation.resolve_identity_question({"questionId": question["id"], "decision": "distinct", "acknowledged": True})
        self._batch()
        self.assertEqual(self._count(
            "SELECT COUNT(DISTINCT investment_vehicle_id) FROM accounts WHERE external_reference IN ('ROW-500', 'ROW-501')"
        ), 2)

    def test_migration_statement_naming_another_investor_is_not_applied(self) -> None:
        self._migration_matter(notice_investor="Sable Asset Management Ltd")
        batch = self._batch()
        self.assertIn("identity_link_requires_review", {item["reason"] for item in batch["reviewRequired"]})
        self.assertEqual(self._count("SELECT COUNT(*) FROM account_relationships"), 0)

    def test_reconstruction_carries_the_balance_across_the_migration_date(self) -> None:
        self._migration_matter()
        self._import(self._opening(), self._aliases(), self._transfer(), self._closing())  # another matter in the workspace
        provider = _Provider()
        self._ask(self._matter(provider), "Reconstruct each Rowan Growth Fund holding as of 30 April 2026, with opening positions and the transfer.")
        context = provider.calls[0][1]
        self.assertIn("Rowan Growth Fund — accounts ROW-500 → ROW-501, ISIN XS5555555555:", context)
        self.assertIn("31 Mar 2026: account ROW-500 400,000 documented", context)
        self.assertRegex(context, r"5 Apr 2026: account migration — account ROW-500 replaced by ROW-501 \[E\d+\]( \[E\d+\])?; no securities moved; 400,000 carried to account ROW-501 \(inferred\)\.")
        self.assertIn("account ROW-501 securities transfer of 40,000 out to Rowan Growth Fund (account SAB-900), effective 14 Apr 2026", context)
        self.assertIn("→ 360,000 inferred", context)
        self.assertIn("30 Apr 2026: account ROW-501 360,000 documented", context)
        self.assertIn("Rowan Growth Fund — account SAB-900, ISIN XS5555555555:", context)
        self.assertNotIn("Linden", context)

    def test_migration_after_the_question_window_is_not_used(self) -> None:
        self._migration_matter()
        provider = _Provider()
        self._ask(self._matter(provider), "Reconstruct the Rowan Growth Fund holdings from 1 March through 31 March 2026, including any transfer and effective date.")
        context = provider.calls[0][1]
        derived = context[context.index("DERIVED LOCAL RECONSTRUCTION"):]
        self.assertNotIn("account migration —", derived)
        self.assertNotIn("ROW-501", derived)
        self.assertIn("Rowan Growth Fund — account ROW-500, ISIN XS5555555555:", derived)

    # Follow-up questions -------------------------------------------------------------

    _ROWAN_IDENTITY_QUESTION = (
        "Which Rowan Growth Fund accounts belong to the same underlying fund? Explain the migration from ROW-500 to ROW-501, "
        "identify the effective date, and explain why SAB-900 must remain a separate fund. Cite the source documents."
    )

    def test_follow_up_after_a_long_local_answer_keeps_room_for_cited_evidence(self) -> None:
        from group_claims_backend.services.structured_evidence import IDENTITY_INTRO
        self._migration_matter()
        service = self._matter()
        first = self._ask(service, self._ROWAN_IDENTITY_QUESTION)
        self.assertGreater(len(str(first["answer"])), 1200)  # long enough to have overflowed the budget before
        follow_up = {**self._request("Did the account migration from ROW-500 to ROW-501 transfer any securities? Explain from the evidence."), "followUpQuestionId": first["questionId"]}
        preview = service.question_preview(follow_up)
        self.assertFalse(preview["localOnly"])
        self.assertGreaterEqual(preview["scope"]["sourceItems"], 1)
        scope, plan, _ = service._prepare_question(service._matter_question_params(follow_up, False))
        context = str(plan["context"])
        self.assertLessEqual(len(context), service._analysis._cost_controls.max_input_characters)
        previous = context[context.index("Previous provisional answer:"):context.index("DOCUMENT SCOPE")]
        self.assertNotIn("[", previous)  # citation markers are not reused as context
        self.assertNotIn(IDENTITY_INTRO, previous)
        evidence = context[context.index("EVIDENCE-LINKED"):]
        self.assertIn("(account migration); EVIDENCE: account ROW-500 replaced by ROW-501 effective 2026-04-05", evidence.splitlines()[1])

    def test_identity_follow_up_is_answered_locally_with_both_funds(self) -> None:
        self._migration_matter()
        service = self._matter()
        first = self._ask(service, self._ROWAN_IDENTITY_QUESTION)
        request = {**self._request("Why must SAB-900 remain separate?"), "followUpQuestionId": first["questionId"]}
        started = service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": []})
        status = service.question_status({"questionId": started["questionId"]})
        self.assertEqual((status["status"], status["intent"]), ("completed", "identity_resolution"))
        self.assertIn("The name “Rowan Growth Fund” is used by separate funds", str(status["answer"]))
        self.assertIn("ROW-500 / ROW-501", str(status["answer"]))

    def test_follow_up_budget_shortens_then_drops_the_earlier_answer(self) -> None:
        service = self._matter()
        follow_up = {"question": "Earlier question?", "answer": "A" * 1000}
        empty = ({}, [], "", "COVERAGE")
        full, note = service._budgeted_follow_up_context(follow_up, "Now?", empty, 20_000, historical=False)
        self.assertIn("A" * 1000, full)
        self.assertIsNone(note)
        baseline = len(service._analysis._question_prompt("Now?", "", "", "COVERAGE\n"))
        shortened, note = service._budgeted_follow_up_context(follow_up, "Now?", empty, baseline + 600 + 400, historical=False)
        self.assertIn("Previous user question: Earlier question?", shortened)
        self.assertTrue(shortened.rstrip().endswith("…"))
        self.assertIn("shortened", str(note))
        dropped, note = service._budgeted_follow_up_context(follow_up, "Now?", empty, baseline + 100, historical=False)
        self.assertEqual(dropped, "")
        self.assertIn("left out", str(note))

    def test_letter_prefixed_account_codes_are_question_identifiers(self) -> None:
        from group_claims_backend.services.question_intent import lookup_identifiers
        self.assertEqual(lookup_identifiers("Why must DEL-880 remain separate from CED-701?"), ("DEL-880", "CED-701"))
        self.assertEqual(lookup_identifiers("Allocate 180,000 to the funds"), ())

    # Holdings conflicts --------------------------------------------------------------

    CONFLICT_QUESTION = "Identify all conflicting holdings records and their exact as-of date, accounts, securities, values and difference."
    TRUST_QUESTION = "Which position should we trust and why?"

    def _disputed_statement(self, name: str = "rowan_custodian_statement.pdf", quantity: str = "345,000") -> Path:
        path = self.sources / name
        _pdf(path, "Position statement", "Issue date: 2026-05-06\nHoldings as of: 2026-04-30\nInvestor: Rowan Family Office Ltd",
             f"For Rowan Family Office Ltd, Rowan Growth Fund, account ROW-501, ISIN XS5555555555, the reported position at 30 April 2026 is {quantity} nominal.")
        return path

    def _snapshot(self) -> tuple[int, int, int]:
        return (
            self._count("SELECT COUNT(*) FROM reconciliation_candidates"),
            self._count("SELECT COUNT(*) FROM issues WHERE status = 'open'"),
            self._count("SELECT COUNT(*) FROM holding_positions"),
        )

    def test_conflict_question_lists_the_exact_competing_records(self) -> None:
        self._migration_matter()
        self._import(self._disputed_statement())
        self._import(self._opening(), self._aliases(), self._transfer(), self._closing())  # unrelated matter
        before = self._snapshot()
        status = self._ask(self._matter(), self.CONFLICT_QUESTION)
        self.assertEqual((status["status"], status["intent"]), ("completed", "holdings_conflict"))
        answer = str(status["answer"])
        self.assertIn("1 holdings conflict was found", answer)
        self.assertIn("1. Rowan Growth Fund — investor Rowan Family Office Ltd — account ROW-501 — ISIN XS5555555555 — as of 30 Apr 2026", answer)
        self.assertIn("• 360,000 nominal [rowan_positions_2026-04-30.xlsx, Positions!F2]", answer)
        self.assertRegex(answer, r"• 345,000 nominal \[rowan_custodian_statement\.pdf, page 1 block \d+\]")
        self.assertIn("Difference: 15,000 nominal. Status: unresolved — recorded as an open issue in Issues.", answer)
        self.assertNotIn("SAB-900 —", answer)  # the same-name fund of another investor is not part of the conflict
        self.assertNotIn("Linden", answer)
        self.assertEqual(self._snapshot(), before)  # answering changes no records

    def test_trust_follow_up_explains_support_without_resolving(self) -> None:
        self._migration_matter()
        self._import(self._disputed_statement())
        service = self._matter()
        first = self._ask(service, self.CONFLICT_QUESTION)
        started = service.ask_question({**self._request(self.TRUST_QUESTION), "followUpQuestionId": first["questionId"], "acknowledged": True, "consentedDocumentIds": []})
        status = service.question_status({"questionId": started["questionId"]})
        self.assertEqual((status["status"], status["intent"]), ("completed", "holdings_conflict"))
        answer = str(status["answer"])
        self.assertRegex(answer, (
            r"Expected balance from the other records: 400,000 documented for account ROW-500 on 31 Mar 2026 \[rowan_positions_2026-03-31\.xlsx, Positions!F2\]; "
            r"account migration from ROW-500 to ROW-501 effective 5 Apr 2026, which moved no securities \[rowan_account_change\.pdf, page 1 block \d+\]( \[[^\]]+\])?; "
            r"less 40,000 transferred out effective 14 Apr 2026 \[rowan_transfer\.pdf, page 1 block \d+\]( \[[^\]]+\])? → 360,000\."
        ))
        self.assertIn("Assessment: 360,000 matches that reconstruction, so it has stronger supporting evidence; 345,000 is not explained by any record used.", answer)
        self.assertIn("This is an evidence assessment, not a resolution", answer)
        self.assertIn("the conflict stays unresolved", answer)
        self.assertEqual(self._count("SELECT COUNT(*) FROM reconciliation_holding_conflicts WHERE status = 'open'"), 1)
        cited = {(citation["documentId"], citation["sourceReference"]) for citation in status["citations"]}
        self.assertEqual(len(cited), len(status["citations"]))
        self.assertGreaterEqual(len(cited), 5)

    def test_more_than_two_values_on_the_same_date_are_all_kept(self) -> None:
        self._migration_matter()
        self._import(self._disputed_statement(), self._disputed_statement("rowan_second_statement.pdf", "350,500"))
        answer = str(self._ask(self._matter(), self.CONFLICT_QUESTION)["answer"])
        for value in ("360,000", "350,500", "345,000"):
            self.assertIn(f"• {value} nominal", answer)
        self.assertIn("Difference: 15,000 nominal between the highest and lowest of 3 values.", answer)

    def test_missing_opening_balance_is_stated_instead_of_an_assessment(self) -> None:
        closing = self.sources / "only_closing.xlsx"
        _workbook(closing, [("2026-04-30", "Rowan Family Office Ltd", "Rowan Growth Fund", "ROW-501", "XS5555555555", 360000, "Eastbank Custody")])
        self._import(closing, self._disputed_statement())
        answer = str(self._ask(self._matter(), self.TRUST_QUESTION)["answer"])
        self.assertIn("Which value is better supported cannot be assessed from the records used: no earlier documented holding for this position before 30 Apr 2026", answer)
        self.assertIn("Status: unresolved", answer)

    def test_no_conflict_is_reported_plainly(self) -> None:
        self._import(self._opening(), self._aliases(), self._transfer(), self._closing())
        status = self._ask(self._matter(), self.CONFLICT_QUESTION)
        self.assertEqual(status["intent"], "holdings_conflict")
        self.assertIn("No conflicting holdings records were found in the extracted records of the selected documents.", str(status["answer"]))

    def test_dated_spreadsheet_comparison_keeps_its_own_workflow(self) -> None:
        document_ids = self._migration_matter()
        self._import(self._disputed_statement())
        service = self._matter()
        request = {"matterId": "workspace", "scope": {"mode": "selected", "documentIds": [document_ids[0], document_ids[3]]},
                   "question": "Compare the holdings in the March and April spreadsheets."}
        started = service.ask_question({**request, "acknowledged": True, "consentedDocumentIds": []})
        status = service.question_status({"questionId": started["questionId"]})
        self.assertEqual(status["intent"], "holdings_comparison")
        self.assertIn("Compared rowan_positions_2026-03-31.xlsx", str(status["answer"]))

    def test_negated_or_unquoted_alias_wording_is_not_an_alias_statement(self) -> None:
        blocks = [
            (0, "'Alder Fund' does not refer to the same fund as 'Alder Fund II' for custody account ALD-1."),
            (1, "Alder Fund identifies Alder Fund II in custody account ALD-2."),
        ]
        self.assertEqual(extract_page_aliases(blocks), [])


if __name__ == "__main__":
    unittest.main()
