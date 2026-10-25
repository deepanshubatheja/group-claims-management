"""Deterministic field extraction from PDF text that is worded differently.

Reads three record types from one page: dated holdings, securities
transfers, and claim-right assignments.  Three layouts are understood:

* holdings tables (a header row naming account and nominal columns, one
  holding per data row), rebuilt from stored block positions;
* labelled fields ("Account No: 123", "Value date: 15 Feb 2025"), forming at
  most one record per page; and
* prose sentences, one record per sentence, located by meaning words
  (transfer/deliver, assign, holds/holding) rather than one fixed phrasing.

The extractor is conservative.  A sentence containing a negation ("does not
assign") is ignored; numeric dates whose day and month could be swapped and
amounts such as "200.000" are refused rather than guessed; a labelled
field given two different values on one page is unusable; a table row that
cannot be lined up with its header is skipped; and a table whose "Total" line
does not equal its parsed rows is refused entirely.  Every returned
value is copied from the page text, so the caller's source-support check
still verifies it against the stored extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence


# Dates ---------------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DATE = re.compile(
    rf"(?P<iso>\b\d{{4}}-\d{{2}}-\d{{2}}\b)"
    rf"|(?P<dmy>\b(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.]+(?P<m1>{_MONTH})[\s\-/.,]+(?P<y1>\d{{4}})\b)"
    rf"|(?P<mdy>\b(?P<m2>{_MONTH})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}})\b)"
    rf"|(?P<num>\b(?P<a>\d{{1,2}})[./-](?P<b>\d{{1,2}})[./-](?P<y3>\d{{4}})\b)",
    re.IGNORECASE,
)


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _date_value(match: re.Match[str]) -> str | None:
    if match.group("iso"):
        try:
            return date.fromisoformat(match.group("iso")).isoformat()
        except ValueError:
            return None
    if match.group("dmy"):
        return _iso(int(match.group("y1")), _MONTHS[match.group("m1").rstrip(".").lower()], int(match.group("d1")))
    if match.group("mdy"):
        return _iso(int(match.group("y2")), _MONTHS[match.group("m2").rstrip(".").lower()], int(match.group("d2")))
    first, second, year = int(match.group("a")), int(match.group("b")), int(match.group("y3"))
    if first == second or second > 12:
        return _iso(year, first, second) if second > 12 else _iso(year, first, first)
    if first > 12:
        return _iso(year, second, first)
    return None  # 03/04/2025: day-first or month-first cannot be told apart


def find_dates(text: str) -> list[tuple[str | None, int]]:
    """Every date-like token as (ISO date or None when ambiguous, start offset)."""
    return [(_date_value(match), match.start()) for match in _DATE.finditer(text)]


def parse_date(value: object) -> str | None:
    """The first date in ``value`` as ISO text, or None if absent or ambiguous."""
    if not isinstance(value, str):
        return None
    dates = find_dates(value)
    return dates[0][0] if dates else None


# Amounts -------------------------------------------------------------------

_NUMBER = r"\d{1,3}(?:[ \u00a0'’]\d{3})+(?:[.,]\d+)?|\d[\d,.]*\d|\d"
_NUMBER_TOKEN = re.compile(rf"(?<![\w.,/\-])(?:{_NUMBER})(?![\w/\-]|[.,]\d)")


def parse_amount(token: str) -> Decimal | None:
    """Parse 1,000,000 / 1.000.000,50 / 1 000 000 / 12,5; refuse 200.000."""
    text = re.sub(r"[ \u00a0'’]", "", token.strip())
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_separator == "," else ","
        whole, _, fraction = text.rpartition(decimal_separator)
        if not re.fullmatch(rf"\d{{1,3}}(?:{re.escape(thousands)}\d{{3}})*", whole) or not fraction.isdigit():
            return None
        text = whole.replace(thousands, "") + "." + fraction
    elif "," in text:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
            text = text.replace(",", "")
        elif re.fullmatch(r"\d+,\d{1,2}", text):
            text = text.replace(",", ".")
        else:
            return None
    elif "." in text:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3}){2,}", text):
            text = text.replace(".", "")
        elif re.fullmatch(r"\d+\.\d{3}", text) or not re.fullmatch(r"\d+\.\d+", text):
            return None  # "200.000" may be thousands or decimals
    elif not text.isdigit():
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def find_amounts(text: str) -> set[Decimal]:
    return {amount for amount in (parse_amount(match.group(0)) for match in _NUMBER_TOKEN.finditer(text)) if amount is not None}


def amount_text(value: Decimal) -> str:
    result = format(value.normalize(), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


# Identifiers and names -----------------------------------------------------

_ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
_ISIN_LABELLED = re.compile(r"\bISIN(?:\s+(?:code|no\.?))?\s*:?\s*([A-Za-z]{2}[A-Za-z0-9]{9}[0-9])\b", re.IGNORECASE)
_CODE = r"(?=[A-Za-z0-9\-/.]*\d)[A-Za-z0-9](?:[A-Za-z0-9\-/.]*[A-Za-z0-9])?"
_ACCOUNT_PHRASE = re.compile(
    rf"\b(?:securities\s+|custody\s+|safekeeping\s+|depot\s+)?(?:account|acct\.?|a/c)"
    rf"(?:\s+(?:no\.?|number|#|ref(?:erence)?\.?))?\s*:?\s*(?P<code>{_CODE})",
    re.IGNORECASE,
)
_CODE_ONLY = re.compile(rf"^\s*(?P<code>{_CODE})\s*$")
_NEGATION = re.compile(r"\b(?:not|never|no\s+longer|neither|nor)\b|\bno\b(?!\.|\s*[:#]|\s*\d)|n['’]t\b", re.IGNORECASE)
# Account renumbering moves no securities; never read it as a transfer.
_MIGRATION_WORD = re.compile(r"\b(?:migrat\w*|renumber\w*|replaced\s+by|re-?designat\w*)\b", re.IGNORECASE)
_TRANSFER_WORD = re.compile(r"\b(?:transfer(?:s|red|ring)?|deliver(?:s|ed|y)?|mov(?:e|es|ed|ement)|reallocat\w*|debit(?:ed)?)\b", re.IGNORECASE)
_ASSIGN_WORD = re.compile(r"\bassign(?:s|ed)?\b", re.IGNORECASE)
_CLAIM_WORD = re.compile(r"\bclaims?\b", re.IGNORECASE)
_HOLDING_WORD = re.compile(r"\b(?:hold|holds|held|holding|holdings|position|balance)\b", re.IGNORECASE)
_TIED_AMOUNT = re.compile(
    rf"(?P<a>{_NUMBER})\s*(?:nominal|units|bonds|notes)\b"
    rf"|\bnominal(?:\s+(?:amount|value))?(?:\s+of)?\s+(?:[A-Z]{{3}}\s+)?(?P<b>{_NUMBER})",
    re.IGNORECASE,
)
_PERCENT = re.compile(rf"(?P<n>{_NUMBER})\s*(?:%|percent\b|per\s+cent\b)", re.IGNORECASE)
_NAME_CONNECTORS = frozenset({"of", "and", "&", "de", "du", "des", "la", "le", "the", "for", "und", "et"})
_LEADING_WORDS = frozenset({"for", "in", "on", "at", "by", "under", "pursuant", "with", "as", "from", "to", "this", "that", "it"})
_LEGAL_FORM_ONLY = frozenset({"inc", "inc.", "ltd", "ltd.", "limited", "llc", "lp", "l.p.", "llp", "plc", "sa", "s.a.", "ag", "nv", "n.v.", "bv", "b.v.", "gmbh", "sarl", "s.a.r.l."})


def _trim_name(value: str) -> str | None:
    """Keep the leading proper-noun run of a phrase; None if it is not a name."""
    tokens = value.strip(" \t,;:.").split()
    kept: list[str] = []
    for position, token in enumerate(tokens):
        bare = token.rstrip(",;")
        name_like = (
            bare[:1].isupper() or bare[:1] in "(&\"'"
            or (any(ch.isdigit() for ch in bare) and any(ch.isalpha() for ch in bare))
            or (bool(kept) and (bare.lower() in _NAME_CONNECTORS or (bare.isdigit() and len(bare) <= 2)))
        )
        if not name_like and bare in {"-", "–", "—"} and kept and position + 1 < len(tokens) and tokens[position + 1][:1].isupper():
            kept.append(bare)
            continue
        if not name_like:
            break
        kept.append(bare)
        if token != bare:
            # A comma ends the name unless a legal form follows ("Alpha, Inc.").
            following = tokens[position + 1].strip(",;.").lower() if position + 1 < len(tokens) else ""
            if following in {form.rstrip(".") for form in _LEGAL_FORM_ONLY}:
                kept[-1] = bare + ","
                continue
            break
    while kept and (kept[-1].lower() in _NAME_CONNECTORS or kept[-1] in {"-", "–", "—"}):
        kept.pop()
    name = " ".join(kept).strip(" ,;:")
    if not name or len(name) > 160 or len(kept) > 14 or name.split()[0].lower() in _LEADING_WORDS or _ISIN.search(name) or name.split()[0].upper() == "ISIN":
        return None
    return name


def _party(value: str) -> tuple[str | None, str | None]:
    """Split "Alpha Fund (SF-A), account CUST-01" into (name, account)."""
    account_match = _ACCOUNT_PHRASE.search(value)
    if account_match is not None:
        code, remainder = account_match.group("code"), value[:account_match.start()] + " " + value[account_match.end():]
    else:
        leading = re.match(rf"^\s*(?P<code>{_CODE})(?=$|[\s,;(])", value)
        if leading is None or _ISIN.fullmatch(leading.group("code").upper()):
            return _trim_name(value), None
        code, remainder = leading.group("code"), value[leading.end():]
    remainder = re.sub(r"\(\s*\)", " ", remainder)
    remainder = re.sub(r"\s*,\s*(?:,\s*)+", ", ", remainder)
    remainder = re.sub(r"^[\s,;]+|[\s,;]+$", "", remainder)
    remainder = re.sub(r"^\(\s*(.*?)\s*\)$", r"\1", remainder)
    remainder = re.sub(r"^(?:of|held\s+by|belonging\s+to|in\s+the\s+name\s+of)\s+", "", remainder, flags=re.IGNORECASE)
    return (_trim_name(remainder) if remainder else None), code


def _single(values: Iterable[str | None]) -> str | None:
    distinct = {value for value in values if value}
    return next(iter(distinct)) if len(distinct) == 1 else None


# Labels --------------------------------------------------------------------

_LABELS: Mapping[str, tuple[str, ...]] = {
    "issue_date": ("issue date", "date of issue", "issued", "issued on", "document date", "date issued", "notice date", "date of notice", "date"),
    "effective_date": ("effective date", "effective", "value date", "settlement date", "transfer date", "date of transfer", "date of assignment", "assignment date", "effective as of", "with effect from"),
    "as_of": ("as-of date", "as of", "as at", "as at date", "position date", "holding date", "balance date", "statement date", "statement as-of date", "reporting date", "valuation date", "holdings as of", "holding as of", "positions as of", "position as of", "balances as of"),
    "investor": ("investor", "investor group", "investor name", "client", "client name", "beneficial owner", "account holder"),
    "account": ("account", "account no", "account no.", "account number", "account reference", "securities account", "custody account", "safekeeping account", "depot", "acct", "acct no", "a/c", "a/c no"),
    "from_account": ("from account", "source account", "debit account", "account debited", "delivering account", "sending account", "transferor account", "account to be debited"),
    "to_account": ("to account", "destination account", "credit account", "account credited", "receiving account", "beneficiary account", "transferee account", "account to be credited"),
    "subfund": ("subfund", "sub-fund", "fund", "fund name", "portfolio", "compartment"),
    "from_party": ("from", "transferor", "delivering party", "delivering fund", "from fund", "from subfund", "source fund", "transferring fund"),
    "to_party": ("to", "transferee", "receiving party", "receiving fund", "to fund", "to subfund", "destination fund"),
    "isin": ("isin", "isin code", "isin no", "security isin"),
    "quantity": ("nominal", "nominal amount", "nominal value", "quantity", "units", "number of units", "holding", "position", "balance", "amount", "transferred nominal", "nominal transferred", "quantity transferred", "face amount", "principal amount"),
    "claim_reference": ("claim reference", "claim ref", "claim ref.", "claim no", "claim no.", "claim number", "claim id", "reference", "ref"),
    "assignor": ("assignor", "assigning party"),
    "assignee": ("assignee", "assigned to"),
    "percentage": ("percentage", "percentage assigned", "assigned percentage", "share assigned", "portion assigned", "assigned share"),
}
_LABEL_TO_FIELD = {label: name for name, labels in _LABELS.items() for label in labels}


def _label_pattern(label: str) -> str:
    return r"[\s\-]+".join(re.escape(word) for word in re.split(r"[\s\-]+", label))


def _label_form(label: str) -> str:
    return " ".join(part for part in re.split(r"[\s\-_]+", label.strip().lower()) if part)


_LABEL_FORMS = {_label_form(label): name for label, name in _LABEL_TO_FIELD.items()}
_LABEL_ALTERNATION = "|".join(_label_pattern(label) for label in sorted(_LABEL_TO_FIELD, key=len, reverse=True))
_LABEL_LINE = re.compile(rf"^\s*(?P<label>{_LABEL_ALTERNATION})\s*(?::\s*|\s+[-–]\s+|\t+|\s{{2,}})(?P<value>\S.*?)\s*$", re.IGNORECASE)
_LABEL_ONLY = re.compile(rf"^\s*(?P<label>{_LABEL_ALTERNATION})\s*:?\s*$", re.IGNORECASE)
# Any other "Key: value" line (e.g. "Custodian: Atlas") stands alone in prose.
_OTHER_KEY_VALUE = re.compile(r"^\s*[A-Z][A-Za-z /&\-]{1,40}:\s+\S")


def _parse_field(name: str, value: str) -> object | None:
    if name in {"issue_date", "effective_date", "as_of"}:
        return parse_date(value)
    if name in {"account", "from_account", "to_account"}:
        return _party(value)[1]
    if name in {"from_party", "to_party"}:
        party = _party(value)
        return party if any(party) else None
    if name == "isin":
        match = _ISIN.search(value.upper())
        return match.group(0) if match else None
    if name == "quantity":
        amounts = find_amounts(value)
        return amount_text(next(iter(amounts))) if len(amounts) == 1 else None
    if name == "percentage":
        amounts = find_amounts(value)
        if len(amounts) != 1:
            return None
        amount = next(iter(amounts))
        return amount_text(amount) if 0 < amount <= 100 else None
    if name == "claim_reference":
        match = re.search(_CODE, value)
        return match.group(0) if match else None
    return _trim_name(value)


@dataclass
class _Labels:
    values: dict[str, object] = field(default_factory=dict)
    blocks: dict[str, int] = field(default_factory=dict)
    lines: dict[str, str] = field(default_factory=dict)
    ambiguous: set[str] = field(default_factory=set)

    def add(self, name: str, value: object, block: int, line: str) -> None:
        if name in self.ambiguous:
            return
        if name in self.values and self.values[name] != value:
            self.ambiguous.add(name)
            for store in (self.values, self.blocks, self.lines):
                store.pop(name, None)
            return
        self.values.setdefault(name, value)
        self.blocks.setdefault(name, block)
        self.lines.setdefault(name, line.strip())

    def get(self, name: str) -> object | None:
        return self.values.get(name)


def _read_labels(blocks: Sequence[tuple[int, str]]) -> tuple[_Labels, list[tuple[int, str]]]:
    """Return labelled fields and the remaining prose of each block."""
    labels = _Labels()
    prose: list[tuple[int, str]] = []
    lines = [(index, line) for index, text in blocks for line in text.splitlines()]
    consumed: set[int] = set()
    for position, (block, line) in enumerate(lines):
        match = _LABEL_LINE.match(line)
        value: str | None = None
        if match:
            value = match.group("value")
        else:
            match = _LABEL_ONLY.match(line)
            if match:
                following = next(((p, text) for p, (_b, text) in enumerate(lines[position + 1:], start=position + 1) if text.strip()), None)
                if following is not None and not _LABEL_LINE.match(following[1]) and not _LABEL_ONLY.match(following[1]):
                    value = following[1]
                    consumed.add(following[0])
        if match is None or value is None:
            continue
        name = _LABEL_FORMS.get(_label_form(match.group("label")))
        if name is None:
            continue
        parsed = _parse_field(name, value)
        if parsed is not None:
            labels.add(name, parsed, block, line)
            consumed.add(position)
    group: list[str] = []
    group_block: int | None = None

    def flush() -> None:
        text = " ".join(" ".join(group).split())
        if text and group_block is not None:
            prose.append((group_block, text))
        group.clear()

    for position, (block, line) in enumerate(lines):
        if block != group_block:
            flush()
            group_block = block
        if position in consumed:
            flush()
            continue
        if _OTHER_KEY_VALUE.match(line):
            flush()
            group.append(line)
            flush()
            continue
        group.append(line)
    flush()
    return labels, prose


# Tables --------------------------------------------------------------------

_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "account": ("account", "account no", "account no.", "account number", "account reference", "securities account", "custody account", "safekeeping account", "depot", "acct", "a/c"),
    "subfund": ("subfund", "sub-fund", "sub fund", "fund", "fund name", "reported fund", "portfolio", "compartment"),
    "investor": ("investor", "investor name", "investor group", "client", "client name", "beneficial owner", "account holder"),
    "isin": ("isin", "isin code", "security isin"),
    "quantity": ("nominal", "nominal amount", "nominal value", "nominal held", "quantity", "holding", "holding amount", "position", "balance", "units", "face amount", "principal amount"),
    "as_of": ("as of", "as of date", "as-of date", "asof date", "as at", "position date", "holding date", "balance date", "statement date", "valuation date"),
}
_COLUMN_FORMS = {_label_form(label): name for name, labels in _COLUMNS.items() for label in labels}
_TOTAL_LINE = re.compile(r"^\s*(?:grand\s+)?total\b", re.IGNORECASE)
# Lines of one block sitting side by side: far less height per line than text.
_ROW_BLOCK_POINTS_PER_LINE = 7.0
_ROW_BASELINE_TOLERANCE = 3.0

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class _Row:
    blocks: tuple[int, ...]
    cells: tuple[str, ...]
    spans: tuple[tuple[float, float], ...] | None  # x extent per cell, if known
    y: float


@dataclass(frozen=True)
class _Table:
    header: _Row
    columns: Mapping[str, int]
    rows: tuple[_Row, ...]
    total: tuple[Decimal | None, int] | None  # (amount, block) of a "Total" line
    blocks: frozenset[int]


def column_name(cell: str) -> str | None:
    """The holdings field a table or spreadsheet column heading names, if any."""
    text = re.sub(r"\([^)]*\)", " ", cell)
    text = re.sub(r"\b[A-Z]{3}\b\s*$", "", text.strip())  # "Nominal USD"
    return _COLUMN_FORMS.get(_label_form(text)) if text.strip() else None


def _layout_rows(blocks: Sequence[tuple[int, str]], geometry: Mapping[int, Box]) -> list[_Row]:
    rows: list[_Row] = []
    singles: list[tuple[int, str, Box]] = []
    for index, text in blocks:
        box = geometry.get(index)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if box is None or not lines:
            continue
        if len(lines) >= 2 and (box[3] - box[1]) / len(lines) < _ROW_BLOCK_POINTS_PER_LINE:
            rows.append(_Row((index,), tuple(lines), None, (box[1] + box[3]) / 2))
        elif len(lines) == 1:
            singles.append((index, lines[0], box))
    groups: list[list[tuple[int, str, Box]]] = []
    for item in sorted(singles, key=lambda entry: ((entry[2][1] + entry[2][3]) / 2, entry[2][0])):
        middle = (item[2][1] + item[2][3]) / 2
        if groups and abs((groups[-1][0][2][1] + groups[-1][0][2][3]) / 2 - middle) <= _ROW_BASELINE_TOLERANCE:
            groups[-1].append(item)
        else:
            groups.append([item])
    for group in groups:
        group.sort(key=lambda entry: entry[2][0])
        rows.append(_Row(
            tuple(entry[0] for entry in group), tuple(entry[1] for entry in group),
            tuple((entry[2][0], entry[2][2]) for entry in group), (group[0][2][1] + group[0][2][3]) / 2,
        ))
    return sorted(rows, key=lambda row: row.y)


def _header_columns(row: _Row) -> dict[str, int] | None:
    columns: dict[str, int] = {}
    for position, cell in enumerate(row.cells):
        name = column_name(cell)
        if name is not None and name not in columns:
            columns[name] = position
    return columns if {"account", "quantity"} <= set(columns) and len(columns) >= 3 else None


def _aligned(header: _Row, row: _Row) -> tuple[str, ...] | None:
    """Cells of ``row`` in header column order, or None if they cannot be lined up."""
    if len(row.cells) == len(header.cells):
        return row.cells
    if header.spans is None or row.spans is None:
        return None
    cells = [""] * len(header.cells)
    for cell, (left, right) in zip(row.cells, row.spans):
        centre = (left + right) / 2
        position = min(range(len(header.spans)), key=lambda index: abs((header.spans[index][0] + header.spans[index][1]) / 2 - centre))
        if cells[position]:
            return None
        cells[position] = cell
    return tuple(cells)


def _find_tables(blocks: Sequence[tuple[int, str]], geometry: Mapping[int, Box]) -> list[_Table]:
    rows = _layout_rows(blocks, geometry)
    tables: list[_Table] = []
    position = 0
    while position < len(rows):
        columns = _header_columns(rows[position])
        if columns is None:
            position += 1
            continue
        header, data = rows[position], []
        position += 1
        while position < len(rows) and _header_columns(rows[position]) is None and not _TOTAL_LINE.match(rows[position].cells[0]):
            data.append(rows[position])
            position += 1
        used = {index for row in (header, *data) for index in row.blocks}
        total: tuple[Decimal | None, int] | None = None
        bottom = data[-1].y if data else header.y
        for index, text in blocks:
            box = geometry.get(index)
            if index in used or box is None or (box[1] + box[3]) / 2 < bottom or not _TOTAL_LINE.match(text):
                continue
            amounts = find_amounts(_TOTAL_LINE.sub("", text))
            total = (next(iter(amounts)) if len(amounts) == 1 else None, index)
            used.add(index)
            break
        tables.append(_Table(header, columns, tuple(data), total, frozenset(used)))
    return tables


def _table_records(table: _Table, labels: "_Labels", investor: object) -> list[PdfRecord]:
    def cell(cells: Sequence[str], name: str) -> str | None:
        index = table.columns.get(name)
        return cells[index].strip() if index is not None and cells[index].strip() else None

    page_isin, page_as_of = labels.get("isin"), labels.get("as_of")
    records: list[PdfRecord] = []
    skipped, total = 0, Decimal(0)
    for row in table.rows:
        cells = _aligned(table.header, row)
        if cells is None:
            skipped += 1
            continue
        account, quantity_text = cell(cells, "account"), cell(cells, "quantity")
        isin_text, as_of_text = cell(cells, "isin"), cell(cells, "as_of")
        isin = isin_text.upper() if isin_text and _ISIN.fullmatch(isin_text.upper()) else (None if isin_text else page_isin)
        as_of = parse_date(as_of_text) if as_of_text else page_as_of
        amounts = find_amounts(quantity_text) if quantity_text else set()
        if not (account and re.fullmatch(_CODE, account) and isinstance(isin, str) and isinstance(as_of, str) and len(amounts) == 1):
            skipped += 1
            continue
        quantity = next(iter(amounts))
        if quantity <= 0:
            skipped += 1
            continue
        total += quantity
        row_investor = cell(cells, "investor") or (investor if isinstance(investor, str) else None)
        snippet = " | ".join(table.header.cells) + ": " + " | ".join(cells)
        used = {*table.header.blocks, *row.blocks, *(labels.blocks[name] for name in ("as_of", "isin") if name in labels.blocks and not cell(cells, name))}
        records.append(PdfRecord("holding_snapshot", {
            "investor": row_investor, "subfund": cell(cells, "subfund"), "account": account,
            "isin": isin, "quantity": amount_text(quantity), "positionDate": as_of,
        }, snippet[:400], tuple(sorted(used))))
    if table.total is not None:
        expected, total_block = table.total
        if expected is None or skipped or expected != total:
            return []  # the parsed rows do not account for the stated total
        records = [PdfRecord(record.kind, record.payload, record.snippet, tuple(sorted({*record.block_indexes, total_block}))) for record in records]
    return records


# Alias statements ------------------------------------------------------------

_QUOTED = re.compile(r"(?:(?<=^)|(?<=[\s(]))[\"“'‘]([^\"“”'‘’]{1,120}?)[\"”'’](?=[\s,.;:)]|$)")
_IDENTITY_VERB = re.compile(
    r"\b(?:identif(?:y|ies)|refers?\s+to|(?:is|are)\s+(?:also\s+)?(?:known|referred\s+to|designated)\s+as"
    r"|(?:is|are)\s+(?:an?\s+)?(?:abbreviation|short\s+form|alias|shorthand)(?:es|s)?\s+(?:of|for)|denotes?|means?)\b",
    re.IGNORECASE,
)
_SAME_AS = re.compile(r"\bthe\s+same\s+(?P<kind>sub-?funds?|funds?|portfolios?|compartments?|entity|entities|company|investors?|party|account\s+holder)\s+as\s+(?P<target>.+)$", re.IGNORECASE)
_ACCOUNT_CONTEXT = re.compile(r"\b(?:for|in|at|under|on|with)\s+(?:the\s+)?(?:securities\s+|custody\s+|safekeeping\s+)?(?:account|acct\.?|a/c)\b", re.IGNORECASE)


@dataclass(frozen=True)
class AliasStatement:
    """A document's explicit statement that names refer to one record."""
    target_type: str  # investment_vehicle or legal_entity
    aliases: tuple[str, ...]
    canonical: str
    account: str | None
    snippet: str
    block_indexes: tuple[int, ...]


def _alias_kind(word: str | None) -> str | None:
    if word is None:
        return None
    word = word.lower()
    if re.match(r"sub-?funds?|funds?|portfolios?|compartments?", word):
        return "investment_vehicle"
    return "legal_entity"


def extract_page_aliases(blocks: Sequence[tuple[int, str]], geometry: Mapping[int, Box] | None = None) -> list[AliasStatement]:
    """Statements such as "'X' and 'Y' identify the same subfund as 'Z' for account A".

    Only quoted alias names are accepted, the sentence must not be negated,
    and a fund alias without a named kind must state its account.  The caller
    applies a statement only where that account matches the record.
    """
    tables = _find_tables(blocks, geometry) if geometry else []
    table_blocks = {index for table in tables for index in table.blocks}
    _labels, prose = _read_labels([(index, text) for index, text in blocks if index not in table_blocks])
    statements: list[AliasStatement] = []
    for block, sentence in _sentences(prose):
        if _NEGATION.search(sentence):
            continue
        verb = _IDENTITY_VERB.search(sentence)
        if verb is None:
            continue
        aliases = [match.group(1).strip() for match in _QUOTED.finditer(sentence[:verb.start()])]
        if not aliases:
            continue
        after = sentence[verb.end():]
        same = _SAME_AS.search(after)
        kind = _alias_kind(same.group("kind")) if same else None
        target_text = same.group("target") if same else after
        target_text = _ACCOUNT_CONTEXT.split(target_text, maxsplit=1)[0].strip(" .;,")
        quoted_target = _QUOTED.match(target_text)
        canonical = quoted_target.group(1).strip() if quoted_target else _trim_name(target_text)
        accounts = {match.group("code") for match in _ACCOUNT_PHRASE.finditer(sentence)}
        account = next(iter(accounts)) if len(accounts) == 1 else None
        if kind is None and account is not None:
            kind = "investment_vehicle"
        if kind is None or not canonical:
            continue
        distinct = tuple(dict.fromkeys(alias for alias in aliases if alias.casefold() != canonical.casefold()))
        if distinct:
            statements.append(AliasStatement(kind, distinct, canonical, account, sentence[:400], (block,)))
    return statements


# Account migrations ------------------------------------------------------------

_ACCOUNT_TOKEN = r"(?=[A-Za-z0-9\-/.]*\d)(?=[A-Za-z0-9\-/.]*[A-Za-z])[A-Za-z0-9](?:[A-Za-z0-9\-/.]*[A-Za-z0-9])?"
_ACCOUNT_REF = rf"(?:(?:securities\s+|custody\s+|safekeeping\s+)?(?:account|acct\.?|a/c)(?:\s+(?:no\.?|number))?\s*:?\s*)?(?P<{{name}}>{_ACCOUNT_TOKEN})"
_MIGRATION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    _ACCOUNT_REF.format(name="old") + r"\s+(?:was|has\s+been|is|will\s+be)\s+(?:replaced|superseded|succeeded)\s+by\s+(?:the\s+new\s+)?" + _ACCOUNT_REF.format(name="new"),
    _ACCOUNT_REF.format(name="old") + r"\s+(?:was|has\s+been|is|will\s+be)\s+(?:migrated|renumbered|re-?designated|changed|converted)\s+(?:in)?to\s+(?:the\s+new\s+)?" + _ACCOUNT_REF.format(name="new"),
    r"\b(?:account\s+)?(?:migration|renumbering|change)\s+from\s+" + _ACCOUNT_REF.format(name="old") + r"\s+to\s+" + _ACCOUNT_REF.format(name="new"),
    r"\b(?:new\s+)?" + _ACCOUNT_REF.format(name="new") + r"\s+(?:replaces|supersedes|succeeds)\s+(?:the\s+former\s+|the\s+old\s+)?" + _ACCOUNT_REF.format(name="old"),
))
_HOLDER_FUND = re.compile(r"\bfor\s+(?P<holder>[A-Z][^,;]{1,80}?)['’]s\s+(?P<fund>[A-Z][^,;.]{1,100})")


@dataclass(frozen=True)
class AccountMigration:
    """A document's statement that one custody account replaced another."""
    old_account: str
    new_account: str
    effective_date: str | None
    investor: str | None
    fund: str | None
    snippet: str
    block_indexes: tuple[int, ...]


def extract_page_migrations(blocks: Sequence[tuple[int, str]], geometry: Mapping[int, Box] | None = None, document_investor: str | None = None) -> list[AccountMigration]:
    """Statements such as "Custody account X was replaced by Y for <investor>'s <fund>".

    Only an explicit replacement or migration wording between two account
    identifiers counts; matching names or investors never imply one.  The
    effective date comes from the sentence or the page's "Effective date"
    label; the investor and fund from "for <investor>'s <fund>" or labels.
    """
    tables = _find_tables(blocks, geometry) if geometry else []
    table_blocks = {index for table in tables for index in table.blocks}
    labels, prose = _read_labels([(index, text) for index, text in blocks if index not in table_blocks])
    page_investor = labels.get("investor") if isinstance(labels.get("investor"), str) else document_investor
    migrations: list[AccountMigration] = []
    for block, sentence in _sentences(prose):
        if _NEGATION.search(sentence):
            continue
        match = next((found for pattern in _MIGRATION_PATTERNS if (found := pattern.search(sentence))), None)
        if (
            match is None or match.group("old").casefold() == match.group("new").casefold()
            or _ISIN.fullmatch(match.group("new").upper())
            or not re.search(r"\b(?:account|acct|a/c)\b", match.group(0), re.IGNORECASE)
        ):
            continue
        effective = _keyword_date(sentence, r"effective|with\s+effect\s+from|as\s+of|on|from") or labels.get("effective_date")
        holder = _HOLDER_FUND.search(sentence)
        investor = _trim_name(holder.group("holder")) if holder else page_investor
        fund = _trim_name(holder.group("fund")) if holder else (labels.get("subfund") if isinstance(labels.get("subfund"), str) else None)
        used = {block, *(labels.blocks[name] for name in ("effective_date", "investor") if name in labels.blocks)}
        migrations.append(AccountMigration(
            match.group("old"), match.group("new"), effective if isinstance(effective, str) else None,
            investor if isinstance(investor, str) else None, fund, sentence[:400], tuple(sorted(used)),
        ))
    return migrations


# Records -------------------------------------------------------------------

@dataclass(frozen=True)
class PdfRecord:
    kind: str
    payload: Mapping[str, object]
    snippet: str
    block_indexes: tuple[int, ...]


def _sentences(prose: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for block, text in prose:
        for sentence in re.split(r"(?<=[.;!?])\s+(?=[A-Z(\"'])", text):
            if sentence.strip():
                result.append((block, sentence.strip()))
    return result


def _keyword_date(sentence: str, keywords: str) -> str | None:
    """The single date in a sentence preceded by one of ``keywords``."""
    values: list[str | None] = []
    for value, start in find_dates(sentence):
        window = sentence[max(0, start - 45):start]
        if re.search(rf"\b(?:{keywords})\s*(?:as\s+(?:of|at)|from|on)?\s*:?\s*(?:the\s+)?$", window, re.IGNORECASE):
            values.append(value)
    if not values or any(value is None for value in values):
        return None
    return _single(values)


def _sentence_isin(sentence: str, accounts: Iterable[str]) -> str | None:
    labelled = [match.group(1).upper() for match in _ISIN_LABELLED.finditer(sentence)]
    if labelled:
        return _single(labelled)
    excluded = {account.upper() for account in accounts}
    return _single(match.group(0) for match in _ISIN.finditer(sentence) if match.group(0) not in excluded)


def _tied_quantity(sentence: str) -> str | None:
    amounts = set()
    for match in _TIED_AMOUNT.finditer(sentence):
        amount = parse_amount(match.group("a") or match.group("b"))
        if amount is None:
            return None
        amounts.add(amount)
    return amount_text(next(iter(amounts))) if len(amounts) == 1 and next(iter(amounts)) > 0 else None


_TRANSFER_SIDE_END = re.compile(r",?\s+(?:effective|with\s+effect|value\s+dated?|on\s+(?=\d)|as\s+of|dated)\b.*$|[.;]\s*$", re.IGNORECASE)


def _transfer_sides(sentence: str) -> tuple[tuple[str | None, str], tuple[str | None, str]] | None:
    def side(text: str) -> tuple[str | None, str] | None:
        name, account = _party(_TRANSFER_SIDE_END.sub("", text))
        return (name, account) if account else None

    pairs: list[tuple[str, str]] = []
    for start in re.finditer(r"\bfrom\b", sentence, re.IGNORECASE):
        rest = sentence[start.end():]
        for to in re.finditer(r"\b(?:to|into)\b", rest, re.IGNORECASE):
            pairs.append((rest[:to.start()], rest[to.end():]))
    for start in re.finditer(r"\bto\b", sentence, re.IGNORECASE):
        rest = sentence[start.end():]
        for origin in re.finditer(r"\bfrom\b", rest, re.IGNORECASE):
            pairs.append((rest[origin.end():], rest[:origin.start()]))
    debit = re.search(r"\bdebit(?:ed)?\b(?:\s+from)?(?P<a>.+?)\bcredit(?:ed)?\b(?:\s+to)?(?P<b>.+)$", sentence, re.IGNORECASE)
    if debit:
        pairs.append((debit.group("a"), debit.group("b")))
    for source_text, destination_text in pairs:
        source, destination = side(source_text), side(destination_text)
        if source and destination and source[1] != destination[1]:
            return source, destination
    return None


def _subject_before(sentence: str, verb: re.Pattern[str]) -> str | None:
    match = verb.search(sentence)
    if match is None:
        return None
    subject = re.sub(r"\b(?:hereby|has|have|irrevocably|unconditionally)\b", " ", sentence[:match.start()], flags=re.IGNORECASE)
    segments = [segment.strip() for segment in subject.split(",") if segment.strip()]
    if not segments:
        return None
    candidate = segments[-1]
    if candidate.lower().rstrip(".") in {form.rstrip(".") for form in _LEGAL_FORM_ONLY} and len(segments) > 1:
        candidate = f"{segments[-2]}, {candidate}"
    return _trim_name(candidate)


def _holding_subfund(sentence: str, account: str) -> str | None:
    code = re.escape(account)
    for pattern in (
        rf"account\s+(?:no\.?\s*|number\s*)?{code}\s*,\s*(?P<n>[^,]{{2,80}}?)\s*,",
        rf"account\s+(?:no\.?\s*|number\s*)?{code}\s*\((?P<n>[^)]{{2,80}})\)",
        rf"(?P<n>[A-Z][^,]{{1,80}}?)\s*,?\s*\(?\s*(?:securities\s+|custody\s+)?account\s+(?:no\.?\s*|number\s*)?{code}",
    ):
        match = re.search(pattern, sentence, re.IGNORECASE)
        if match:
            name = _trim_name(match.group("n"))
            if name and "account" not in name.lower():
                return name
    return _subject_before(sentence, re.compile(r"\b(?:holds|held)\b", re.IGNORECASE))


def extract_page_records(
    blocks: Sequence[tuple[int, str]], document_investor: str | None = None, geometry: Mapping[int, Box] | None = None,
) -> tuple[list[PdfRecord], str | None, int | None]:
    """Records on one page, plus its labelled issue date and that date's block.

    ``geometry`` maps block index to its (x0, y0, x1, y1) box; without it
    tables are not read.  Table blocks are kept out of label and prose reading
    so a row is never also misread as a labelled field.
    """
    tables = _find_tables(blocks, geometry) if geometry else []
    table_blocks = {index for table in tables for index in table.blocks}
    labels, prose = _read_labels([(index, text) for index, text in blocks if index not in table_blocks])
    investor = labels.get("investor") if isinstance(labels.get("investor"), str) else document_investor
    investor_blocks = (labels.blocks["investor"],) if "investor" in labels.blocks else ()
    records: list[PdfRecord] = []
    page_isin = labels.get("isin") if isinstance(labels.get("isin"), str) else None

    for block, sentence in _sentences(prose):
        if _NEGATION.search(sentence):
            continue
        accounts = [match.group("code") for match in _ACCOUNT_PHRASE.finditer(sentence)]
        isin = _sentence_isin(sentence, accounts) or page_isin
        if _ASSIGN_WORD.search(sentence) and _CLAIM_WORD.search(sentence):
            reference_match = re.search(rf"\bclaim\s+(?:reference|ref\.?|no\.?|number|id)\s*:?\s*({_CODE})", sentence, re.IGNORECASE)
            claim_reference = reference_match.group(1) if reference_match else labels.get("claim_reference")
            assignor = labels.get("assignor") or _subject_before(sentence, _ASSIGN_WORD)
            after_verb = sentence[_ASSIGN_WORD.search(sentence).end():]  # type: ignore[union-attr]
            to_match = re.search(r"\bto\s+(?P<to>[A-Z].{0,140})", after_verb)
            assignee = labels.get("assignee") or (_trim_name(_TRANSFER_SIDE_END.sub("", to_match.group("to"))) if to_match else None)
            effective = _keyword_date(sentence, r"effective|with\s+effect\s+from|as\s+of|on") or labels.get("effective_date")
            percentages = {parse_amount(match.group("n")) for match in _PERCENT.finditer(sentence)}
            percentage = labels.get("percentage")
            if percentage is None and len(percentages) == 1 and None not in percentages:
                value = next(iter(percentages))
                percentage = amount_text(value) if value is not None and 0 < value <= 100 else None
            if all(isinstance(item, str) for item in (claim_reference, assignor, assignee, isin, effective)) and assignor != assignee:
                payload: dict[str, object] = {"claimReference": claim_reference, "fromEntity": assignor, "toEntity": assignee, "isin": isin, "effectiveDate": effective}
                if percentage is not None:
                    payload["percentage"] = percentage
                used = {block, *(labels.blocks[name] for name in ("claim_reference", "assignor", "assignee", "effective_date", "percentage", "isin") if name in labels.blocks)}
                records.append(PdfRecord("claim_assignment", payload, sentence[:400], tuple(sorted(used))))
            continue
        if _TRANSFER_WORD.search(sentence) and len(accounts) >= 2 and not _MIGRATION_WORD.search(sentence):
            sides = _transfer_sides(sentence)
            quantity = _tied_quantity(sentence) or labels.get("quantity")
            effective = _keyword_date(sentence, r"effective|with\s+effect\s+from|value\s+date[d]?|settle(?:d|ment\s+date)|on") or labels.get("effective_date")
            if sides and all(isinstance(item, str) for item in (isin, quantity, effective)):
                (from_name, from_account), (to_name, to_account) = sides
                used = {block, *investor_blocks, *(labels.blocks[name] for name in ("quantity", "effective_date", "isin") if name in labels.blocks)}
                records.append(PdfRecord("securities_transfer", {
                    "investor": investor, "fromSubfund": from_name, "fromAccount": from_account, "toSubfund": to_name, "toAccount": to_account,
                    "isin": isin, "quantity": quantity, "effectiveDate": effective,
                }, sentence[:400], tuple(sorted(used))))
            continue
        if _HOLDING_WORD.search(sentence) and len(set(accounts)) == 1 and not _TRANSFER_WORD.search(sentence):
            account = accounts[0]
            quantity = _tied_quantity(sentence)
            as_of = _keyword_date(sentence, r"as\s+(?:of|at|on)|at\s+close\s+of\s+business\s+on|on|at") or labels.get("as_of")
            if all(isinstance(item, str) for item in (isin, quantity, as_of)):
                used = {block, *investor_blocks, *(labels.blocks[name] for name in ("as_of", "isin") if name in labels.blocks)}
                records.append(PdfRecord("holding_snapshot", {
                    "investor": investor, "subfund": _holding_subfund(sentence, account), "account": account,
                    "isin": isin, "quantity": quantity, "positionDate": as_of,
                }, sentence[:400], tuple(sorted(used))))

    for table in tables:
        records.extend(_table_records(table, labels, investor))
    kinds = {record.kind for record in records}
    labelled = _labelled_record(labels, investor)
    if labelled is not None and labelled.kind not in kinds:
        records.append(labelled)
    issue_date = labels.get("issue_date")
    return records, issue_date if isinstance(issue_date, str) else None, labels.blocks.get("issue_date")


def _labelled_record(labels: _Labels, investor: object) -> PdfRecord | None:
    def text(name: str) -> str | None:
        value = labels.get(name)
        return value if isinstance(value, str) else None

    def party(name: str) -> tuple[str | None, str | None]:
        value = labels.get(name)
        return value if isinstance(value, tuple) else (None, None)

    def record(kind: str, payload: dict[str, object], names: Sequence[str]) -> PdfRecord:
        used = [name for name in names if name in labels.blocks]
        return PdfRecord(kind, payload, "; ".join(labels.lines[name] for name in used)[:400], tuple(sorted({labels.blocks[name] for name in used})))

    from_name, from_party_account = party("from_party")
    to_name, to_party_account = party("to_party")
    from_account, to_account = text("from_account") or from_party_account, text("to_account") or to_party_account
    isin, quantity = text("isin"), text("quantity")
    if from_account and to_account and from_account != to_account and isin and quantity and text("effective_date"):
        return record("securities_transfer", {
            "investor": investor if isinstance(investor, str) else None, "fromSubfund": from_name, "fromAccount": from_account,
            "toSubfund": to_name, "toAccount": to_account, "isin": isin, "quantity": quantity, "effectiveDate": text("effective_date"),
        }, ("from_party", "from_account", "to_party", "to_account", "isin", "quantity", "effective_date", "investor"))
    assignor, assignee = text("assignor"), text("assignee")
    if text("claim_reference") and assignor and assignee and assignor != assignee and isin and text("effective_date"):
        payload: dict[str, object] = {"claimReference": text("claim_reference"), "fromEntity": assignor, "toEntity": assignee, "isin": isin, "effectiveDate": text("effective_date")}
        if text("percentage"):
            payload["percentage"] = text("percentage")
        return record("claim_assignment", payload, ("claim_reference", "assignor", "assignee", "isin", "effective_date", "percentage"))
    account = text("account")
    if account and isin and quantity and text("as_of") and not (from_account or to_account):
        return record("holding_snapshot", {
            "investor": investor if isinstance(investor, str) else None, "subfund": text("subfund"), "account": account,
            "isin": isin, "quantity": quantity, "positionDate": text("as_of"),
        }, ("account", "subfund", "isin", "quantity", "as_of", "investor"))
    return None


def labelled_value(blocks: Sequence[tuple[int, str]], name: str) -> tuple[object | None, int | None]:
    """One labelled field across blocks (used for document-level issue dates)."""
    labels, _prose = _read_labels(blocks)
    return labels.get(name), labels.blocks.get(name)
