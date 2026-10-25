"""Tests for reading PDF records written in different wording and formats."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import pymupdf

from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.pdf_fields import extract_page_records, parse_amount, parse_date
from group_claims_backend.services.reconciliation import ReconciliationService
from group_claims_backend.storage.service import StorageService


ROOT = Path(__file__).resolve().parents[2]
GENERATOR_ROOT = ROOT / "scripts" / "validation-sprint"
NAMES = (
    "01_opening_holdings_2025-01-31.xlsx",
    "02_partial_transfer_effective_2025-02-15.pdf",
    "03_claim_right_assignment_effective_2025-02-18.pdf",
    "04_later_holdings_2025-02-28.xlsx",
    "05_contradictory_custodian_statement_2025-02-28.pdf",
)


def _records(*blocks: str):
    records, issue_date, _block = extract_page_records(list(enumerate(blocks)))
    return [(record.kind, dict(record.payload)) for record in records], issue_date


class ParsingTests(unittest.TestCase):
    def test_dates_in_common_formats_and_ambiguity_refused(self) -> None:
        for text in ("2025-02-15", "15 February 2025", "15th Feb 2025", "15-Feb-2025", "February 15, 2025", "Feb. 15 2025", "15/02/2025", "02/15/2025", "15.02.2025"):
            self.assertEqual(parse_date(text), "2025-02-15", text)
        self.assertIsNone(parse_date("03/04/2025"))  # 3 April or March 4
        self.assertEqual(parse_date("04/04/2025"), "2025-04-04")
        self.assertIsNone(parse_date("31 February 2025"))

    def test_amounts_in_common_formats_and_ambiguity_refused(self) -> None:
        self.assertEqual(parse_amount("1,000,000"), Decimal("1000000"))
        self.assertEqual(parse_amount("1.000.000,50"), Decimal("1000000.50"))
        self.assertEqual(parse_amount("1,000,000.50"), Decimal("1000000.50"))
        self.assertEqual(parse_amount("1 000 000"), Decimal("1000000"))
        self.assertEqual(parse_amount("1'000'000"), Decimal("1000000"))
        self.assertEqual(parse_amount("12,5"), Decimal("12.5"))
        self.assertIsNone(parse_amount("200.000"))  # thousands or decimals


class ExtractionTests(unittest.TestCase):
    def test_labelled_transfer_with_debit_and_credit_accounts(self) -> None:
        records, issue = _records("Delivery instruction\nDate: 14/03/2025\nValue date: 2025-03-03\nDebit account: CUST-ALPHA-02\nCredit account: CUST-ALPHA-03\nISIN: XS0123456789\nNominal: USD 50.000,00")
        self.assertEqual(issue, "2025-03-14")
        self.assertEqual(records, [("securities_transfer", {
            "investor": None, "fromSubfund": None, "fromAccount": "CUST-ALPHA-02", "toSubfund": None, "toAccount": "CUST-ALPHA-03",
            "isin": "XS0123456789", "quantity": "50000", "effectiveDate": "2025-03-03",
        })])

    def test_prose_transfer_in_passive_voice_with_us_dates(self) -> None:
        records, _issue = _records("Date: March 12, 2025", "On March 10, 2025, 75,000 units of XS0123456789 were delivered from account CUST-ALPHA-03 to Meridian Income Fund C (SF-C), account CUST-GAMMA-09.")
        self.assertEqual(records, [("securities_transfer", {
            "investor": None, "fromSubfund": None, "fromAccount": "CUST-ALPHA-03", "toSubfund": "Meridian Income Fund C (SF-C)", "toAccount": "CUST-GAMMA-09",
            "isin": "XS0123456789", "quantity": "75000", "effectiveDate": "2025-03-10",
        })])

    def test_several_holdings_phrased_differently_on_one_page(self) -> None:
        records, _issue = _records(
            "Issue date: 5 April 2025",
            "As at 31-Mar-2025, Horizon Credit Fund A held 400 000 nominal of ISIN XS0123456789 in account CUST-ALPHA-01.",
            "Account CUST-ALPHA-02 (Horizon Credit Fund B) holds a position of 1,000,000 nominal units in XS0123456789 as of 31 March 2025.",
        )
        self.assertEqual([(payload["account"], payload["subfund"], payload["quantity"], payload["positionDate"]) for _kind, payload in records], [
            ("CUST-ALPHA-01", "Horizon Credit Fund A", "400000", "2025-03-31"),
            ("CUST-ALPHA-02", "Horizon Credit Fund B", "1000000", "2025-03-31"),
        ])

    def test_labelled_assignment(self) -> None:
        records, _issue = _records("Deed of assignment\nDate of issue: 2 April 2025\nClaim ref: CR-2025-GAMMA\nAssignor: Meridian Income Fund C (SF-C)\nAssignee: Northstar Claims SPV\nISIN: XS0123456789\nEffective date: 1 April 2025\nPercentage assigned: 25%")
        self.assertEqual(records, [("claim_assignment", {
            "claimReference": "CR-2025-GAMMA", "fromEntity": "Meridian Income Fund C (SF-C)", "toEntity": "Northstar Claims SPV",
            "isin": "XS0123456789", "effectiveDate": "2025-04-01", "percentage": "25",
        })])

    def test_negated_ambiguous_and_conflicting_text_produces_nothing(self) -> None:
        self.assertEqual(_records("Issue date: 1 May 2025", "This notice does not transfer 10,000 nominal of ISIN XS0123456789 from account CUST-ALPHA-01 to account CUST-ALPHA-02, effective 1 May 2025.")[0], [])
        self.assertEqual(_records("Issue date: 1 May 2025", "Account CUST-ALPHA-01 holds 200.000 nominal of ISIN XS0123456789 as of 30 April 2025.")[0], [])
        self.assertEqual(_records("Issue date: 1 May 2025", "Account CUST-ALPHA-01 holds 200,000 nominal of ISIN XS0123456789 as of 03/04/2025.")[0], [])
        self.assertEqual(_records("Issue date: 1 May 2025\nAccount: CUST-ALPHA-01\nAccount: CUST-ALPHA-02\nISIN: XS0123456789\nNominal: 10,000\nAs of: 30 April 2025")[0], [])


HEADER = "Account\nSubfund\nISIN\nNominal\nCcy\n"
ROWS = (
    "CUST-ALPHA-01\nHorizon Credit Fund A (SF-A)\nXS0123456789\n400,000\nUSD\n",
    "CUST-ALPHA-02\nHorizon Credit Fund B (SF-B)\nXS0123456789\n1,000,000\nUSD\n",
    "CUST-ALPHA-03\nMeridian Income Fund C (SF-C)\nXS0123456789\n500,000\nUSD\n",
)


def _row_block_page(rows: tuple[str, ...] = ROWS, total: str | None = "Total 1,900,000\n"):
    """Blocks as PyMuPDF stores a table: one short block per row, cells as lines."""
    blocks = [(0, "Atlas Custody Bank - Position statement\n"), (1, "Issue date: 5 April 2025\nStatement date: 31 March 2025\n"), (2, HEADER)]
    geometry = {0: (54.0, 47.0, 268.0, 64.0), 1: (54.0, 69.0, 194.0, 98.0), 2: (54.0, 120.0, 535.0, 133.0)}
    y = 138.0
    for index, text in enumerate(rows, start=3):
        blocks.append((index, text))
        geometry[index] = (54.0, y, 539.0, y + 13)
        y += 18
    if total is not None:
        blocks.append((len(blocks), total))
        geometry[len(geometry)] = (54.0, y + 20, 117.0, y + 33)
    records, _issue, _block = extract_page_records(blocks, None, geometry)
    return records


class TableTests(unittest.TestCase):
    def test_row_block_table_with_matching_total(self) -> None:
        records = _row_block_page()
        self.assertEqual([(r.payload["account"], r.payload["subfund"], r.payload["quantity"], r.payload["positionDate"]) for r in records], [
            ("CUST-ALPHA-01", "Horizon Credit Fund A (SF-A)", "400000", "2025-03-31"),
            ("CUST-ALPHA-02", "Horizon Credit Fund B (SF-B)", "1000000", "2025-03-31"),
            ("CUST-ALPHA-03", "Meridian Income Fund C (SF-C)", "500000", "2025-03-31"),
        ])
        self.assertEqual(records[0].block_indexes, (1, 2, 3, 6))  # as-of label, header, row, total
        self.assertEqual({r.kind for r in records}, {"holding_snapshot"})

    def test_total_that_does_not_match_refuses_the_table(self) -> None:
        self.assertEqual(_row_block_page(total="Total 2,000,000\n"), [])

    def test_row_that_cannot_be_aligned_is_skipped_and_then_total_refuses(self) -> None:
        wrapped = ("CUST-ALPHA-01\nHorizon Credit Fund A\n(SF-A)\nXS0123456789\n400,000\nUSD\n", *ROWS[1:])
        self.assertEqual([r.payload["account"] for r in _row_block_page(wrapped, total=None)], ["CUST-ALPHA-02", "CUST-ALPHA-03"])
        self.assertEqual(_row_block_page(wrapped), [])

    def test_cell_block_table_aligns_by_position_with_an_empty_cell(self) -> None:
        columns = (54.0, 150.0, 330.0, 430.0)
        cells = [("Account", "Subfund", "ISIN", "Nominal amount"), ("CUST-ALPHA-01", "", "XS0123456789", "400 000"), ("CUST-ALPHA-02", "Horizon Credit Fund B (SF-B)", "XS0123456789", "1.000.000,00")]
        blocks, geometry, index = [(0, "Issue date: 5 April 2025\nAs at: 31/03/2025")], {0: (54.0, 60.0, 200.0, 85.0)}, 1
        for row_number, row in enumerate(cells):
            y = 120.0 + row_number * 18
            for x, text in zip(columns, row):
                if text:
                    blocks.append((index, text))
                    geometry[index] = (x, y, x + 60, y + 11)
                    index += 1
        records, _issue, _block = extract_page_records(blocks, None, geometry)
        self.assertEqual([(r.payload["account"], r.payload["subfund"], r.payload["quantity"], r.payload["positionDate"]) for r in records], [
            ("CUST-ALPHA-01", None, "400000", "2025-03-31"),
            ("CUST-ALPHA-02", "Horizon Credit Fund B (SF-B)", "1000000", "2025-03-31"),
        ])

    def test_without_positions_tables_are_not_read(self) -> None:
        records, _issue, _block = extract_page_records([(0, "Issue date: 5 April 2025\nStatement date: 31 March 2025"), (1, HEADER), (2, ROWS[0])])
        self.assertEqual(records, [])


class PdfPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claimflow-pdf-fields-")
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
        self._batch([str(row["id"]) for row in self._candidate_rows()])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self, *paths: Path) -> None:
        result = self.imports.import_authorized_paths([str(path) for path in paths])
        self.assertTrue(all(item["ok"] for item in result["documents"]))

    def _batch(self, candidate_ids: list[str]) -> dict[str, object]:
        return dict(self.service.apply_policy({"candidateIds": candidate_ids, "policy": "reviewed_batch", "acknowledged": True}))

    def _candidate_rows(self, filename: str | None = None):
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            rows = connection.execute(
                "SELECT c.*, d.original_filename FROM reconciliation_candidates c JOIN source_documents d ON d.id = c.source_document_id ORDER BY c.created_at, c.id"
            ).fetchall()
        return [row for row in rows if filename is None or row["original_filename"] == filename]

    def _pdf(self, filename: str, *paragraphs: str) -> Path:
        path = self.sources / filename
        document = pymupdf.open()
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(54, 54, 558, 738), "\n\n".join(paragraphs), fontsize=11)
        document.save(path)
        document.close()
        return path

    def test_labelled_transfer_without_names_fills_details_from_known_accounts(self) -> None:
        self._import(self._pdf("delivery.pdf", "Delivery instruction\nDate: 14/03/2025\nValue date: 2025-03-03\nDebit account: CUST-ALPHA-02\nCredit account: CUST-ALPHA-03\nISIN: XS0123456789\nNominal: USD 50.000,00"))
        [candidate] = self._candidate_rows("delivery.pdf")
        self.assertEqual(candidate["document_issue_date"], "2025-03-14")
        self.assertRegex(candidate["source_reference"], r"^page 1 block \d+")
        self.assertEqual(len(self._batch([candidate["id"]])["confirmed"]), 1)
        positions = {item["account"]: item["quantity"] for item in self.service.holdings_as_of({"asOfDate": "2025-03-04"})["positions"]}
        self.assertEqual(positions["CUST-ALPHA-02"], "950000")
        self.assertEqual(positions["CUST-ALPHA-03"], "550000")

    def test_transfer_to_unknown_account_without_investor_is_flagged_not_guessed(self) -> None:
        self._import(self._pdf("new-account.pdf", "Date: March 12, 2025", "On March 10, 2025, 75,000 units of XS0123456789 were delivered from account CUST-ALPHA-03 to Meridian Income Fund C (SF-C), account CUST-GAMMA-09."))
        [candidate] = self._candidate_rows("new-account.pdf")
        batch = self._batch([candidate["id"]])
        self.assertEqual(batch["reviewRequired"], [{"candidateId": candidate["id"], "reason": "account_details_incomplete"}])
        assert self.storage.database is not None
        with self.storage.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM securities_transfers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open' AND reason = 'account details incomplete'").fetchone()[0], 1)

    def test_reworded_holding_statement_is_confirmed(self) -> None:
        self._import(self._pdf("april-statement.pdf", "Issue date: 5 April 2025", "As at 31-Mar-2025, Horizon Credit Fund A (SF-A) held 400 000 nominal of ISIN XS0123456789 in account CUST-ALPHA-01."))
        [candidate] = self._candidate_rows("april-statement.pdf")
        self.assertEqual(len(self._batch([candidate["id"]])["confirmed"]), 1)
        positions = {item["account"]: (item["quantity"], item["reconstructionState"]) for item in self.service.holdings_as_of({"asOfDate": "2025-03-31"})["positions"]}
        self.assertEqual(positions["CUST-ALPHA-01"], ("400000", "documented"))

    def test_statement_table_is_read_into_one_holding_per_row(self) -> None:
        path = self.sources / "table-statement.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((54, 60), "Atlas Custody Bank - Position statement", fontsize=12)
        page.insert_text((54, 80), "Issue date: 5 April 2025", fontsize=10)
        page.insert_text((54, 95), "Statement date: 31 March 2025", fontsize=10)
        y = 130
        for row in (("Account", "Subfund", "ISIN", "Nominal", "Ccy"), ("CUST-ALPHA-01", "Horizon Credit Fund A (SF-A)", "XS0123456789", "400,000", "USD"), ("CUST-ALPHA-03", "Meridian Income Fund C (SF-C)", "XS0123456789", "500,000", "USD")):
            for x, text in zip((54, 150, 330, 430, 520), row):
                page.insert_text((x, y), text, fontsize=9)
            y += 18
        page.insert_text((54, y + 20), "Total 900,000", fontsize=9)
        document.save(path)
        document.close()
        self._import(path)
        candidates = self._candidate_rows("table-statement.pdf")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(self._batch([row["id"] for row in candidates])["confirmed"]), 2)
        positions = {item["account"]: (item["quantity"], item["reconstructionState"]) for item in self.service.holdings_as_of({"asOfDate": "2025-03-31"})["positions"]}
        self.assertEqual(positions["CUST-ALPHA-01"], ("400000", "documented"))
        self.assertEqual(positions["CUST-ALPHA-03"], ("500000", "documented"))

    def test_document_with_only_ambiguous_dates_stages_nothing(self) -> None:
        self._import(self._pdf("ambiguous.pdf", "Date: 03/04/2025", "Account CUST-ALPHA-01 holds 200,000 nominal of ISIN XS0123456789 as of 03/04/2025."))
        self.assertEqual(self._candidate_rows("ambiguous.pdf"), [])


if __name__ == "__main__":
    unittest.main()
