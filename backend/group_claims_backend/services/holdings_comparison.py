"""Deterministic comparison of dated holdings between two spreadsheets.

Columns are identified only from documented header text.  Rows are matched
conservatively on the same entity names, account, and ISIN; distinct accounts
are never combined and duplicate rows are never summed.  Quantities use
``Decimal``.  A formula without a stored value, an ambiguous number format,
or a unit or currency mismatch is reported with its source cells instead of
calculated.  A document's control total is checked against its own records.
A different quantity is a documented snapshot difference only: it does not
establish a trade, settlement, transfer, or any change in legal claim rights.
Legal wording found in the spreadsheets is reported separately as a caveat,
never as a holding value.  Nothing here contacts an AI provider.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .document_references import filename_dates
from .entity_inventory import entity_key
from .question_intent import question_entity_names

MAX_HEADER_SCAN_ROWS = 60
MAX_DATA_ROWS = 2_000
MAX_RESULT_ROWS = 200
MAX_SCANNED_CELLS = 60_000
MAX_CAVEATS_PER_DOCUMENT = 5

_ISIN_HEADERS = frozenset({"isin", "isin code", "security isin", "isin/wkn", "wkn/isin"})
_QUANTITY_HEADERS = frozenset({
    "quantity", "qty", "nominal", "nominal amount", "nominal value", "units", "holding", "holdings", "position",
    "shares", "number of shares", "number of units", "menge", "bestand", "stück", "stueck", "stückzahl", "nominale", "anzahl",
    "eligible nominal amount", "eligible nominal", "eligible quantity", "nominal holding", "nominal held", "face amount",
    "face value", "principal amount", "quantity held", "units held", "position amount",
})
_UNIT_HEADERS = frozenset({"unit", "quantity unit", "unit type", "einheit"})
_CURRENCY_HEADERS = frozenset({
    "currency", "ccy", "nominal currency", "position currency", "security currency", "denomination", "währung", "waehrung",
})
_ACCOUNT_HEADERS = frozenset({
    "account", "account number", "account no", "account no.", "securities account", "custody account", "konto",
    "kontonummer", "depot", "depotnummer", "depot number",
})
_DATE_HEADERS = frozenset({"as of", "as-of", "as of date", "as-of date", "position date", "valuation date", "date", "stichtag", "datum"})
_ENTITY_HEADERS = {
    "investor": "investor", "investor name": "investor", "anleger": "investor",
    "subfund": "subfund", "sub-fund": "subfund", "sub fund": "subfund", "teilfonds": "subfund", "subfund name": "subfund",
    "fund": "fund", "fund name": "fund", "investment vehicle": "fund", "vehicle": "fund", "fonds": "fund",
    "holder": "holder", "account holder": "holder",
}
# A label above the table that names the investor for the whole sheet, with
# its value in the next cell to the right (e.g. "Investor / legal entity").
_SHEET_INVESTOR_LABELS = frozenset({
    "investor", "investor name", "investor/legal entity", "investor/entity", "legal entity", "anleger",
})
_TOTAL_LABEL = re.compile(r"\b(?:control\s+total|total|sum|summe|gesamt\w*)\b")
_SUM_FORMULA = re.compile(r"^=\s*SUM\(\s*\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)\s*\)$", re.IGNORECASE)
_LEGAL_CAVEAT = re.compile(
    r"\b(?:legal|claims?|rights?|assignments?|assigned|litigation|entitlements?|abtretung|anspruch|ansprüche)\b"
)
# An unmatched capitalised phrase is reported as a missing entity only when it
# reads like an entity name ("Zeta Growth Fund Z"), not ordinary capitalised
# words in a sentence.
_ENTITY_WORD = re.compile(
    r"\b(?:fund|fonds|sub-?fund|teilfonds|compartment|portfolio|trust|partners|capital|investors?|holdings|lp|llp|llc|"
    r"ltd|limited|ag|sa|plc|gmbh|sicav|sarl|inc|corp|bank)\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00(?::00)?)?$")
_REFERENCE = re.compile(r"^(?P<sheet>.*)!(?P<column>[A-Z]{1,3})(?P<row>\d+)$")
_PLAIN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_EN_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_SPACE_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:[   ']\d{3})+(?:[.,]\d+)?$")
_DE_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:\.\d{3})+(?:,\d+)?$")
_DE_DECIMAL = re.compile(r"^[+-]?\d+,\d+$")


def _heading(text: str) -> str:
    return " ".join(text.casefold().replace(":", " ").replace(" / ", "/").split())


def _column_letter(reference: str) -> str | None:
    match = _REFERENCE.match(reference)
    return match.group("column") if match else None


def _iso_date(text: str) -> str | None:
    match = _ISO_DATE.match(text.strip())
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def _signed(value: Decimal) -> str:
    return format(value, "f") if value <= 0 else f"+{format(value, 'f')}"


def _grouped(value: Decimal, *, signed: bool = False) -> str:
    """Thousands-grouped display of an exact Decimal (no float conversion)."""
    sign = "-" if value < 0 else "+" if signed and value > 0 else ""
    whole, _, fraction = format(abs(value), "f").partition(".")
    grouped = f"{int(whole):,}"
    return f"{sign}{grouped}.{fraction}" if fraction.strip("0") else f"{sign}{grouped}"


def grouped_quantity(value: Decimal, *, signed: bool = False) -> str:
    """Public, display-only grouping of an exact Decimal for answer text."""
    return _grouped(value, signed=signed)


def parse_quantity(text: str, value_type: str | None) -> tuple[Decimal | None, str | None]:
    """Return an exact Decimal, or why the stored value cannot be used safely."""
    raw = text.strip()
    if value_type == "f" or raw.startswith("="):
        return None, "the cell holds a formula and no stored calculated value was extracted"
    if not raw:
        return None, "the quantity cell is empty"
    try:
        if value_type == "n":
            return Decimal(raw), None
        if _PLAIN.fullmatch(raw):
            # In a text cell "1.000" may be one thousand (German grouping) or 1.000.
            if re.fullmatch(r"[+-]?\d{1,3}\.\d{3}", raw):
                return None, f"the number format of “{raw}” is ambiguous (thousands separator or decimal point)"
            return Decimal(raw), None
        if _EN_GROUPED.fullmatch(raw):
            # "1,000" could also be a German decimal; only accept English
            # grouping when the German reading is not possible.
            if _DE_DECIMAL.fullmatch(raw):
                return None, f"the number format of “{raw}” is ambiguous (thousands separator or decimal comma)"
            return Decimal(raw.replace(",", "")), None
        if _DE_GROUPED.fullmatch(raw):
            return Decimal(raw.replace(".", "").replace(",", ".")), None
        if _SPACE_GROUPED.fullmatch(raw):
            compact = re.sub(r"[   ']", "", raw)
            return Decimal(compact.replace(",", ".")), None
        if _DE_DECIMAL.fullmatch(raw):
            return None, f"the number format of “{raw}” is ambiguous (decimal comma or thousands separator)"
    except InvalidOperation:
        pass
    return None, f"“{raw}” is not a recognised quantity"


@dataclass(frozen=True)
class HoldingsDocument:
    document_id: str
    filename: str
    extraction_id: str


@dataclass(frozen=True)
class _Cell:
    text: str
    value_type: str | None
    formula: str | None
    reference: str


@dataclass
class _Row:
    document: HoldingsDocument
    sheet: str
    row_number: int
    entities: dict[str, tuple[str, str]] = field(default_factory=dict)  # kind -> (name, reference)
    account: tuple[str, str] | None = None
    isin: tuple[str, str] | None = None
    quantity: tuple[str, str, str | None] | None = None  # (text, reference, value type)
    unit: tuple[str, str] | None = None
    currency: tuple[str, str] | None = None
    date: tuple[str, str] | None = None
    # Every cell of the row under the table's header, with its heading.
    cells: list[dict[str, str]] = field(default_factory=list)
    row_reference: str | None = None

    def key(self) -> tuple[object, ...]:
        return (
            tuple(sorted((kind, entity_key(name)) for kind, (name, _ref) in self.entities.items())),
            entity_key(self.account[0]) if self.account else None,
            self.isin[0].upper() if self.isin else None,
        )


@dataclass
class _Table:
    document: HoldingsDocument
    sheet: str
    header_row: int
    header_reference: str
    quantity_heading: tuple[str, str]  # (heading text, reference)
    records: list[_Row] = field(default_factory=list)
    investor: dict[str, str] | None = None
    stated_date: dict[str, str | None] | None = None
    control_total: dict[str, object] | None = None
    caveats: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DatedRecord:
    """One holdings record from one spreadsheet, read with the same header rules as a comparison."""

    document: HoldingsDocument
    entities: Mapping[str, tuple[str, str]]  # kind -> (name, reference)
    isin: str | None
    quantity: Decimal | None
    quantity_text: str
    quantity_reference: str
    # The record's own row date, else its table's stated date, else the file-name date.
    date: str | None


@dataclass(frozen=True)
class HoldingsResult:
    answer: str
    payload: Mapping[str, object]
    citations: tuple[tuple[str, str, str, str], ...]
    evidence_document_ids: frozenset[str]
    complete: bool


class HoldingsComparison:
    def __init__(self, connection: sqlite3.Connection, documents: Sequence[HoldingsDocument]) -> None:
        self._connection = connection
        self._documents = list(documents)
        self._issues: list[str] = []
        self._tables: dict[str, list[_Table]] = {}

    def compare(self, question: str = "", *, legal_question: bool = False) -> HoldingsResult:
        """Compare the two documents, restricted to the entities the question names.

        Requested entities are read from ``question`` by finding the names
        actually present in these spreadsheets; shorthand such as "Fund A" is
        resolved only to a single matching name.  Every requested entity is
        accounted for in ``requestedEntities``; one that is missing or
        ambiguous is reported and makes the comparison incomplete.
        ``legal_question`` marks a question that also asked about legal
        status, which the comparison states it does not answer.
        """
        rows_by_id = {document.document_id: self._rows(document) for document in self._documents}
        ordered, order_basis = self._ordered_documents()
        rows_by_document = [rows_by_id[document.document_id] for document in ordered]
        self._check_dates(ordered, rows_by_document)
        names, requested = self._requested_entities(question, rows_by_document)
        if names:
            rows_by_document = [
                [row for row in rows if any(entity_key(name) in names for name, _ref in row.entities.values())]
                for rows in rows_by_document
            ]
        earlier = self._index(rows_by_document[0])
        later = self._index(rows_by_document[1])
        result_rows: list[dict[str, object]] = []
        lines: list[str] = []
        compared_values: list[tuple[Decimal, Decimal, str | None, _Row, _Row]] = []
        citations: list[tuple[str, str, str, str]] = []

        def cite(document: HoldingsDocument, reference: str | None) -> None:
            if reference:
                citation = (document.document_id, document.extraction_id, reference, document.filename)
                if citation not in citations:
                    citations.append(citation)

        for key in list(dict.fromkeys([*earlier, *later]))[:MAX_RESULT_ROWS]:
            first, second = earlier.get(key, []), later.get(key, [])
            row = (first or second)[0]
            currencies = {item.currency[0].strip().upper() for item in (*first, *second) if item.currency and item.currency[0].strip()}
            entry: dict[str, object] = {
                "entities": [{"kind": kind, "name": name} for kind, (name, _ref) in sorted(row.entities.items())],
                "account": row.account[0] if row.account else None,
                "isin": row.isin[0] if row.isin else None,
                "currency": next(iter(currencies)) if len(currencies) == 1 else None,
                "earlier": self._side(first[0]) if len(first) == 1 else None,
                "later": self._side(second[0]) if len(second) == 1 else None,
                "difference": None,
                "status": "compared",
                "note": None,
            }
            label = " / ".join(
                [name for kind, (name, _ref) in sorted(row.entities.items()) if kind != "investor"]
                + ([row.account[0]] if row.account else [])
            ) or (row.isin[0] if row.isin else "Unnamed position")
            for source in (*first, *second):
                for reference in self._references(source):
                    cite(source.document, reference)
            if len(first) > 1 or len(second) > 1:
                duplicates = first if len(first) > 1 else second
                entry["status"] = "duplicate_rows"
                entry["note"] = (
                    f"{duplicates[0].document.filename} has {len(duplicates)} rows for the same entity, account and ISIN "
                    f"(rows {', '.join(str(item.row_number) for item in duplicates)}); they were not summed."
                )
            elif not first or not second:
                entry["status"] = "only_later" if not first else "only_earlier"
                present = (first or second)[0].document.filename
                entry["note"] = (
                    f"Recorded only in {present}. Absence from the other spreadsheet is not evidence of a sale or transfer."
                )
            else:
                earlier_row, later_row = first[0], second[0]
                earlier_value, earlier_problem = parse_quantity(*self._quantity(earlier_row))
                later_value, later_problem = parse_quantity(*self._quantity(later_row))
                earlier_unit, later_unit = self._normalised(earlier_row.unit), self._normalised(later_row.unit)
                earlier_currency, later_currency = self._normalised(earlier_row.currency), self._normalised(later_row.currency)
                if earlier_problem or later_problem:
                    entry["status"] = "unusable_quantity"
                    entry["note"] = "; ".join(
                        f"{source.document.filename} {source.quantity[1] if source.quantity else 'quantity'}: {problem}"
                        for source, problem in ((earlier_row, earlier_problem), (later_row, later_problem)) if problem
                    ) + ". No difference was calculated."
                elif earlier_unit != later_unit:
                    entry["status"] = "unit_mismatch"
                    entry["note"] = (
                        f"Units differ or are missing on one side ({earlier_row.unit[0] if earlier_row.unit else 'not stated'} vs "
                        f"{later_row.unit[0] if later_row.unit else 'not stated'}). No difference was calculated."
                    )
                elif earlier_currency != later_currency:
                    entry["status"] = "currency_mismatch"
                    entry["note"] = (
                        f"Currencies differ or are missing on one side ({earlier_row.currency[0] if earlier_row.currency else 'not stated'} vs "
                        f"{later_row.currency[0] if later_row.currency else 'not stated'}). No difference was calculated."
                    )
                else:
                    assert earlier_value is not None and later_value is not None
                    difference = later_value - earlier_value
                    entry["difference"] = _signed(difference)
                    compared_values.append((earlier_value, later_value, earlier_unit, earlier_row, later_row))
                    lines.append(
                        f"{label}: {_grouped(earlier_value)} → {_grouped(later_value)} "
                        f"(change {_grouped(difference, signed=True)})"
                    )
                    if earlier_unit is None and earlier_currency is None:
                        entry["note"] = "Neither spreadsheet states a unit or currency for this quantity."
            if entry["status"] != "compared":
                lines.append(f"{label}: not compared")
            result_rows.append(entry)
        totals = self._totals(result_rows, compared_values)
        tables = [table for document in ordered for table in self._tables.get(document.document_id, [])]
        self._reconcile(tables, compared_values, totals)
        for entry in requested:
            if entry["name"] is not None:
                entry["positions"] = sum(
                    any(entity_key(str(entity["name"])) == entity_key(str(entry["name"])) for entity in row["entities"])  # type: ignore[union-attr]
                    for row in result_rows
                )
        for table in tables:
            cite(table.document, table.quantity_heading[1])
            if table.control_total:
                cite(table.document, str(table.control_total["reference"]))
        # Legal wording is cited last and only as a caveat, never as a value.
        for table in tables:
            for caveat in table.caveats:
                cite(table.document, caveat["reference"])
        complete = all(entry["status"] == "compared" for entry in result_rows) and not self._issues and bool(result_rows)
        documents_payload = []
        for document in ordered:
            date, basis = self._date(document)
            documents_payload.append({
                "documentId": document.document_id, "filename": document.filename, "date": date, "dateBasis": basis,
                "tables": [self._table_payload(table) for table in self._tables.get(document.document_id, [])],
            })
        caveats = self._caveats(tables)
        payload = {
            "documents": documents_payload,
            "orderBasis": order_basis,
            "rows": result_rows,
            "totals": totals,
            "issues": list(self._issues),
            "requestedEntities": requested,
            "caveats": caveats,
            "legalQuestionNotAnswered": legal_question,
        }
        return HoldingsResult(
            answer=self._answer(ordered, order_basis, result_rows, lines, totals, tables, caveats, legal_question),
            payload=payload, citations=tuple(citations[:120]),
            evidence_document_ids=frozenset(citation[0] for citation in citations), complete=complete,
        )

    # -- dates -----------------------------------------------------------

    def dated_records(self, document: HoldingsDocument) -> list[DatedRecord]:
        """Every holdings record of one spreadsheet with a usable quantity cell and its date."""
        self._rows(document)
        file_date, _basis = self._date(document)
        records: list[DatedRecord] = []
        for table in self._tables.get(document.document_id, []):
            stated = table.stated_date.get("date") if table.stated_date else None
            for row in table.records:
                if row.quantity is None:
                    continue
                quantity, _issue = parse_quantity(row.quantity[0], row.quantity[2])
                row_date = _iso_date(row.date[0]) if row.date else None
                records.append(DatedRecord(
                    document, dict(row.entities),
                    row.isin[0].upper() if row.isin else None, quantity, row.quantity[0], row.quantity[1],
                    row_date or (str(stated) if stated else None) or file_date,
                ))
        return records

    def _date(self, document: HoldingsDocument) -> tuple[str | None, str | None]:
        """The document's date from its file name, else from a single "As of date" label cell."""
        dates = filename_dates(document.filename)
        if len(dates) == 1:
            return f"{dates[0][0]:04d}-{dates[0][1]:02d}-{dates[0][2]:02d}", "file name"
        stated = {
            str(table.stated_date["date"]): str(table.stated_date["reference"])
            for table in self._tables.get(document.document_id, [])
            if table.stated_date and table.stated_date.get("date")
        }
        if len(stated) == 1:
            date, reference = next(iter(stated.items()))
            return date, f"cell {reference}"
        return None, None

    def _ordered_documents(self) -> tuple[list[HoldingsDocument], str]:
        dated = [self._date(document) for document in self._documents]
        dates = [date for date, _basis in dated]
        if all(dates) and len(set(dates)) == len(dates):
            basis = "file-name dates" if all(basis == "file name" for _date, basis in dated) else "stated dates"
            return sorted(self._documents, key=lambda document: self._date(document)[0] or ""), basis
        self._issues.append(
            "At least one spreadsheet carries no single date in its file name or an “As of date” label, so the "
            "difference is shown as the second listed file minus the first."
        )
        return sorted(self._documents, key=lambda document: document.filename.casefold()), "file-name order"

    def _check_dates(self, ordered: Sequence[HoldingsDocument], rows_by_document: Sequence[Sequence[_Row]]) -> None:
        """Report, never resolve, a disagreement between the file-name date and dates in the cells."""
        for document, rows in zip(ordered, rows_by_document):
            date, basis = self._date(document)
            if date is None or basis != "file name":
                continue
            for table in self._tables.get(document.document_id, []):
                stated = table.stated_date.get("date") if table.stated_date else None
                if table.stated_date and stated and stated != date:
                    self._issues.append(
                        f"{document.filename}: the file name date {date} differs from the stated date {stated} at "
                        f"{table.stated_date['reference']}."
                    )
            mismatched = [row for row in rows if row.date and _iso_date(row.date[0]) not in {None, date}]
            if mismatched and mismatched[0].date:
                self._issues.append(
                    f"{document.filename}: {len(mismatched)} row date(s) differ from the file name date {date} "
                    f"(first at {mismatched[0].date[1]})."
                )

    # -- extraction ----------------------------------------------------

    def _rows(self, document: HoldingsDocument) -> list[_Row]:
        fetched = self._connection.execute(
            "SELECT sheet_name, worksheet_index, row_number, column_number, value_text, value_type, formula_text, source_reference "
            "FROM extracted_content WHERE extraction_id = ? AND content_kind IN ('spreadsheet_cell', 'csv_cell') "
            "AND is_empty = 0 AND row_number <= ? ORDER BY worksheet_index, sheet_name, row_number, column_number LIMIT ?",
            (document.extraction_id, MAX_HEADER_SCAN_ROWS + MAX_DATA_ROWS, MAX_SCANNED_CELLS + 1),
        ).fetchall()
        if len(fetched) > MAX_SCANNED_CELLS:
            self._issues.append(f"{document.filename} exceeds the local comparison limit; only its first cells were read.")
            fetched = fetched[:MAX_SCANNED_CELLS]
        grid: dict[str, dict[int, dict[int, _Cell]]] = {}
        for cell in fetched:
            grid.setdefault(str(cell["sheet_name"]), {}).setdefault(int(cell["row_number"]), {})[int(cell["column_number"])] = _Cell(
                str(cell["value_text"] or "").strip(), cell["value_type"], cell["formula_text"], str(cell["source_reference"]),
            )
        tables: list[tuple[str, int, dict[int, tuple[str, str, str]]]] = []
        for sheet, sheet_rows in grid.items():
            for row_number in sorted(sheet_rows):
                if row_number > MAX_HEADER_SCAN_ROWS:
                    break
                columns: dict[int, tuple[str, str, str]] = {}
                for column, cell in sheet_rows[row_number].items():
                    heading = _heading(cell.text)
                    kind = (
                        "isin" if heading in _ISIN_HEADERS else "quantity" if heading in _QUANTITY_HEADERS
                        else "unit" if heading in _UNIT_HEADERS else "currency" if heading in _CURRENCY_HEADERS
                        else "account" if heading in _ACCOUNT_HEADERS else "date" if heading in _DATE_HEADERS
                        else _ENTITY_HEADERS.get(heading)
                    )
                    if kind:
                        columns[column] = (kind, cell.text, cell.reference)
                if {"isin", "quantity"} <= {kind for kind, _text, _ref in columns.values()}:
                    tables.append((sheet, row_number, columns))
        if not tables:
            self._issues.append(f"{document.filename} has no header row with both an ISIN and a quantity column heading.")
            return []
        rows: list[_Row] = []
        for index, (sheet, header_row, columns) in enumerate(tables):
            quantity_columns = [column for column, (kind, _text, _ref) in columns.items() if kind == "quantity"]
            if len(quantity_columns) != 1:
                self._issues.append(
                    f"{document.filename} {sheet} row {header_row} has more than one quantity-like heading ("
                    + ", ".join(f"{columns[column][1]} at {columns[column][2]}" for column in quantity_columns)
                    + "); the quantity column is ambiguous, so this table was not compared."
                )
                continue
            quantity_column = quantity_columns[0]
            sheet_rows = grid[sheet]
            # A later header row on the same sheet starts a new table.
            end = min([
                header_row + MAX_DATA_ROWS + 1,
                *(other for other_sheet, other, _columns in tables[index + 1:] if other_sheet == sheet),
            ])
            headings = sheet_rows[header_row]
            header_columns = sorted(headings)
            table = _Table(
                document, sheet, header_row, self._range(sheet, header_columns, header_row, headings),
                (columns[quantity_column][1], columns[quantity_column][2]),
            )
            self._sheet_labels(table, sheet_rows, columns)
            used_rows = {header_row}
            totals: list[tuple[int, _Cell, _Cell]] = []
            for row_number in sorted(number for number in sheet_rows if header_row < number < end):
                row_cells = sheet_rows[row_number]
                record = _Row(document, sheet, row_number)
                for column, cell in sorted(row_cells.items()):
                    if column in headings:
                        record.cells.append({"heading": headings[column].text, "value": cell.text, "reference": cell.reference})
                    if column not in columns:
                        continue
                    kind = columns[column][0]
                    value = (cell.text, cell.reference)
                    if kind == "isin":
                        record.isin = value
                    elif kind == "quantity":
                        record.quantity = (cell.text, cell.reference, cell.value_type)
                    elif kind == "unit":
                        record.unit = value
                    elif kind == "currency":
                        record.currency = value
                    elif kind == "account":
                        record.account = value
                    elif kind == "date":
                        record.date = value
                    else:
                        record.entities[kind] = value
                if record.isin and (record.quantity or record.entities):
                    if table.investor and "investor" not in record.entities:
                        record.entities["investor"] = (table.investor["name"], table.investor["reference"])
                    record.row_reference = self._range(sheet, header_columns, row_number, headings)
                    table.records.append(record)
                    used_rows.add(row_number)
                    continue
                label = next((cell for cell in row_cells.values() if _TOTAL_LABEL.search(cell.text.casefold())), None)
                if label is not None and quantity_column in row_cells:
                    totals.append((row_number, label, row_cells[quantity_column]))
                    used_rows.add(row_number)
            if totals:
                table.control_total = self._control_total(table, totals[0][1], totals[0][2])
            # Legal wording outside the header, records and totals is kept
            # apart as a caveat; it is never read as a holding value.
            table.caveats = [
                {"reference": cell.reference, "text": cell.text}
                for number in sorted(sheet_rows) if number < end and number not in used_rows
                for _column, cell in sorted(sheet_rows[number].items())
                if len(cell.text) >= 40 and _LEGAL_CAVEAT.search(cell.text.casefold())
            ][:MAX_CAVEATS_PER_DOCUMENT]
            self._tables.setdefault(document.document_id, []).append(table)
            rows.extend(table.records)
        return rows

    @staticmethod
    def _range(sheet: str, columns: Sequence[int], row_number: int, headings: Mapping[int, _Cell]) -> str:
        first = _column_letter(headings[columns[0]].reference) or ""
        last = _column_letter(headings[columns[-1]].reference) or ""
        return f"{sheet}!{first}{row_number}:{last}{row_number}"

    @staticmethod
    def _sheet_labels(
        table: _Table, sheet_rows: Mapping[int, Mapping[int, _Cell]], columns: Mapping[int, tuple[str, str, str]],
    ) -> None:
        """Read "Investor / legal entity | X" and "As of date | Y" label pairs above the table."""
        has_investor_column = any(kind == "investor" for kind, _text, _ref in columns.values())
        for row_number in sorted((number for number in sheet_rows if number < table.header_row), reverse=True):
            cells = [cell for _column, cell in sorted(sheet_rows[row_number].items())]
            for position, cell in enumerate(cells[:-1]):
                heading, value = _heading(cell.text), cells[position + 1]
                if not value.text:
                    continue
                if heading in _SHEET_INVESTOR_LABELS and table.investor is None and not has_investor_column:
                    table.investor = {
                        "name": value.text, "reference": value.reference, "label": cell.text, "labelReference": cell.reference,
                    }
                elif heading in _DATE_HEADERS and table.stated_date is None:
                    table.stated_date = {
                        "date": _iso_date(value.text), "text": value.text, "reference": value.reference,
                        "labelReference": cell.reference,
                    }

    def _control_total(self, table: _Table, label: _Cell, cell: _Cell) -> dict[str, object]:
        """Check a control-total cell against the exact total of the table's own record rows."""
        parsed = [parse_quantity(*self._quantity(record)) for record in table.records]
        usable = [value for value, _problem in parsed if value is not None]
        records_total = sum(usable, Decimal(0)) if parsed and len(usable) == len(parsed) else None
        record_rows = sorted(record.row_number for record in table.records)
        quantity_column = _column_letter(table.quantity_heading[1])
        formula = cell.formula or (cell.text if cell.text.startswith("=") else None)
        result: dict[str, object] = {
            "reference": cell.reference, "label": label.text, "labelReference": label.reference,
            "value": None if formula else cell.text, "formula": formula,
            "recordsTotal": format(records_total, "f") if records_total is not None else None,
            "status": "not_verified", "note": "",
        }
        if records_total is None:
            result["note"] = f"{cell.reference} was not checked because at least one record quantity is not usable."
        elif formula:
            match = _SUM_FORMULA.match(formula.strip())
            covers = bool(
                match and match.group(1).upper() == quantity_column and match.group(3).upper() == quantity_column
                and list(range(int(match.group(2)), int(match.group(4)) + 1)) == record_rows
            )
            if covers:
                result["status"] = "formula_covers_records"
                result["note"] = (
                    f"{cell.reference} holds the formula {formula} with no stored result; it sums exactly the "
                    f"{len(record_rows)} record row(s), whose exact total is {_grouped(records_total)}."
                )
            else:
                result["note"] = (
                    f"{cell.reference} holds the formula {formula} with no stored result and does not sum exactly the "
                    "record rows, so it was not verified."
                )
        else:
            stated, problem = parse_quantity(cell.text, cell.value_type)
            if stated is None:
                result["note"] = f"{cell.reference} was not verified: {problem}."
            elif stated == records_total:
                result["status"] = "matches"
                result["note"] = f"{cell.reference} states {_grouped(stated)}, equal to the exact total of the record rows."
            else:
                result["status"] = "differs"
                result["note"] = f"{cell.reference} states {_grouped(stated)}, but the record rows total {_grouped(records_total)}."
                self._issues.append(f"{table.document.filename}: control total mismatch. {result['note']}")
        return result

    @staticmethod
    def _totals(
        rows: Sequence[Mapping[str, object]], values: Sequence[tuple[Decimal, Decimal, str | None, _Row, _Row]],
    ) -> dict[str, object]:
        """Totals of the compared rows, only when every row compared and all share one ISIN, currency and unit.

        ``coverage`` is set by ``_reconcile``: "all_records" only when the
        compared rows are every record row of both tables, so a subset is never
        presented as the spreadsheets' holdings total.
        """
        empty: dict[str, object] = {"earlier": None, "later": None, "difference": None, "currency": None, "coverage": "selected_rows"}
        if not rows or len(values) != len(rows):
            return {**empty, "note": "Totals are not shown because at least one position could not be compared."}
        if len({row["isin"] for row in rows}) != 1 or len({row["currency"] for row in rows}) != 1 or len({value[2] for value in values}) != 1:
            return {**empty, "note": "Totals are not shown because the positions cover more than one ISIN, currency or unit."}
        earlier = sum((value[0] for value in values), Decimal(0))
        later = sum((value[1] for value in values), Decimal(0))
        return {
            "earlier": format(earlier, "f"), "later": format(later, "f"), "difference": _signed(later - earlier),
            "currency": rows[0]["currency"], "coverage": "selected_rows", "note": None,
        }

    @staticmethod
    def _reconcile(
        tables: Sequence[_Table], values: Sequence[tuple[Decimal, Decimal, str | None, _Row, _Row]], totals: dict[str, object],
    ) -> None:
        """Relate each source control total to the rows actually compared.

        A control total checks its own record rows.  It reconciles the
        comparison only when the compared rows are exactly those records and
        their exact total equals the control total's record total.
        """
        compared: dict[tuple[str, str, int], Decimal] = {}
        for earlier_value, later_value, _unit, earlier_row, later_row in values:
            compared[(earlier_row.document.document_id, earlier_row.sheet, earlier_row.row_number)] = earlier_value
            compared[(later_row.document.document_id, later_row.sheet, later_row.row_number)] = later_value
        every_record = bool(tables)
        for table in tables:
            records = {(table.document.document_id, table.sheet, record.row_number) for record in table.records}
            included = records & compared.keys()
            every_record = every_record and included == records
            control = table.control_total
            if control is None:
                continue
            comparison_total = sum((compared[key] for key in included), Decimal(0))
            control["recordRows"] = len(records)
            control["comparisonRows"] = len(included)
            control["comparisonTotal"] = format(comparison_total, "f") if included else None
            control["reconciled"] = bool(
                included == records and control["status"] in {"formula_covers_records", "matches"}
                and control["recordsTotal"] is not None and Decimal(str(control["recordsTotal"])) == comparison_total
            )
            if included != records and control["status"] in {"formula_covers_records", "matches"}:
                control["note"] = (
                    f"{control['note']} The comparison includes {len(included)} of these {len(records)} record rows, so this "
                    "source control total does not reconcile the comparison totals."
                )
        if totals.get("earlier") is not None:
            totals["coverage"] = "all_records" if every_record else "selected_rows"
            if not every_record:
                totals["note"] = (
                    f"Totals of the {len(values)} compared position(s) only, not the spreadsheets' full holdings totals."
                )

    def _requested_entities(
        self, question: str, rows_by_document: Sequence[Sequence[_Row]],
    ) -> tuple[set[str], list[dict[str, object]]]:
        """The entities the question asks about, matched against names in these spreadsheets.

        Full names found in the question come first.  A remaining capitalised
        phrase is resolved as shorthand only when it is the ending of exactly
        one name (preferring names the question already named); otherwise it
        is reported as ambiguous, or as missing when it reads like an entity
        name.  Nothing is guessed from similarity beyond a whole-word ending.
        """
        present: dict[str, str] = {}
        for rows in rows_by_document:
            for row in rows:
                for name, _reference in row.entities.values():
                    present.setdefault(entity_key(name), name)
        text = " ".join(question.casefold().split())
        requested: list[dict[str, object]] = []
        selected: set[str] = set()
        spans: list[tuple[int, int]] = []
        first_seen: dict[str, int] = {}
        # Longest names first, so "Fund A" inside "Horizon Credit Fund A" is not a separate match.
        for key in sorted(present, key=len, reverse=True):
            for match in re.finditer(rf"(?<![\w-]){re.escape(key)}(?![\w-])", text):
                if any(match.start() < end and start < match.end() for start, end in spans):
                    continue
                spans.append((match.start(), match.end()))
                first_seen[key] = min(first_seen.get(key, match.start()), match.start())
        for key in sorted(first_seen, key=first_seen.__getitem__):
            selected.add(key)
            requested.append({"text": present[key], "name": present[key], "match": "exact", "candidates": [], "positions": 0})
        named_in_question = set(selected)
        for variants in question_entity_names(question):
            phrase = variants[0]
            key = entity_key(phrase)
            if not key or key in named_in_question or any(f" {name} " in f" {key} " for name in named_in_question):
                continue  # the phrase is, or contains, a name already found
            tokens = key.split()

            def ends_with(name: str) -> bool:
                parts = name.split()
                return len(parts) > len(tokens) and parts[-len(tokens):] == tokens

            prior = [name for name in named_in_question if ends_with(name)]
            candidates = prior or [name for name in present if ends_with(name)]
            if len(candidates) == 1:
                (resolved,) = candidates
                selected.add(resolved)
                if not any(entry["text"] == phrase for entry in requested):
                    requested.append({"text": phrase, "name": present[resolved], "match": "alias", "candidates": [], "positions": 0})
            elif candidates:
                options = sorted(present[name] for name in candidates)
                requested.append({"text": phrase, "name": None, "match": "ambiguous", "candidates": options, "positions": 0})
                self._issues.append(
                    f"“{phrase}” could refer to more than one entity ({', '.join(options)}); it was not resolved."
                )
            elif _ENTITY_WORD.search(phrase) or re.search(r"\s(?:[A-Z]|[IVX]{1,4})$", phrase):
                if any(entity_key(str(entry["text"])).endswith(f" {key}") for entry in requested if entry["match"] == "not_found"):
                    continue  # "Fund Z" adds nothing once "Zeta Growth Fund Z" is reported
                requested.append({"text": phrase, "name": None, "match": "not_found", "candidates": [], "positions": 0})
                self._issues.append(f"“{phrase}” does not appear under an entity heading in either spreadsheet.")
        return selected, requested

    @staticmethod
    def _caveats(tables: Sequence[_Table]) -> list[dict[str, object]]:
        """Legal wording found in the spreadsheets, grouped by identical text."""
        grouped: dict[str, dict[str, object]] = {}
        for table in tables:
            for caveat in table.caveats:
                sources = grouped.setdefault(caveat["text"], {"text": caveat["text"], "sources": []})["sources"]
                assert isinstance(sources, list)
                sources.append({
                    "documentId": table.document.document_id, "filename": table.document.filename,
                    "reference": caveat["reference"],
                })
        return list(grouped.values())

    @staticmethod
    def _table_payload(table: _Table) -> dict[str, object]:
        rows = [record.row_number for record in table.records]
        quantity_column = _column_letter(table.quantity_heading[1]) or ""
        first, last = (table.records[0].row_reference, table.records[-1].row_reference) if table.records else (None, None)
        return {
            "sheet": table.sheet,
            "headerReference": table.header_reference,
            "recordReference": f"{first.split(':')[0]}:{last.split(':')[1]}" if first and last else None,
            "quantityHeading": table.quantity_heading[0],
            "quantityHeadingReference": table.quantity_heading[1],
            "quantityReference": f"{table.sheet}!{quantity_column}{min(rows)}:{quantity_column}{max(rows)}" if rows else None,
            "investor": table.investor,
            "statedDate": table.stated_date,
            "controlTotal": table.control_total,
        }

    @staticmethod
    def _index(rows: Sequence[_Row]) -> dict[tuple[object, ...], list[_Row]]:
        index: dict[tuple[object, ...], list[_Row]] = {}
        for row in rows:
            index.setdefault(row.key(), []).append(row)
        return index

    @staticmethod
    def _normalised(item: tuple[str, str] | None) -> str | None:
        return item[0].strip().casefold() if item and item[0].strip() else None

    @staticmethod
    def _quantity(row: _Row) -> tuple[str, str | None]:
        return (row.quantity[0], row.quantity[2]) if row.quantity else ("", None)

    @staticmethod
    def _references(row: _Row) -> list[str]:
        references = [reference for _name, reference in row.entities.values()]
        for item in (row.account, row.isin, row.currency, row.unit, row.date):
            if item:
                references.append(item[1])
        if row.quantity:
            references.append(row.quantity[1])
        return references

    @staticmethod
    def _side(row: _Row) -> dict[str, object]:
        return {
            "documentId": row.document.document_id, "filename": row.document.filename, "sheet": row.sheet, "row": row.row_number,
            "rowReference": row.row_reference,
            "quantity": row.quantity[0] if row.quantity else None,
            "quantityReference": row.quantity[1] if row.quantity else None,
            "isinReference": row.isin[1] if row.isin else None,
            "accountReference": row.account[1] if row.account else None,
            "entityReferences": [reference for _name, reference in row.entities.values()],
            "unit": row.unit[0] if row.unit else None,
            "unitReference": row.unit[1] if row.unit else None,
            "currency": row.currency[0] if row.currency else None,
            "currencyReference": row.currency[1] if row.currency else None,
            "rowDate": row.date[0] if row.date else None,
            "rowDateReference": row.date[1] if row.date else None,
            "cells": list(row.cells),
        }

    def _answer(
        self, ordered: Sequence[HoldingsDocument], order_basis: str, rows: Sequence[Mapping[str, object]],
        lines: Sequence[str], totals: Mapping[str, object], tables: Sequence[_Table],
        caveats: Sequence[Mapping[str, object]], legal_question: bool,
    ) -> str:
        labels = [f"{document.filename} ({self._date(document)[0] or 'undated'})" for document in ordered]
        compared = sum(row["status"] == "compared" for row in rows)
        differing = sum(row["status"] == "compared" and Decimal(str(row["difference"])) != 0 for row in rows)
        parts: list[str] = []
        if not rows:
            parts.append(f"No comparable ISIN rows were found in {labels[0]} and {labels[1]}.")
        else:
            parts.append(
                f"Compared {labels[0]} with {labels[1]} (ordered by {order_basis}). {compared} of {len(rows)} position(s) "
                f"had usable quantities on both dates; {differing} differ. Differences are later minus earlier, calculated "
                "exactly from the stored cell values. Rows are matched only when entity, account and ISIN agree."
            )
            context = []
            for title, values in (
                ("Investor", {
                    str(entity["name"]) for row in rows for entity in row["entities"]  # type: ignore[attr-defined]
                    if entity["kind"] == "investor"
                }),
                ("ISIN", {str(row["isin"]) for row in rows if row["isin"]}),
                ("Currency", {str(row["currency"]) for row in rows if row["currency"]}),
            ):
                if len(values) == 1:
                    context.append(f"{title}: {next(iter(values))}.")
            parts.extend(context)
            parts.append("; ".join(lines) + ".")
            if totals.get("earlier") is not None:
                parts.append(
                    ("Totals: " if totals.get("coverage") == "all_records" else "Totals of the compared positions only (not the full holdings totals): ")
                    + f"{_grouped(Decimal(str(totals['earlier'])))} → {_grouped(Decimal(str(totals['later'])))} "
                    f"(net change {_grouped(Decimal(str(totals['difference'])), signed=True)})."
                )
        incomplete = len(rows) - compared
        if incomplete:
            parts.append(f"{incomplete} position(s) could not be compared; see the notes and source cells.")
        for table in tables:
            if table.control_total:
                parts.append(f"Control total in {table.document.filename}: {table.control_total['note']}")
        if self._issues:
            parts.append(" ".join(self._issues))
        caveat = (
            "Legal caveat: a different recorded quantity is a documented snapshot difference only; it does not by itself "
            "show a trade, settlement, transfer, or any change in legal claim rights."
        )
        for item in caveats:
            sources = ", ".join(
                f"{source['filename']} {source['reference']}" for source in item["sources"]  # type: ignore[attr-defined]
            )
            caveat += f" The spreadsheets also state ({sources}): “{item['text']}”"
        if legal_question:
            caveat += (
                " Legal claim status: the compared spreadsheets record holdings only. They do not establish whether "
                "legal claim rights were assigned, transferred or withdrawn, and no other document was used for this "
                "part of the question."
            )
        parts.append(caveat)
        return " ".join(parts)
