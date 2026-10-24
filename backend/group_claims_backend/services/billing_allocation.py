"""Structured result of a document-backed pro-rata allocation.

The cent amounts come from ``MatterReviewService._allocate_pro_rata``
unchanged; this module only describes them: the instruction's stated
inputs and where each is written, every participant the instruction names
(eligible or excluded, with its stated reason), subfunds the scoped
spreadsheets list that the instruction does not name (eligibility
undetermined), and a narrow cross-check of each eligible amount against
holding records in scope dated on the comparison date.

The cross-check only reports.  A differing record is an unresolved
discrepancy; it never replaces an instruction amount and it is not a full
reconciliation.  Nothing here persists data or contacts an AI provider.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from .document_references import question_dates
from .document_search import DocumentSearchIndex
from .entity_inventory import entity_key
from .holdings_comparison import DatedRecord, HoldingsComparison, HoldingsDocument

ROUNDING_POLICY = (
    "Each amount is eligible amount ÷ total eligible amount × fee pool, truncated to the cent. The cents left "
    "over go one at a time to the largest truncated remainders (ties in a fixed participant order), so the "
    "amounts add up exactly to the fee pool."
)
FORMULA = "eligible nominal amount ÷ total eligible nominal amount × fee pool"
MAX_UNDETERMINED = 200
MAX_PDF_HITS = 200

_CENT = Decimal("0.01")
_SHARE_PLACES = Decimal("0.0000000001")
_PERCENT_PLACES = Decimal("0.0001")
_UNROUNDED_PLACES = Decimal("0.000001")
_LABELLED = re.compile(r"^(?P<name>.+?)\s*\((?P<identifier>[^()]+)\)$")
_LISTED_EXCLUSION = re.compile(
    r"Excluded\s+participants?(?:\s+\d+)?\s*\n\s*([^\n(]+?)\s*\(([^)]+)\)\s*:\s*([^\n]+)", re.IGNORECASE,
)
# "Meridian Income Fund C (SF-C) is excluded because ...".  The name is a run
# of capitalised words so it cannot start mid-sentence or cross a full stop.
_SENTENCE_EXCLUSION = re.compile(
    r"(?<![\w,.\-])(?P<name>[A-Z][\w&'’\-]*(?:\s+[A-Z0-9][\w&'’\-]*)*?)\s*\((?P<identifier>[^()]{1,40})\)\s+is\s+(?:also\s+)?"
    r"excluded\b(?:[^.]*?\b(?:because|as|since)\s+(?P<reason>[^.]+))?"
)
_LABEL_LINE = re.compile(r"^\s*(?P<label>Approved\s+by|Investor\s+group|Investor)\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_AS_OF = re.compile(r"\bas[\s-](?:of|at)(?:\s+date)?\s*:?\s*", re.IGNORECASE)
_ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
_HOLDING_CUE = re.compile(r"\b(?:holdings?|positions?|balances?|held)\b", re.IGNORECASE)
_NOMINAL_QUANTITY = re.compile(r"(?<![\d.,])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s+(?:nominal|units|shares)\b", re.IGNORECASE)
_SUBFUND_KINDS = ("subfund", "fund", "holder")


@dataclass(frozen=True)
class InstructionBlock:
    document_id: str
    extraction_id: str
    filename: str
    reference: str
    text: str


@dataclass(frozen=True)
class ScopeDocument:
    document_id: str
    filename: str
    extraction_id: str
    spreadsheet: bool


@dataclass(frozen=True)
class Exclusion:
    name: str
    identifier: str | None
    reason: str
    # "listed": "Excluded participant / Name (ID): withdrawn"; "sentence": "Name (ID) is excluded because …".
    form: str

    @property
    def phrase(self) -> str:
        label = f"{self.name} ({self.identifier})" if self.identifier else self.name
        if not self.reason:
            return f"{label} is excluded by the instruction."
        if self.form == "listed":
            return f"{label} is excluded by the instruction because it is recorded as {self.reason}."
        return f"{label} is excluded by the instruction because {self.reason}."


@dataclass(frozen=True)
class Participant:
    name: str
    identifier: str | None
    status: str  # eligible | excluded
    eligible_amount: Decimal | None = None


@dataclass
class CrossCheck:
    checks: list[dict[str, object]] = field(default_factory=list)
    undetermined: list[dict[str, object]] = field(default_factory=list)
    comparison_date: str | None = None
    date_basis: str = ""
    documents_checked: int = 0
    limitations: list[str] = field(default_factory=list)
    # (document_id, extraction_id, reference, filename) for each record shown.
    citations: list[tuple[str, str, str, str]] = field(default_factory=list)


def split_label(label: str) -> tuple[str, str | None]:
    match = _LABELLED.match(label.strip())
    return (match.group("name").strip(), match.group("identifier").strip()) if match else (label.strip(), None)


def exclusions(source_text: str) -> list[Exclusion]:
    """Participants the instruction expressly excludes, in source order."""
    found: list[tuple[int, Exclusion]] = []
    for match in _LISTED_EXCLUSION.finditer(source_text):
        found.append((match.start(), Exclusion(match.group(1).strip(), match.group(2).strip(), match.group(3).strip().rstrip("."), "listed")))
    normalized = " ".join(source_text.split())
    for match in _SENTENCE_EXCLUSION.finditer(normalized):
        reason = (match.group("reason") or "").strip()
        found.append((len(source_text) + match.start(), Exclusion(match.group("name").strip(), match.group("identifier").strip(), reason, "sentence")))
    unique: list[Exclusion] = []
    seen: set[tuple[str, str | None]] = set()
    for _position, exclusion in sorted(found, key=lambda item: item[0]):
        key = (entity_key(exclusion.name), exclusion.identifier)
        if key not in seen:
            seen.add(key)
            unique.append(exclusion)
    return unique


def missing_inputs(source_text: str) -> list[str]:
    """What an instruction-like page lacks for a document-backed allocation (same rules as the parser)."""
    folded = " ".join(source_text.split()).casefold()
    missing: list[str] = []
    if "approved" not in folded:
        missing.append("a statement that the instruction is approved")
    if re.search(r"\b[A-Z]{3}\s+[0-9][0-9,]*(?:\.[0-9]{1,2})?\b", source_text) is None or "fee pool" not in folded:
        missing.append("the fee pool amount and currency")
    if re.search(r"Billing\s+period\s*\n\s*[^\n]+", source_text, flags=re.IGNORECASE) is None:
        missing.append("the billing period")
    if "allocation rule" not in folded or "eligible nominal amount / total eligible nominal amount" not in folded:
        missing.append("the allocation rule (eligible nominal amount / total eligible nominal amount)")
    if not re.search(
        r"Eligible\s+participant(?:\s+\d+)?\s*\n\s*[^\n(]+?\s*\([^)]+\)\s*:\s*[0-9]", source_text, flags=re.IGNORECASE,
    ):
        missing.append("the eligible participants with their eligible nominal amounts")
    return missing


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _block(blocks: Sequence[InstructionBlock], *needles: str) -> InstructionBlock | None:
    wanted = [_normal(needle) for needle in needles if needle]
    return next((block for block in blocks if all(needle in _normal(block.text) for needle in wanted)), None)


def _source(block: InstructionBlock | None) -> dict[str, str] | None:
    return {"documentId": block.document_id, "filename": block.filename, "reference": block.reference} if block else None


def _decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _first_dated(text: str) -> str | None:
    for date in question_dates(text):
        if date.year is not None:
            return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
    return None


def _as_of(text: str) -> str | None:
    """The single date a passage states as its as-of date, if any."""
    dates = {_first_dated(text[match.end():match.end() + 40]) for match in _AS_OF.finditer(text)} - {None}
    return next(iter(dates)) if len(dates) == 1 else None


def comparison_date(instruction_text: str, period: str | None) -> tuple[str | None, str]:
    stated = _as_of(" ".join(instruction_text.split()))
    if stated:
        return stated, "the as-of date stated in the instruction"
    if period:
        dated = [date for date in question_dates(period) if date.year is not None]
        if dated:
            end = dated[-1]
            return f"{end.year:04d}-{end.month:02d}-{end.day:02d}", "the billing-period end (the instruction states no as-of date)"
    return None, ""


def instruction_isin(instruction_text: str) -> str | None:
    found = set(_ISIN.findall(instruction_text))
    return next(iter(found)) if len(found) == 1 else None


def _mentions(text: str, participant: Participant) -> bool:
    if participant.identifier and re.search(
        rf"(?<![A-Za-z0-9-]){re.escape(participant.identifier)}(?![A-Za-z0-9-])", text,
    ):
        return True
    name = entity_key(participant.name)
    return bool(name) and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", _normal(text)) is not None


def _matches_name(value: str, participant: Participant) -> bool:
    name, identifier = split_label(value)
    if participant.identifier and identifier and identifier.casefold() == participant.identifier.casefold():
        return True
    return entity_key(name) == entity_key(participant.name) or _mentions(value, participant)


def cross_check(
    connection: sqlite3.Connection,
    documents: Sequence[ScopeDocument],
    instruction_document_id: str,
    participants: Sequence[Participant],
    on_date: str | None,
    date_basis: str,
    isin: str | None,
) -> CrossCheck:
    """Compare each eligible amount with holding records in scope dated ``on_date``.

    A record is used only when it names exactly one instruction participant
    and states one nominal quantity for it on that date (and the same ISIN,
    when the instruction names one).  Anything less certain is skipped.
    """
    result = CrossCheck(comparison_date=on_date, date_basis=date_basis)
    eligible = [participant for participant in participants if participant.status == "eligible"]
    if on_date is None:
        result.limitations.append(
            "The instruction states no as-of date or billing-period end, so its amounts were not compared with holding records."
        )
    spreadsheets = [document for document in documents if document.spreadsheet and document.document_id != instruction_document_id]
    reader = HoldingsComparison(connection, [HoldingsDocument(d.document_id, d.filename, d.extraction_id) for d in spreadsheets])
    undetermined: dict[str, dict[str, object]] = {}
    for document in spreadsheets:
        records = reader.dated_records(HoldingsDocument(document.document_id, document.filename, document.extraction_id))
        if records:
            result.documents_checked += 1
        matched: dict[int, list[DatedRecord]] = {}
        for record in records:
            names = [record.entities[kind] for kind in _SUBFUND_KINDS if kind in record.entities]
            owners = {
                index for index, participant in enumerate(participants)
                if any(_matches_name(name, participant) for name, _reference in names)
            }
            if not owners:
                for name, reference in names[:1]:
                    key = entity_key(name)
                    if key and key not in undetermined and len(undetermined) < MAX_UNDETERMINED:
                        label_name, identifier = split_label(name)
                        undetermined[key] = {
                            "name": label_name, "identifier": identifier,
                            "source": {"documentId": document.document_id, "filename": document.filename, "reference": reference},
                            "extractionId": document.extraction_id,
                        }
                continue
            if len(owners) == 1 and on_date and record.date == on_date and record.quantity is not None and (isin is None or record.isin == isin):
                matched.setdefault(next(iter(owners)), []).append(record)
        for index, records_for in matched.items():
            participant = participants[index]
            if participant.status != "eligible" or participant.eligible_amount is None:
                continue
            if len(records_for) != 1:
                result.limitations.append(
                    f"{document.filename} has {len(records_for)} records for {participant.name} on {on_date} "
                    "(several accounts or securities); they were not compared with the instruction."
                )
                continue
            record = records_for[0]
            assert record.quantity is not None
            _add_check(result, participant, record.quantity, on_date,
                       document.document_id, document.extraction_id, document.filename, record.quantity_reference)

    pdfs = [document for document in documents if not document.spreadsheet and document.document_id != instruction_document_id]
    if pdfs and eligible and on_date:
        terms = [term for participant in eligible for term in (participant.identifier, participant.name) if term]
        hits = DocumentSearchIndex.search(
            connection, [document.extraction_id for document in pdfs], terms,
            per_document_limit=20, total_limit=MAX_PDF_HITS,
        ).hits
        names = {document.document_id: document.filename for document in pdfs}
        result.documents_checked += len({hit.document_id for hit in hits})
        for hit in hits:
            if hit.source_kind != "pdf_text_block":
                continue
            text = " ".join(hit.text.split())
            mentioned = [participant for participant in participants if _mentions(text, participant)]
            if len(mentioned) != 1 or mentioned[0].status != "eligible" or not _HOLDING_CUE.search(text):
                continue
            quantities = {Decimal(value.replace(",", "")) for value in _NOMINAL_QUANTITY.findall(text)}
            stated_isins = set(_ISIN.findall(text))
            if len(quantities) != 1 or (isin is not None and stated_isins and stated_isins != {isin}):
                continue
            if (_as_of(text) or _page_as_of(connection, hit.extraction_id, hit.page_number)) != on_date:
                continue
            _add_check(result, mentioned[0], next(iter(quantities)), on_date,
                       hit.document_id, hit.extraction_id, names.get(hit.document_id, ""), hit.source_reference)
    result.undetermined = list(undetermined.values())
    if not result.documents_checked:
        result.limitations.append("No other document in scope records holdings for the instruction's participants.")
    return result


def _page_as_of(connection: sqlite3.Connection, extraction_id: str, page_number: int | None) -> str | None:
    if page_number is None:
        return None
    rows = connection.execute(
        "SELECT b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id "
        "WHERE p.extraction_id = ? AND p.page_number = ? ORDER BY b.block_index",
        (extraction_id, page_number),
    ).fetchall()
    return _as_of(" ".join(" ".join(str(row["text"] or "") for row in rows).split()))


def _add_check(
    result: CrossCheck, participant: Participant, recorded: Decimal, on_date: str | None,
    document_id: str, extraction_id: str, filename: str, reference: str,
) -> None:
    assert participant.eligible_amount is not None
    result.checks.append({
        "participant": participant.name,
        "identifier": participant.identifier,
        "status": "consistent" if recorded == participant.eligible_amount else "conflict",
        "instructionAmount": _decimal_string(participant.eligible_amount),
        "recordedAmount": _decimal_string(recorded),
        "asOfDate": on_date,
        "source": {"documentId": document_id, "filename": filename, "reference": reference},
    })
    result.citations.append((document_id, extraction_id, reference, filename))


def allocation_payload(
    blocks: Sequence[InstructionBlock],
    *,
    currency: str | None,
    fee_pool: Decimal | None,
    period: str | None,
    allocations: Sequence[tuple[str, Decimal, Decimal]] = (),
    excluded: Sequence[Exclusion] = (),
    checks: CrossCheck | None = None,
    missing: Sequence[str] = (),
    issues: Sequence[str] = (),
) -> dict[str, object]:
    """Describe one allocation (``allocations`` = label, eligible amount, allocated cents) for display."""
    instruction_text = "\n".join(block.text for block in blocks)
    complete = bool(allocations) and fee_pool is not None and currency is not None and not missing
    first = blocks[0] if blocks else None
    labels = {match.group("label").casefold(): (match.group("value"), match) for match in _LABEL_LINE.finditer(instruction_text)}

    def labelled(*names: str) -> dict[str, object] | None:
        for name in names:
            if name in labels:
                value = labels[name][0]
                block = _block(blocks, value)
                return {"text": value, "source": _source(block)}
        return None

    total = sum((eligible for _label, eligible, _fee in allocations), Decimal("0"))
    participants: list[dict[str, object]] = []
    for label, eligible, fee in allocations:
        name, identifier = split_label(label)
        share = eligible / total if total else Decimal("0")
        exact = fee_pool * share if fee_pool is not None else Decimal("0")
        floored = exact.quantize(_CENT, rounding=ROUND_DOWN)
        participants.append({
            "name": name, "identifier": identifier, "status": "eligible",
            "eligibleAmount": _decimal_string(eligible),
            "share": format(share.quantize(_SHARE_PLACES, rounding=ROUND_HALF_EVEN), "f"),
            "sharePercent": format((share * 100).quantize(_PERCENT_PLACES, rounding=ROUND_HALF_EVEN), "f"),
            "allocatedAmount": format(fee.quantize(_CENT), "f"),
            "unroundedAmount": format(exact.quantize(_UNROUNDED_PLACES, rounding=ROUND_HALF_EVEN), "f"),
            "roundingAdjustment": format((fee - floored).quantize(_CENT), "f"),
            "reason": None,
            "source": _source(_block(blocks, identifier or name, _decimal_grouped(eligible)) or _block(blocks, identifier or name)),
        })
    eligible_keys = {(entity_key(row["name"]), row["identifier"]) for row in participants}  # type: ignore[arg-type]
    issue_list = list(issues)
    for exclusion in excluded:
        if (entity_key(exclusion.name), exclusion.identifier) in eligible_keys:
            issue_list.append(f"{exclusion.name} is named both as eligible and as excluded in the instruction.")
            continue
        participants.append({
            "name": exclusion.name, "identifier": exclusion.identifier, "status": "excluded",
            "eligibleAmount": None, "share": "0" if complete else None, "sharePercent": "0" if complete else None,
            # A pro-rata pool shared among the eligible participants leaves an
            # expressly excluded participant with no part of it.
            "allocatedAmount": "0.00" if complete else None,
            "unroundedAmount": None, "roundingAdjustment": None,
            "reason": exclusion.reason or None,
            "source": _source(_block(blocks, exclusion.identifier or exclusion.name, "exclu") or _block(blocks, exclusion.name)),
        })
    for entry in (checks.undetermined if checks else []):
        participants.append({
            "name": entry["name"], "identifier": entry["identifier"], "status": "undetermined",
            "eligibleAmount": None, "share": None, "sharePercent": None, "allocatedAmount": None,
            "unroundedAmount": None, "roundingAdjustment": None,
            "reason": "Listed in the documents in scope but not named in the billing instruction, so its eligibility is not established.",
            "source": entry["source"],
        })
    allocated = sum((fee for _label, _eligible, fee in allocations), Decimal("0"))
    conflicts = [check for check in (checks.checks if checks else []) if check["status"] == "conflict"]
    rule_block = _block(blocks, "allocation rule")
    rule_text = None
    if rule_block is not None:
        match = re.search(r"Allocation\s+rule\s*:?\s*(.+)", rule_block.text, flags=re.IGNORECASE | re.DOTALL)
        rule_text = " ".join((match.group(1) if match else rule_block.text).split())[:240]
    period_block = _block(blocks, "billing period", period or "")
    fee_block = _block(blocks, "fee pool", currency or "") if fee_pool is not None else None
    return {
        "calculationStatus": "complete" if complete else "incomplete",
        "resultType": "provisional_allocation",
        # ClaimFlow does not issue invoices from Matter Review.
        "invoiceStatus": "not_issued",
        "issuanceBlocked": bool(conflicts) or not complete,
        "instruction": {"documentId": first.document_id, "filename": first.filename} if first else None,
        "approvedBy": labelled("approved by"),
        "investorGroup": labelled("investor group", "investor"),
        "period": {"text": period, "source": _source(period_block)} if period else None,
        "currency": currency,
        "feePool": {"amount": format(fee_pool.quantize(_CENT), "f"), "source": _source(fee_block)} if fee_pool is not None else None,
        "rule": {"text": rule_text, "formula": FORMULA, "source": _source(rule_block)} if rule_text else None,
        "roundingPolicy": ROUNDING_POLICY,
        "totals": {
            "eligibleAmount": _decimal_string(total),
            "allocatedAmount": format(allocated.quantize(_CENT), "f"),
            "remainder": format(((fee_pool or Decimal("0")) - allocated).quantize(_CENT), "f"),
            "sharePercent": "100.0000",
        } if complete else None,
        "participants": participants,
        "crossChecks": checks.checks if checks else [],
        "crossCheck": {
            "comparisonDate": checks.comparison_date if checks else None,
            "dateBasis": checks.date_basis if checks else "",
            "documentsChecked": checks.documents_checked if checks else 0,
            "limitations": checks.limitations if checks else [],
        },
        "missing": list(missing),
        "issues": issue_list,
    }


def _decimal_grouped(value: Decimal) -> str:
    whole, _, fraction = _decimal_string(value).partition(".")
    return f"{int(whole):,}" + (f".{fraction}" if fraction else "")
