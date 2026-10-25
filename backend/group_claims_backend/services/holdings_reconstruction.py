"""Chronological, cited reconstruction of one subject's securities holdings.

For a named subfund (or fund) this collects, from the documents in scope:

* dated holding records in spreadsheets (the same header rules as a
  holdings comparison) and dated holding statements in PDF text blocks;
* effective-dated securities transfers that name the subject on exactly one
  side ("from … to …");
* passages that only concern legal claim rights, which are listed but never
  applied to a securities balance.

The earliest documented balance before the requested date is rolled
forward with the documented transfers in between (``Decimal``).  Every
record on the requested date is then compared with that derived balance and
with each other.  Disagreement is reported as an unresolved discrepancy:
no source is preferred because it was issued later, by a custodian, or
because it agrees with the arithmetic.

Where a billing payload is supplied, its use of the subject's amount is
described against those records.  The allocation itself is never
recalculated or changed here.  Nothing here persists data or contacts an AI
provider.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .billing_allocation import (
    _AS_OF, _HOLDING_CUE, _ISIN, _NOMINAL_QUANTITY, ScopeDocument, _decimal_grouped, _first_dated, _normal, split_label,
)
from .document_search import DocumentSearchIndex
from .entity_inventory import entity_key
from .holdings_comparison import DatedRecord, HoldingsComparison, HoldingsDocument

MAX_PDF_HITS = 200
MAX_TIMELINE_ENTRIES = 40
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SUBJECT_KINDS = ("subfund", "fund", "holder")
_TRANSFER_CUE = re.compile(r"\btransfer(?:s|red)?\b", re.IGNORECASE)
_DIRECTION = re.compile(r"\bfrom\b(?P<origin>.+?)\bto\b(?P<target>.+)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"\.(?:\s|$)")
_EFFECTIVE = re.compile(r"\beffective(?:\s+date)?(?:\s+(?:as\s+of|from|on))?\s*:?\s*", re.IGNORECASE)
_CLAIM_RIGHTS = re.compile(r"\bclaim[- ]rights?\b|\blegal\s+claims?\b", re.IGNORECASE)
_STATED_BY_SOURCE = "the source itself states the conflict"
_STATED_CONFLICT = re.compile(r"\b(?:conflicts?|conflicting|contradict\w*|discrepanc(?:y|ies)|unresolved)\b", re.IGNORECASE)

# (document_id, extraction_id, reference, filename)
Source = tuple[str, str, str, str]


@dataclass(frozen=True)
class Subject:
    name: str
    identifier: str | None = None
    accounts: frozenset[str] = frozenset()

    @property
    def label(self) -> str:
        return f"{self.name} ({self.identifier})" if self.identifier else self.name

    def named_in(self, text: str) -> bool:
        folded = _normal(text)
        for token in (self.identifier, *self.accounts):
            if token and re.search(rf"(?<![a-z0-9-]){re.escape(token.casefold())}(?![a-z0-9-])", folded):
                return True
        key = entity_key(self.name)
        return bool(key) and re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", folded) is not None

    def is_value(self, value: str) -> bool:
        """Whether a spreadsheet name cell ("Horizon Credit Fund B (SF-B)") is this subject."""
        name, identifier = split_label(value)
        if self.identifier and identifier:
            return identifier.casefold() == self.identifier.casefold()
        return entity_key(name) == entity_key(self.name)


@dataclass(frozen=True)
class Observation:
    date: str
    quantity: Decimal
    isin: str | None
    account: str | None
    kind: str  # "holdings record" (spreadsheet) | "holding statement" (PDF text)
    source: Source


@dataclass(frozen=True)
class Transfer:
    date: str
    quantity: Decimal
    incoming: bool
    counterparty: str
    isin: str | None
    source: Source


@dataclass
class Evidence:
    subject: Subject
    observations: list[Observation] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    # Passages shown but never applied to a balance: claim-rights statements
    # and a source's own statement that it conflicts with another record.
    notes: list[tuple[str, Source]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def found(self) -> bool:
        return bool(self.observations or self.transfers)


@dataclass
class Reconstruction:
    subject: Subject
    on_date: str | None
    isin: str | None
    opening: list[Observation] = field(default_factory=list)
    applied: list[Transfer] = field(default_factory=list)
    derived: Decimal | None = None
    on_date_records: list[Observation] = field(default_factory=list)
    intermediate: list[Observation] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def day(iso: str | None) -> str:
    """"2025-02-28" → "28 Feb 2025"; anything else unchanged."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not match:
        return iso or "an unstated date"
    return f"{int(match.group(3))} {_MONTHS[int(match.group(2)) - 1]} {match.group(1)}"


def amount(value: Decimal) -> str:
    return _decimal_grouped(value)


def cite(source: Source) -> str:
    return f"{source[3]}, {source[2]}"


def _dated_after(pattern: re.Pattern[str], text: str) -> str | None:
    dates = {_first_dated(text[match.end():match.end() + 40]) for match in pattern.finditer(text)} - {None}
    return next(iter(dates)) if len(dates) == 1 else None


def _page_blocks(connection: sqlite3.Connection, extraction_id: str, page_number: int | None) -> list[tuple[str, str]]:
    if page_number is None:
        return []
    return [
        (f"page {page_number} block {row['block_index']}", str(row["text"] or ""))
        for row in connection.execute(
            "SELECT b.block_index, b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id "
            "WHERE p.extraction_id = ? AND p.page_number = ? ORDER BY b.block_index",
            (extraction_id, page_number),
        ).fetchall()
    ]


def _quantity(text: str) -> Decimal | None:
    try:
        values = {Decimal(value.replace(",", "")) for value in _NOMINAL_QUANTITY.findall(text)}
    except InvalidOperation:
        return None
    return next(iter(values)) if len(values) == 1 else None


def gather(
    connection: sqlite3.Connection, documents: Sequence[ScopeDocument], subject: Subject,
) -> Evidence:
    """Collect the subject's dated records, transfers and claim-rights passages in scope."""
    spreadsheets = [document for document in documents if document.spreadsheet]
    reader = HoldingsComparison(connection, [HoldingsDocument(d.document_id, d.filename, d.extraction_id) for d in spreadsheets])
    identifier, accounts = subject.identifier, set(subject.accounts)
    others: dict[str, Subject] = {}
    pending: list[tuple[ScopeDocument, DatedRecord]] = []
    for document in spreadsheets:
        for record in reader.dated_records(HoldingsDocument(document.document_id, document.filename, document.extraction_id)):
            values = [record.entities[kind][0] for kind in _SUBJECT_KINDS if kind in record.entities]
            if any(subject.is_value(value) for value in values):
                labelled = next((split_label(value)[1] for value in values if subject.is_value(value)), None)
                identifier = identifier or labelled
                if record.account:
                    accounts.add(record.account)
                pending.append((document, record))
            else:
                for value in values[:1]:
                    name, other_identifier = split_label(value)
                    others.setdefault(entity_key(name), Subject(name, other_identifier))
    subject = Subject(subject.name, identifier, frozenset(accounts))
    evidence = Evidence(subject)
    unusable = 0
    for document, record in pending:
        if record.quantity is None or record.date is None:
            unusable += 1
            continue
        evidence.observations.append(Observation(
            record.date, record.quantity, record.isin, record.account, "holdings record",
            (document.document_id, document.extraction_id, record.quantity_reference, document.filename),
        ))
    if unusable:
        evidence.limitations.append(
            f"{unusable} spreadsheet record{'s' if unusable != 1 else ''} for {subject.label} had no readable quantity or date and "
            f"{'were' if unusable != 1 else 'was'} not used."
        )

    pdfs = [document for document in documents if not document.spreadsheet]
    if not pdfs:
        return evidence
    # The index matches literal text, and PDF text can break a name across
    # lines ("Meridian Income\nFund C"), so the name's longest word is also
    # searched.  Every hit must still name the subject exactly (below).
    longest = max(re.findall(r"[^\W\d_]{5,}", subject.name), key=len, default=None)
    terms = [term for term in (subject.name, longest, subject.identifier, *sorted(subject.accounts)) if term]
    search = DocumentSearchIndex.search(
        connection, [document.extraction_id for document in pdfs], terms, per_document_limit=40, total_limit=MAX_PDF_HITS,
    )
    evidence.truncated = search.truncated
    names = {document.document_id: document.filename for document in pdfs}
    pages_noted: set[tuple[str, int | None]] = set()
    for hit in search.hits:
        if hit.source_kind != "pdf_text_block":
            continue
        text = " ".join(hit.text.split())
        if not subject.named_in(text):
            continue
        source: Source = (hit.document_id, hit.extraction_id, hit.source_reference, names.get(hit.document_id, ""))
        page = _page_blocks(connection, hit.extraction_id, hit.page_number)
        page_text = " ".join(" ".join(block_text for _reference, block_text in page).split())
        quantity = _quantity(text)
        if _CLAIM_RIGHTS.search(text) and quantity is None:
            # Claim rights travel separately from securities: never a balance movement.
            evidence.notes.append(("concerns legal claim rights, so it is not applied to the securities balance", source))
            continue
        isins = set(_ISIN.findall(text))
        isin = next(iter(isins)) if len(isins) == 1 else None
        direction = _DIRECTION.search(text) if _TRANSFER_CUE.search(text) else None
        if direction is not None:
            origin = direction.group("origin").strip(" ,;")
            target = _SENTENCE_END.split(direction.group("target"), maxsplit=1)[0].strip(" ,;")
            sides = (subject.named_in(origin), subject.named_in(target))
            effective = _dated_after(_EFFECTIVE, text) or _dated_after(_EFFECTIVE, page_text)
            if quantity is None or sides[0] == sides[1]:
                evidence.limitations.append(
                    f"{cite(source)} mentions a transfer for {subject.label}, but not one quantity with {subject.label} on "
                    "exactly one side, so it was not applied."
                )
            elif effective is None:
                evidence.limitations.append(f"{cite(source)} records a transfer without an effective date, so it was not applied.")
            else:
                evidence.transfers.append(Transfer(effective, quantity, sides[1], (target if sides[0] else origin)[:160], isin, source))
                # The notice's own statement about claim rights, if any, is shown with it.
                for reference, block_text in page:
                    if reference != hit.source_reference and _CLAIM_RIGHTS.search(block_text) and (hit.document_id, hit.page_number) not in pages_noted:
                        pages_noted.add((hit.document_id, hit.page_number))
                        evidence.notes.append((
                            "the transfer notice's own statement about legal claim rights",
                            (hit.document_id, hit.extraction_id, reference, source[3]),
                        ))
            continue
        if any(other.named_in(text) for key, other in others.items() if key != entity_key(subject.name)):
            continue
        as_of = _dated_after(_AS_OF, text) or _dated_after(_AS_OF, page_text)
        if _HOLDING_CUE.search(text) and quantity is not None and as_of is not None:
            evidence.observations.append(Observation(as_of, quantity, isin, None, "holding statement", source))
            for reference, block_text in page:
                if reference != hit.source_reference and _STATED_CONFLICT.search(block_text):
                    evidence.notes.append((
                        _STATED_BY_SOURCE,
                        (hit.document_id, hit.extraction_id, reference, source[3]),
                    ))
    unique: dict[tuple[str, str], tuple[str, Source]] = {}
    for note in evidence.notes:
        unique.setdefault((note[1][0], note[1][2]), note)
    evidence.notes = list(unique.values())
    return evidence


def reconstruct(evidence: Evidence, on_date: str | None, isin_hint: str | None = None) -> Reconstruction:
    """Roll the earliest documented balance before ``on_date`` forward with documented transfers."""
    subject = evidence.subject
    isins = {item.isin for item in [*evidence.observations, *evidence.transfers] if item.isin}
    if isin_hint:
        isin: str | None = isin_hint
    elif len(isins) > 1:
        result = Reconstruction(subject, on_date, None)
        result.limitations.append(
            f"The records for {subject.label} cover {len(isins)} securities ({', '.join(sorted(isins))}); name the ISIN to "
            "reconstruct one holding."
        )
        return result
    else:
        isin = next(iter(isins), None)
    observations = [item for item in evidence.observations if item.isin in {isin, None}]
    transfers = [item for item in evidence.transfers if item.isin in {isin, None}]
    on_date = on_date or max((item.date for item in observations), default=None)
    result = Reconstruction(subject, on_date, isin)
    if on_date is None:
        result.limitations.append(f"No dated holding of {subject.label} is recorded in the documents in scope.")
        return result
    accounts = {item.account for item in observations if item.account}
    if len(accounts) > 1:
        result.limitations.append(
            f"{subject.label} has records for {len(accounts)} accounts ({', '.join(sorted(accounts))}); the balances were "
            "listed but not rolled forward as one holding."
        )
    result.on_date_records = [item for item in observations if item.date == on_date]
    earlier = sorted({item.date for item in observations if item.date < on_date})
    if not earlier:
        result.limitations.append(
            f"No holding of {subject.label} is documented before {day(on_date)}, so no opening balance could be rolled forward."
        )
    else:
        opening_date = earlier[0]
        result.opening = [item for item in observations if item.date == opening_date]
        result.intermediate = [item for item in observations if opening_date < item.date < on_date]
        for transfer in sorted(transfers, key=lambda item: item.date):
            if opening_date < transfer.date <= on_date:
                result.applied.append(transfer)
            elif transfer.date == opening_date:
                result.limitations.append(
                    f"The transfer effective {day(transfer.date)} ({cite(transfer.source)}) falls on the opening date; whether "
                    "the opening balance already reflects it is not documented, so it was not applied."
                )
            elif transfer.date > on_date:
                result.limitations.append(f"The transfer effective {day(transfer.date)} ({cite(transfer.source)}) is after {day(on_date)} and was not applied.")
            else:
                result.limitations.append(f"The transfer effective {day(transfer.date)} ({cite(transfer.source)}) is before the opening balance and was not applied.")
        opening_values = {item.quantity for item in result.opening}
        if len(opening_values) > 1:
            result.discrepancies.append(
                f"The opening records for {day(opening_date)} disagree ("
                + "; ".join(f"{amount(item.quantity)} in {cite(item.source)}" for item in result.opening)
                + "), so no balance was derived."
            )
        elif len(accounts) <= 1:
            result.derived = next(iter(opening_values)) + sum(
                (transfer.quantity if transfer.incoming else -transfer.quantity for transfer in result.applied), Decimal("0"),
            )
    recorded = {item.quantity for item in result.on_date_records}
    if len(recorded) > 1:
        result.discrepancies.append(
            f"The records for {day(on_date)} disagree: "
            + "; ".join(f"{amount(item.quantity)} nominal in {cite(item.source)}" for item in result.on_date_records)
            + f" (a difference of {amount(max(recorded) - min(recorded))})."
        )
    if result.derived is not None:
        for item in result.on_date_records:
            if item.quantity != result.derived:
                result.discrepancies.append(
                    f"{cite(item.source)} records {amount(item.quantity)} nominal, {amount(abs(item.quantity - result.derived))} "
                    f"{'more' if item.quantity > result.derived else 'less'} than the derived {amount(result.derived)}."
                )
    if not result.on_date_records:
        result.limitations.append(f"No record in scope states {subject.label}'s holding on {day(on_date)}.")
    return result


def billing_effect(
    subject: Subject, result: Reconstruction, payload: Mapping[str, object] | None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Describe how the instruction uses the subject's amount; (lines, cited (documentId, reference))."""
    if payload is None:
        return ["No approved billing instruction in scope states a fee pool and eligible amounts, so no billing effect is stated."], []
    instruction = payload.get("instruction") if isinstance(payload.get("instruction"), Mapping) else None
    filename = str(instruction.get("filename")) if instruction else "the billing instruction"  # type: ignore[union-attr]
    if payload.get("calculationStatus") != "complete":
        return [f"No allocation could be calculated from {filename} (inputs are missing or conflicting), so no billing effect is stated."], []
    participants = [item for item in payload.get("participants", []) if isinstance(item, Mapping)]  # type: ignore[union-attr]
    match = next((
        item for item in participants
        if (subject.identifier and item.get("identifier") and str(item["identifier"]).casefold() == subject.identifier.casefold())
        or entity_key(str(item.get("name") or "")) == entity_key(subject.name)
    ), None)
    cited: list[tuple[str, str]] = []
    if match is not None and isinstance(match.get("source"), Mapping):
        cited.append((str(match["source"]["documentId"]), str(match["source"]["reference"])))  # type: ignore[index]
    where = f" ({filename}, {match['source']['reference']})" if match is not None and isinstance(match.get("source"), Mapping) else ""  # type: ignore[index]
    if match is None or match.get("status") == "undetermined":
        return [f"The approved billing instruction ({filename}) does not name {subject.label}, so its eligibility for billing is not established and no billing effect is stated."], cited
    if match.get("status") == "excluded":
        reason = f" because {match['reason']}" if match.get("reason") else ""
        return [
            f"The approved billing instruction excludes {subject.label}{reason}{where}; its allocation is 0.00 under that instruction whatever its holding."
        ], cited
    currency = str(payload.get("currency") or "")
    basis = Decimal(str(match["eligibleAmount"]))
    period = payload.get("period") if isinstance(payload.get("period"), Mapping) else None
    lines = [
        f"The approved billing instruction uses {amount(basis)} nominal as {subject.label}'s eligible amount{where}"
        + (f" for {period['text']}" if period else "")  # type: ignore[index]
        + f", giving {currency} {Decimal(str(match['allocatedAmount'])):,.2f} ({match['sharePercent']}% of the "
        f"{currency} {Decimal(str(payload['feePool']['amount'])):,.2f} fee pool).",  # type: ignore[index]
    ]
    comparison = payload.get("crossCheck") if isinstance(payload.get("crossCheck"), Mapping) else {}
    billing_date = str(comparison.get("comparisonDate") or "") or None  # type: ignore[union-attr]
    if billing_date is None:
        lines.append("The instruction states no as-of date or billing-period end, so its amount was not compared with the records above.")
        return lines, cited
    if billing_date != result.on_date:
        lines.append(
            f"The instruction's comparison date is {day(billing_date)}, not {day(result.on_date)}, so the records above were not "
            "compared with its amount."
        )
        return lines, cited
    agreeing = [cite(item.source) for item in result.on_date_records if item.quantity == basis]
    differing = [f"{cite(item.source)} ({amount(item.quantity)})" for item in result.on_date_records if item.quantity != basis]
    if result.derived is not None:
        (agreeing if result.derived == basis else differing).append(
            "the derived balance" if result.derived == basis else f"the derived balance ({amount(result.derived)})"
        )
    if agreeing:
        lines.append(f"That amount agrees with {'; '.join(agreeing)}.")
    if differing:
        lines.append(
            f"It differs from {'; '.join(differing)}. Because that conflict is unresolved, the allocation is provisional and "
            "should not be invoiced until it is resolved."
        )
        lines.append(
            "ClaimFlow used the instruction amount unchanged. It did not substitute another figure or recalculate. If a different "
            f"eligible amount were confirmed for {subject.label}, the total eligible base and therefore every eligible "
            "participant's share would change, because the rule divides each amount by the total."
        )
    elif agreeing:
        lines.append(f"Nothing in scope dated {day(billing_date)} differs from it, so this holding does not change the approved allocation.")
    else:
        lines.append(f"No record in scope states {subject.label}'s holding on {day(billing_date)}, so the amount could not be checked.")
    return lines, cited


def answer_text(
    evidence: Evidence, result: Reconstruction, billing_lines: Sequence[str] | None,
) -> tuple[str, list[Source]]:
    """Plain-text answer in separate sections, and every source it names."""
    subject, on_date = result.subject, result.on_date
    security = f" in {result.isin}" if result.isin else ""
    sources: list[Source] = []

    def named(source: Source) -> str:
        sources.append(source)
        return cite(source)

    def record_line(item: Observation) -> str:
        account = f", account {item.account}" if item.account else ""
        return f"- {day(item.date)} · {item.kind}: {amount(item.quantity)} nominal{account} ({named(item.source)})"

    facts = [record_line(item) for item in result.opening]
    events: list[tuple[str, str]] = [(item.date, record_line(item)) for item in result.intermediate]
    for transfer in result.applied:
        direction = f"into {subject.label} from" if transfer.incoming else f"out of {subject.label} to"
        events.append((transfer.date, (
            f"- {day(transfer.date)} (effective) · securities transfer of {amount(transfer.quantity)} nominal {direction} "
            f"{transfer.counterparty} ({named(transfer.source)})"
        )))
    facts += [line for _date, line in sorted(events, key=lambda event: event[0])]
    facts += [record_line(item) for item in result.on_date_records]
    if len(facts) > MAX_TIMELINE_ENTRIES:
        hidden = len(facts) - MAX_TIMELINE_ENTRIES
        facts = [*facts[:MAX_TIMELINE_ENTRIES], f"- {hidden} further dated entries are not listed."]

    arithmetic: list[str] = []
    if result.derived is not None and result.opening:
        terms = [f"{amount(result.opening[0].quantity)} (opening, {day(result.opening[0].date)})"]
        terms += [
            f"{'+' if transfer.incoming else '−'} {amount(transfer.quantity)} (transfer {'in' if transfer.incoming else 'out'}, "
            f"{day(transfer.date)})"
            for transfer in result.applied
        ]
        arithmetic.append(f"- {' '.join(terms)} = {amount(result.derived)} nominal as of {day(on_date)}.")
        arithmetic.append(
            f"- This assumes no other movement: no other transfer for {subject.label} between {day(result.opening[0].date)} "
            f"and {day(on_date)} is documented in the documents in scope."
        )
    else:
        arithmetic.append("- No balance was derived (see the limits below).")

    discrepancies = [f"- {text}" for text in result.discrepancies]
    stated = [(text, source) for text, source in evidence.notes if text == _STATED_BY_SOURCE]
    discrepancies += [f"- {named(source)}: {text}." for text, source in stated]
    if result.discrepancies:
        discrepancies.append(
            "- Unresolved. No source was preferred because it was issued later, issued by a custodian, or agrees with the arithmetic."
        )
    elif not stated:
        discrepancies.append(
            f"- None found among the records in scope dated {day(on_date)}." if result.on_date_records
            else f"- No record in scope is dated {day(on_date)}, so nothing could be compared."
        )

    if result.discrepancies:
        values = sorted({item.quantity for item in result.on_date_records})
        conclusion = (
            f"The documents in scope do not agree on {subject.label}'s holding{security} as of {day(on_date)}"
            + (f": they record {' and '.join(amount(value) for value in values)} nominal." if len(values) > 1 else ".")
            + (
                f" Rolling the opening balance forward with the documented transfers gives {amount(result.derived)} nominal."
                if result.derived is not None else ""
            )
            + " The discrepancy is unresolved; ClaimFlow does not choose between the sources."
        )
    elif result.derived is not None and result.on_date_records:
        conclusion = (
            f"The records in scope agree that {subject.label} held {amount(result.on_date_records[0].quantity)} nominal{security} "
            f"as of {day(on_date)}, which matches the balance rolled forward from the opening record and documented transfers."
        )
    elif result.on_date_records:
        conclusion = (
            f"The records in scope state {subject.label}'s holding{security} as of {day(on_date)}, but no earlier balance could be "
            "rolled forward to reconstruct it."
        )
    elif result.derived is not None:
        conclusion = (
            f"No record in scope states {subject.label}'s holding{security} on {day(on_date)}. Rolling the "
            f"{day(result.opening[0].date)} balance forward with the transfers documented in scope gives "
            f"{amount(result.derived)} nominal; that is arithmetic, not a documented balance."
        )
    else:
        conclusion = f"The documents in scope do not establish {subject.label}'s holding{security} as of {day(on_date)}."

    sections = [conclusion, "Documented facts:\n" + ("\n".join(facts) or "- None in scope."), "Derived arithmetic:\n" + "\n".join(arithmetic),
                "Discrepancies:\n" + "\n".join(discrepancies)]
    if billing_lines is not None:
        sections.append("Billing effect (provisional):\n" + "\n".join(f"- {line}" for line in billing_lines))
    claims = [(text, source) for text, source in evidence.notes if text != _STATED_BY_SOURCE]
    if claims:
        sections.append(
            "Not applied to the securities balance:\n"
            + "\n".join(f"- {named(source)}: {text}." for text, source in claims)
            + "\n- A securities movement is not treated as a transfer of legal claim rights, and a claim-rights document does not change a holding."
        )
    limits = [*evidence.limitations, *result.limitations]
    if evidence.truncated:
        limits.append("More PDF passages mention this subject than were read; the reconstruction may be incomplete.")
    limits.append(
        "Only documents in the current scope were read. A transfer is applied only when one passage states its quantity, its "
        "effective date and this subject on one side (“from … to …”)."
    )
    limits.append("This reports dated records and arithmetic. It does not establish legal ownership or claim rights.")
    sections.append("Limits of this answer:\n" + "\n".join(f"- {line}" for line in limits))
    return "\n\n".join(sections), list(dict.fromkeys(sources))
