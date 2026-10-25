"""Read an approved pro-rata billing instruction from PDF text, whatever its layout.

Recognises an instruction only when the page states an approval, a fee pool
with a currency, a pro-rata rule, a billing period and at least one
participant with a positive eligible nominal basis.  Each fact keeps the PDF
block it came from so an answer can cite it.  Nothing is inferred: a
participant is only what the instruction names (for example "Participant A"),
and an exclusion keeps the instruction's own reason.  Mapping a participant
label to a fund is a separate, contextual step (``participant_marker``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .pdf_fields import find_dates, labelled_value, parse_amount

_NUMBER = r"\d{1,3}(?:[, ' ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_FEE_POOL = re.compile(rf"\bfee\s+pool\b\s*[:\-–]?\s*(?:of\s+)?(?P<currency>[A-Z]{{3}})\s*(?P<amount>{_NUMBER})|(?P<currency2>[A-Z]{{3}})\s*(?P<amount2>{_NUMBER})\s+(?:approved\s+)?fee\s+pool\b", re.IGNORECASE)
_PRO_RATA = re.compile(r"\bpro[\s-]?rata\b|eligible\s+nominal\s+amount\s*/\s*total\s+eligible\s+nominal|divided\s+by\s+the\s+total\s+(?:approved\s+)?eligible", re.IGNORECASE)
_PERIOD = re.compile(r"\bbilling\s+period\b\s*[:\-–]?\s*(?P<rest>.+)", re.IGNORECASE)
_APPROVAL = re.compile(r"\bapproved\b", re.IGNORECASE)
_PARTICIPANT = re.compile(
    rf"(?P<label>[A-Z][\w&'’.\- ]{{0,90}}?)\s*(?:\((?P<reference>[^)]{{1,30}})\))?\s*:\s*(?:[A-Z]{{3}}\s*)?(?P<amount>{_NUMBER})(?P<tail>\s*(?:eligible\s+)?nominal)?"
)
_EXCLUSION = re.compile(
    r"(?P<label>[A-Z][\w&'’.\- ]{1,100}?)\s*(?:\((?P<reference>[^)]{1,30})\))?\s+is\s+excluded\b[^.]*?\bbecause\s+(?:it\s+is\s+)?(?P<reason>[^.]+)",
)
_LEGACY_EXCLUSION = re.compile(r"Excluded\s+participant\s+(?P<label>[^:(]+?)\s*(?:\((?P<reference>[^)]+)\))?\s*:\s*(?P<reason>[^.]+)", re.IGNORECASE)
_NOT_PARTICIPANT_LABEL = re.compile(r"\b(?:fee\s+pool|billing\s+period|issue\s+date|date|approved\s+by|total|period|effective)\b", re.IGNORECASE)
_APPROVED_BY = re.compile(r"\bapproved\s+by\s*:\s*(?P<who>[^\n]+)", re.IGNORECASE)
_STATEMENT = re.compile(r"\bnot\s+an\s+invoice\b|\binvoice\s+recipient\b|\bdoes\s+not\s+identify\b", re.IGNORECASE)
_MARKER = re.compile(r"(?:^|\s)(?P<marker>[A-Z]|\d{1,2}|I{1,3}|IV|V|VI{0,3})\s*(?:\([^)]*\))?\s*$")
_GENERIC_LABEL = re.compile(r"^(?:eligible\s+)?(?:participant|fund|sub-?fund|class|series|compartment|portfolio)\s+(?:\d+\s+)?", re.IGNORECASE)


@dataclass(frozen=True)
class Participant:
    label: str
    reference: str | None
    basis: Decimal
    block: int


@dataclass(frozen=True)
class Exclusion:
    label: str
    reference: str | None
    reason: str
    block: int


@dataclass(frozen=True)
class BillingInstruction:
    currency: str
    fee_pool: Decimal
    period_start: str
    period_end: str
    issue_date: str | None
    approved_by: str | None
    participants: tuple[Participant, ...]
    exclusions: tuple[Exclusion, ...]
    blocks: dict[str, int]            # fact -> block index (fee_pool, rule, period, issue_date, approval)
    statements: tuple[tuple[int, str], ...]  # invoice-related statements (block, text)

    @property
    def total_basis(self) -> Decimal:
        return sum((participant.basis for participant in self.participants), Decimal("0"))


def participant_marker(label: str) -> str | None:
    """The series marker that ends a label ("Participant A" → "A", "Fund 2" → "2")."""
    match = _MARKER.search(label.strip())
    return match.group("marker").upper() if match else None


def is_generic_label(label: str) -> bool:
    """Whether a label names a fund only by role and marker ("Participant A", "Fund B")."""
    return bool(_GENERIC_LABEL.match(label.strip())) and participant_marker(label) is not None


def _compact(text: str) -> str:
    return " ".join(text.split())


def parse_billing_instruction(blocks: Sequence[tuple[int, str]]) -> BillingInstruction | None:
    """The single approved pro-rata instruction on these blocks, or None."""
    found: dict[str, int] = {}
    currency, fee_pool, period, approved_by = None, None, None, None
    participants: list[Participant] = []
    exclusions: list[Exclusion] = []
    statements: list[tuple[int, str]] = []
    for index, raw in blocks:
        text = _compact(raw)
        if _APPROVAL.search(text) and "approval" not in found:
            found["approval"] = index
        who = _APPROVED_BY.search(raw)
        if who and approved_by is None:
            approved_by = _compact(who.group("who"))
        pool = _FEE_POOL.search(text)
        if pool and fee_pool is None:
            amount = parse_amount(pool.group("amount") or pool.group("amount2"))
            if amount is not None and amount > 0:
                currency, fee_pool = (pool.group("currency") or pool.group("currency2")).upper(), amount
                found["fee_pool"] = index
        if _PRO_RATA.search(text) and "rule" not in found:
            found["rule"] = index
        period_match = _PERIOD.search(text)
        if period_match and period is None:
            dates = [value for value, _start in find_dates(period_match.group("rest"))]
            if len(dates) >= 2 and all(dates[:2]):
                period = (str(dates[0]), str(dates[1]))
                found["period"] = index
        for match in _EXCLUSION.finditer(text):
            exclusions.append(Exclusion(_compact(match.group("label")), match.group("reference"), _compact(match.group("reason")), index))
        for match in _LEGACY_EXCLUSION.finditer(text):
            exclusions.append(Exclusion(_compact(match.group("label")), match.group("reference"), _compact(match.group("reason")), index))
        for match in _PARTICIPANT.finditer(text):
            label = re.sub(r"^(?:.*\b)?eligible\s+participant\s+\d+\s+", "", _compact(match.group("label")), flags=re.IGNORECASE).strip()
            if _NOT_PARTICIPANT_LABEL.search(label):
                continue
            participant_context = bool(match.group("tail")) or re.search(r"\b(?:eligible|participant)\b", match.group(0) + " " + text[:match.start()][-40:], re.IGNORECASE)
            if not participant_context:
                continue
            amount = parse_amount(match.group("amount"))
            if amount is None or amount <= 0:
                return None  # a declared participant without a usable basis: refuse rather than drop it
            participants.append(Participant(label, match.group("reference"), amount, index))
        if _STATEMENT.search(text):
            statements.append((index, text))
    if fee_pool is None or currency is None or period is None or "rule" not in found or "approval" not in found or not participants:
        return None
    issue_date, issue_block = labelled_value(list(blocks), "issue_date")
    if isinstance(issue_date, str) and issue_block is not None:
        found["issue_date"] = issue_block
    excluded_labels = {exclusion.label.casefold() for exclusion in exclusions}
    participants = [participant for participant in participants if participant.label.casefold() not in excluded_labels]
    return BillingInstruction(
        currency, fee_pool, period[0], period[1], issue_date if isinstance(issue_date, str) else None, approved_by,
        tuple(participants), tuple({(item.label, item.block): item for item in exclusions}.values()), found, tuple(statements),
    )


def allocate(fee_pool: Decimal, bases: Sequence[tuple[str, Decimal]]) -> list[tuple[str, Decimal]]:
    """Pro-rata allocation in cents; the largest remainders take the residual cents."""
    total = sum((basis for _key, basis in bases), Decimal("0"))
    if total <= 0:
        return []
    cents = int((fee_pool * 100).to_integral_value())
    raw = [(key, Decimal(cents) * basis / total) for key, basis in bases]
    floors = {key: int(value) for key, value in raw}
    residual = cents - sum(floors.values())
    for key, _value in sorted(raw, key=lambda item: (-(item[1] - int(item[1])), item[0]))[:residual]:
        floors[key] += 1
    return [(key, Decimal(floors[key]) / 100) for key, _basis in bases]
