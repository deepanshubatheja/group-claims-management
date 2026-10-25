"""Cited facts from the structured records of selected documents.

Matter Review uses this to answer fund-identity and historical-reconstruction
questions from extracted holdings, securities transfers, claim-right
assignments, alias statements and account migrations instead of
keyword-ranked raw text.

Every fact cites one stored spreadsheet cell or PDF text block so an answer
can be checked against the source.  Records awaiting review are included and
labelled as such; rejected records are not.  Accounts form one fund only
through a documented account migration or a confirmed review decision, and
names are treated as one fund only where a document states so for that
account; matching names or investors never link funds.  Calculated balances
are labelled inferred and never replace a documented one; conflicting
records are reported side by side.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Sequence

from .identity import normalize_account_reference, normalize_name
from .pdf_fields import find_amounts, find_dates

# (document id, extraction id, single cell or block reference, filename)
Citation = tuple[str, str, str, str]
_RANGE = re.compile(r"^(?P<sheet>.+)!(?P<column>[A-Z]+)(?P<row>[1-9][0-9]*)(?::[A-Z]+[1-9][0-9]*)?$")
_BLOCK = re.compile(r"^page\s+(\d+)\s+block\s+(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Line:
    """Text with ``{0}``, ``{1}``… placeholders for its citations."""
    template: str
    citations: tuple[Citation, ...] = ()


@dataclass
class _Holding:
    account: str
    isin: str
    quantity: Decimal
    day: str
    fund: str | None
    investor: str | None
    confirmed: bool
    quantity_citation: Citation
    name_citation: Citation
    investor_citation: Citation


@dataclass
class _Migration:
    old_account: str
    new_account: str
    effective: str | None
    investor: str | None
    fund: str | None
    citation: Citation
    date_citation: Citation


@dataclass
class _Transfer:
    from_account: str
    to_account: str
    from_name: str | None
    to_name: str | None
    isin: str
    quantity: Decimal
    effective: str
    issued: str | None
    confirmed: bool
    citation: Citation
    date_citation: Citation


@dataclass
class _Assignment:
    """A claim-right assignment record; ``template``/``citations`` render it as a ``Line``."""
    template: str
    citations: tuple[Citation, ...]
    claim_reference: str
    assignor: str
    assignee: str
    percentage: str | None
    isin: str
    effective: str
    issued: str | None
    confirmed: bool
    main_citation: Citation
    date_citation: Citation
    issue_citation: Citation | None
    scope_citations: tuple[Citation, ...]


@dataclass
class _Alias:
    alias: str
    canonical: str
    account: str | None
    citation: Citation


@dataclass
class _Account:
    display: str
    names: dict[str, tuple[str, Citation]] = field(default_factory=dict)  # normalized -> (name, where recorded)
    investors: dict[str, str] = field(default_factory=dict)
    aliases: list[_Alias] = field(default_factory=list)
    first_day: str = "9999-12-31"


@dataclass
class _Fund:
    """Accounts that documented migrations (or confirmed review) tie to one fund."""
    keys: list[str]
    migrations: list[_Migration]
    reviewed_link: bool = False


IDENTITY_INTRO = (
    "Funds are grouped by custody account. Different names are treated as the same fund only where a document "
    "states so for that account, and different accounts only through a documented account migration or a "
    "confirmed review decision; similar names or a shared investor alone are not merged."
)
RECONSTRUCTION_INTRO = (
    "Reconstruction by account. “Documented” balances are stated in a holdings record; “inferred” balances are "
    "calculated from an earlier documented balance and a cited transfer or account migration. Conflicting records are not resolved."
)
# Fixed explanatory sentences; they carry no facts into a follow-up's context.
ANSWER_BOILERPLATE = (IDENTITY_INTRO, RECONSTRUCTION_INTRO)

# Words that describe many funds and so cannot pick one out of a question.
_GENERIC_WORDS = frozenset({
    "fund", "funds", "subfund", "sub", "class", "series", "share", "capital", "partners", "partnership", "holdings",
    "investments", "investment", "investors", "investor", "management", "asset", "assets", "family", "office", "global",
    "credit", "income", "growth", "reserve", "bond", "bonds", "equity", "opportunities", "strategic", "special",
    "situations", "portfolio", "trust", "group", "company", "the", "and", "for", "ltd", "llc", "llp", "plc", "inc",
    "corp", "limited", "sicav", "spv", "account", "custody",
})


_ISSUE_LABEL = re.compile(r"\b(?:issue\s+date|date\s+of\s+issue|issued|document\s+date)\b", re.IGNORECASE)
# A document's own statement that an assignment moves claim rights only.
_CLAIM_ONLY_SCOPE = re.compile(
    r"\bclaim\s+rights\s+only\b|\b(?:does|do|did)\s+not\s+(?:transfer|move|change|amend|modify|vary)\b[^.]*\b(?:securities|custody|balances?|holdings?)\b",
    re.IGNORECASE,
)
_DENIAL = re.compile(r"\b(?:does|do|did|is|are)\s+not\s+(?:relate|refer|concern|apply|affect|cover)", re.IGNORECASE)


def _distinctive_words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", value.casefold()) if word not in _GENERIC_WORDS}


def question_terms(question: str) -> frozenset[str]:
    """Proper-noun words and identifiers in a question that could name one fund or account.

    Only capitalised words and letter-digit identifiers count, so ordinary
    request words ("reconstruct", "effective") never filter the funds.
    """
    tokens = re.findall(r"\b(?:[A-Z][A-Za-z0-9\-]{2,}|(?=[A-Za-z0-9\-]*\d)(?=[A-Za-z0-9\-]*[A-Za-z])[A-Za-z0-9\-]{3,})\b", question)
    return frozenset(word for token in tokens for word in _distinctive_words(token))


def datetime_from_display(value: str) -> str | None:
    try:
        from datetime import datetime as _datetime
        return _datetime.strptime(value, "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def _e(value: object) -> str:
    """A document value safe to embed in a ``Line`` template."""
    return str(value).replace("{", "{{").replace("}", "}}")


def _grouped(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    whole, _, fraction = text.partition(".")
    return f"{int(whole):,}" + (f".{fraction}" if fraction else "")


def _day(value: str | None) -> str:
    if not value:
        return "date not stated"
    parsed = date.fromisoformat(value)
    return f"{parsed.day} {parsed.strftime('%b')} {parsed.year}"


class StructuredEvidence:
    def __init__(self, connection: sqlite3.Connection, documents: Sequence[tuple[str, str, str]]) -> None:
        self._connection = connection
        self._documents = {document_id: (extraction_id, filename) for document_id, extraction_id, filename in documents}
        self.holdings: list[_Holding] = []
        self.transfers: list[_Transfer] = []
        self.aliases: list[_Alias] = []
        self.assignments: list[_Assignment] = []
        self.migrations: list[_Migration] = []
        # Loaded up front: views are rendered after the connection closes.
        self._vehicle_of: dict[str, str] = {}
        self._denial_blocks: list[tuple[Citation, str]] = []
        self._conflict_status: dict[tuple[str, str, str], str] = {}
        self._load()
        # Documents with any record, before a question narrows the scope.
        self._record_documents = self.relevant_document_ids()
        self._narrowed = False

    # Loading ------------------------------------------------------------

    def _load(self) -> None:
        if not self._documents:
            return
        placeholders = ",".join("?" for _ in self._documents)
        # Superseded readings were replaced when the document was read again.
        rows = self._connection.execute(
            "SELECT c.* FROM reconciliation_candidates c WHERE c.lifecycle_status <> 'rejected' "
            f"AND c.source_document_id IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM reconciliation_candidate_supersessions s WHERE s.candidate_id = c.id AND c.lifecycle_status = 'candidate') "
            "ORDER BY CASE c.lifecycle_status WHEN 'confirmed' THEN 0 ELSE 1 END, c.created_at, c.rowid",
            tuple(self._documents),
        ).fetchall()
        for row in rows:
            document_id = str(row["source_document_id"])
            extraction_id, filename = self._documents[document_id]
            if str(row["extraction_id"]) != extraction_id:
                continue  # staged from an older extraction of the document
            payload = json.loads(str(row["payload_json"]))
            references = [part.strip() for part in str(row["source_reference"]).split(";") if part.strip()]
            confirmed = row["lifecycle_status"] == "confirmed"
            try:
                if row["candidate_type"] == "holding_snapshot":
                    self._add_holding(document_id, extraction_id, filename, references, payload, confirmed)
                elif row["candidate_type"] == "securities_transfer":
                    self._add_transfer(document_id, extraction_id, filename, references, payload, confirmed, row["document_issue_date"] or None)
                elif row["candidate_type"] == "claim_assignment":
                    self._add_assignment(document_id, extraction_id, filename, references, payload, confirmed, row["document_issue_date"] or None)
            except (InvalidOperation, KeyError, TypeError, ValueError):
                continue
        for row in self._connection.execute(
            "SELECT * FROM identity_alias_evidence WHERE target_type = 'investment_vehicle' "
            f"AND source_document_id IN ({placeholders}) ORDER BY created_at, rowid",
            tuple(self._documents),
        ).fetchall():
            document_id = str(row["source_document_id"])
            extraction_id, filename = self._documents[document_id]
            if str(row["extraction_id"]) != extraction_id:
                continue
            reference = str(row["source_reference"]).split(";")[0].strip()
            self.aliases.append(_Alias(str(row["alias_text"]), str(row["canonical_text"]), row["account_reference"], (document_id, extraction_id, reference, filename)))
        for row in self._connection.execute(
            f"SELECT * FROM account_migration_evidence WHERE source_document_id IN ({placeholders}) ORDER BY created_at, rowid",
            tuple(self._documents),
        ).fetchall():
            document_id = str(row["source_document_id"])
            extraction_id, filename = self._documents[document_id]
            if str(row["extraction_id"]) != extraction_id:
                continue
            references = [part.strip() for part in str(row["source_reference"]).split(";") if part.strip()]
            old, new, effective = str(row["old_account_reference"]), str(row["new_account_reference"]), row["effective_date"]
            main = self._locate(extraction_id, references, lambda text: old in text and new in text) or references[-1]
            dated = self._locate(extraction_id, references, lambda text: bool(effective) and any(value == effective for value, _start in find_dates(text)))
            citation = (document_id, extraction_id, main, filename)
            self.migrations.append(_Migration(old, new, effective, row["investor_text"], row["fund_text"], citation, (document_id, extraction_id, dated, filename) if dated else citation))
        self._deduplicate()
        references = {normalize_account_reference(item.account) for item in self.holdings} | {
            normalize_account_reference(account) for item in self.transfers for account in (item.from_account, item.to_account)
        } | {normalize_account_reference(account) for item in self.migrations for account in (item.old_account, item.new_account)}
        for key in references:
            row = self._connection.execute(
                "SELECT a.investment_vehicle_id FROM identity_aliases x JOIN accounts a ON a.id = x.target_id "
                "WHERE x.target_type = 'account' AND x.normalized_alias = ? AND a.investment_vehicle_id IS NOT NULL LIMIT 1",
                (key,),
            ).fetchone()
            if row is not None:
                self._vehicle_of[key] = str(row["investment_vehicle_id"])
        for row in self._connection.execute(
            "SELECT account_reference, security_isin, position_date, status FROM reconciliation_holding_conflicts"
        ):
            self._conflict_status[(normalize_account_reference(str(row["account_reference"])), str(row["security_isin"]), str(row["position_date"]))] = str(row["status"])
        for document_id, (extraction_id, filename) in self._documents.items():
            for row in self._connection.execute(
                "SELECT p.page_number, b.block_index, b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
                (extraction_id,),
            ):
                text = " ".join(str(row["text"] or "").split())
                if _DENIAL.search(text):
                    self._denial_blocks.append(((document_id, extraction_id, f"page {int(row['page_number'])} block {int(row['block_index'])}", filename), text))

    def _deduplicate(self) -> None:
        """Count one fact once, however many records repeat it.

        A re-read or a duplicate notice can restate the same holding or
        transfer; adding it twice would corrupt an inferred balance.  The
        first (confirmed, then earliest) record keeps the citation.
        """
        seen_holdings: set[tuple[str, str, str, Decimal]] = set()
        holdings: list[_Holding] = []
        for holding in self.holdings:
            key = (normalize_account_reference(holding.account), holding.isin, holding.day, holding.quantity)
            if key not in seen_holdings:
                seen_holdings.add(key)
                holdings.append(holding)
        seen_transfers: set[tuple[str, str, str, Decimal, str]] = set()
        transfers: list[_Transfer] = []
        for transfer in self.transfers:
            key = (normalize_account_reference(transfer.from_account), normalize_account_reference(transfer.to_account), transfer.isin, transfer.quantity, transfer.effective)
            if key not in seen_transfers:
                seen_transfers.add(key)
                transfers.append(transfer)
        seen_aliases: set[tuple[str, str, str]] = set()
        aliases: list[_Alias] = []
        for alias in self.aliases:
            key = (normalize_name(alias.alias), normalize_name(alias.canonical), normalize_account_reference(alias.account or ""))
            if key not in seen_aliases:
                seen_aliases.add(key)
                aliases.append(alias)
        seen_migrations: set[tuple[str, str, str]] = set()
        migrations: list[_Migration] = []
        for migration in self.migrations:
            key = (normalize_account_reference(migration.old_account), normalize_account_reference(migration.new_account), migration.effective or "")
            if key not in seen_migrations:
                seen_migrations.add(key)
                migrations.append(migration)
        self.holdings, self.transfers, self.aliases, self.migrations = holdings, transfers, aliases, migrations

    # Scope ----------------------------------------------------------------

    def scope(self, *, start: str | None = None, end: str | None = None, terms: frozenset[str] = frozenset(), include_namesakes: bool = False) -> "StructuredEvidence":
        """Keep only the funds and dates a question asks about.

        ``terms`` are distinctive words or account references from the
        question; accounts whose names, investor or reference contain one are
        kept (all accounts when none match).  With a date window, holdings in
        the window are kept plus each account's latest earlier holding as its
        opening balance, and transfers after that opening up to the window
        end.  Accounts with nothing in that range are dropped.
        ``include_namesakes`` also keeps funds that share a name with a kept
        fund, so an identity answer can explain why they are separate.
        """
        accounts = self._accounts()
        funds = self._funds(accounts)
        keep = set(accounts)
        if terms:
            matched = {key for key, entry in accounts.items() if self._account_words(key, entry) & terms}
            if matched:
                # An account change keeps the fund: asking about one of its
                # accounts includes the others.
                keep = {key for fund in funds if set(fund.keys) & matched for key in fund.keys}
                if include_namesakes:
                    names = {name for key in keep for name in accounts[key].names}
                    keep |= {key for fund in funds if any(names & set(accounts[member].names) for member in fund.keys) for key in fund.keys}
        holdings = [item for item in self.holdings if normalize_account_reference(item.account) in keep]
        transfers = [item for item in self.transfers if {normalize_account_reference(item.from_account), normalize_account_reference(item.to_account)} & keep]
        if start or end:
            low, high = start or "0000-01-01", end or "9999-12-31"
            opening: dict[tuple[str, str], str] = {}
            for item in holdings:
                key = (normalize_account_reference(item.account), item.isin)
                if item.day <= low and item.day > opening.get(key, ""):
                    opening[key] = item.day
            holdings = [
                item for item in holdings
                if low <= item.day <= high or opening.get((normalize_account_reference(item.account), item.isin)) == item.day
            ]
            def after_opening(item: _Transfer) -> bool:
                # Transfers after the earliest opening balance of either side
                # are needed to carry that balance into the window.
                sides = [(normalize_account_reference(account), item.isin) for account in (item.from_account, item.to_account)]
                floor = min(opening.get(side) or low for side in sides)
                return (item.effective > floor or item.effective == low) and item.effective <= high
            transfers = [item for item in transfers if after_opening(item)]
            migrations_in_range = [item for item in self.migrations if not item.effective or item.effective <= high]
            active = {normalize_account_reference(item.account) for item in holdings} | {
                normalize_account_reference(account) for item in transfers for account in (item.from_account, item.to_account)
            } | {normalize_account_reference(account) for item in migrations_in_range for account in (item.old_account, item.new_account)}
            keep &= active
            holdings = [item for item in holdings if normalize_account_reference(item.account) in keep]
        scoped = StructuredEvidence.__new__(StructuredEvidence)
        scoped._connection, scoped._documents = self._connection, self._documents
        scoped._vehicle_of, scoped._denial_blocks, scoped._conflict_status = self._vehicle_of, self._denial_blocks, self._conflict_status
        scoped.holdings, scoped.transfers = holdings, transfers
        scoped.migrations = [
            item for item in self.migrations
            if {normalize_account_reference(item.old_account), normalize_account_reference(item.new_account)} <= keep
            and (not end or not item.effective or item.effective <= end)
        ]
        scoped.aliases = [item for item in self.aliases if item.account is None or normalize_account_reference(item.account) in keep]
        scoped.assignments = list(self.assignments) if (not terms and not (start or end)) else [
            line for line in self.assignments if self._assignment_in_scope(line, keep, accounts, start, end)
        ]
        scoped._record_documents = self._record_documents
        scoped._narrowed = scoped.relevant_document_ids() != self._record_documents
        return scoped

    @staticmethod
    def _account_words(key: str, entry: "_Account") -> set[str]:
        words = {key.casefold(), entry.display.casefold()}
        for value, _citation in entry.names.values():
            words |= _distinctive_words(value)
        for investor in entry.investors.values():
            words |= _distinctive_words(investor)
        return words

    @staticmethod
    def _assignment_in_scope(line: Line, keep: set[str], accounts: Mapping[str, "_Account"], start: str | None, end: str | None) -> bool:
        effective = re.search(r"effective (\d{1,2} \w{3} \d{4})", line.template)
        if effective and (start or end):
            day = datetime_from_display(effective.group(1))
            if day and ((start and day < start) or (end and day > end)):
                return False
        investors = {normalize_name(name) for key in keep for name in accounts[key].investors.values()}
        return any(investor and investor in normalize_name(line.template) for investor in investors)

    def focused_facts(self, terms: frozenset[str], limit: int = 8, days: frozenset[tuple[int, int]] = frozenset()) -> list[tuple[Citation, str]]:
        """Cited facts about the accounts a question names, most specific first.

        Facts dated on a (month, day) the question mentions rank first.

        Empty when the question names no account, fund or investor that has a
        record, so ordinary questions are unaffected.
        """
        if not terms:
            return []
        accounts = self._accounts()
        named = {entry.display for key, entry in accounts.items() if self._account_words(key, entry) & terms}
        if not named:
            return []
        facts = [(citation, description) for citation, description in self.citations() if any(account in description for account in named)]
        def dated(description: str) -> bool:
            return any((int(month), int(day)) in days for month, day in re.findall(r"\b\d{4}-(\d{2})-(\d{2})\b", description))

        facts.sort(key=lambda fact: (not dated(fact[1]), -sum(account in fact[1] for account in named)))
        return facts[:limit]

    def relevant_document_ids(self) -> frozenset[str]:
        """Documents that supply an in-scope record."""
        return frozenset(
            {item.quantity_citation[0] for item in self.holdings} | {item.citation[0] for item in self.transfers}
            | {item.citation[0] for item in self.aliases} | {line.citations[0][0] for line in self.assignments}
            | {item.citation[0] for item in self.migrations}
        )

    def scope_terms(self) -> frozenset[str]:
        """Account references, names and investors of in-scope accounts (to recognise other mentioning documents)."""
        terms: set[str] = set()
        for entry in self._accounts().values():
            terms.add(entry.display)
            terms.update(value for value, _citation in entry.names.values())
            terms.update(entry.investors.values())
        return frozenset(term for term in terms if len(term) >= 3)

    def _add_holding(self, document_id: str, extraction_id: str, filename: str, references: list[str], payload: Mapping[str, object], confirmed: bool) -> None:
        quantity = Decimal(str(payload["quantity"]))
        fund = payload.get("subfund") if isinstance(payload.get("subfund"), str) else None
        quantity_reference = self._locate(extraction_id, references, lambda text: quantity in find_amounts(text))
        name_reference = self._locate(extraction_id, references, lambda text: bool(fund) and text.strip() == fund) if fund else None
        investor = payload.get("investor") if isinstance(payload.get("investor"), str) else None
        investor_reference = self._locate(extraction_id, references, lambda text: bool(investor) and text.strip() == investor) if investor else None
        if quantity_reference is None:
            return
        citation = (document_id, extraction_id, quantity_reference, filename)
        self.holdings.append(_Holding(
            str(payload["account"]), str(payload["isin"]), quantity, str(payload["positionDate"]), fund, investor, confirmed,
            citation, (document_id, extraction_id, name_reference, filename) if name_reference else citation,
            (document_id, extraction_id, investor_reference, filename) if investor_reference else citation,
        ))

    def _add_transfer(self, document_id: str, extraction_id: str, filename: str, references: list[str], payload: Mapping[str, object], confirmed: bool, issued: str | None) -> None:
        from_account, to_account = str(payload["fromAccount"]), str(payload["toAccount"])
        effective = str(payload["effectiveDate"])
        main = self._locate(extraction_id, references, lambda text: from_account in text and to_account in text) or self._locate(extraction_id, references, lambda text: True)
        dated = self._locate(extraction_id, references, lambda text: any(value == effective for value, _start in find_dates(text)))
        if main is None:
            return
        citation = (document_id, extraction_id, main, filename)
        self.transfers.append(_Transfer(
            from_account, to_account,
            payload.get("fromSubfund") if isinstance(payload.get("fromSubfund"), str) else None,
            payload.get("toSubfund") if isinstance(payload.get("toSubfund"), str) else None,
            str(payload["isin"]), Decimal(str(payload["quantity"])), effective, issued, confirmed,
            citation, (document_id, extraction_id, dated, filename) if dated else citation,
        ))

    def _add_assignment(
        self, document_id: str, extraction_id: str, filename: str, references: list[str], payload: Mapping[str, object],
        confirmed: bool = False, issued: str | None = None,
    ) -> None:
        main = self._locate(extraction_id, references, lambda text: str(payload["toEntity"]) in " ".join(text.split())) or self._locate(extraction_id, references, lambda text: True)
        if main is None:
            return
        effective = str(payload["effectiveDate"])
        dated = self._locate(extraction_id, references, lambda text: any(value == effective for value, _start in find_dates(text)))
        issue_reference = self._locate(extraction_id, references, lambda text: bool(issued) and _ISSUE_LABEL.search(text) is not None and any(value == issued for value, _start in find_dates(text))) if issued else None
        scope_references = [
            f"page {int(row['page_number'])} block {int(row['block_index'])}" for row in self._connection.execute(
                "SELECT p.page_number, b.block_index, b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
                (extraction_id,),
            ) if _CLAIM_ONLY_SCOPE.search(" ".join(str(row["text"] or "").split()))
        ]
        citation = (document_id, extraction_id, main, filename)
        percentage = f" {payload['percentage']}% of" if payload.get("percentage") else ""
        self.assignments.append(_Assignment(
            f"Claim-right assignment effective {_day(effective)}: {_e(payload['fromEntity'])} assigned{_e(percentage)} "
            f"claim {_e(payload['claimReference'])} (ISIN {_e(payload['isin'])}) to {_e(payload['toEntity'])} {{0}}. "
            "This is a legal claim-right event; it does not move securities or change a holding.",
            (citation,), str(payload["claimReference"]), str(payload["fromEntity"]), str(payload["toEntity"]),
            str(payload["percentage"]) if payload.get("percentage") else None, str(payload["isin"]), effective, issued, confirmed,
            citation, (document_id, extraction_id, dated, filename) if dated else citation,
            (document_id, extraction_id, issue_reference, filename) if issue_reference else None,
            tuple((document_id, extraction_id, reference, filename) for reference in scope_references),
        ))

    def _locate(self, extraction_id: str, references: Sequence[str], accept: Callable[[str], bool]) -> str | None:
        """The first single cell or block in ``references`` whose text satisfies ``accept``."""
        for reference in references:
            block = _BLOCK.match(reference)
            if block:
                row = self._connection.execute(
                    "SELECT b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id "
                    "WHERE p.extraction_id = ? AND p.page_number = ? AND b.block_index = ?",
                    (extraction_id, int(block.group(1)), int(block.group(2))),
                ).fetchone()
                if row is not None and accept(str(row["text"] or "")):
                    return f"page {int(block.group(1))} block {int(block.group(2))}"
                continue
            cells = _RANGE.match(reference)
            if cells is None:
                continue
            for cell in self._connection.execute(
                "SELECT source_reference, value_text FROM extracted_content WHERE extraction_id = ? AND sheet_name = ? AND row_number = ? AND is_empty = 0 ORDER BY column_number",
                (extraction_id, cells.group("sheet"), int(cells.group("row"))),
            ):
                if accept(str(cell["value_text"] or "")):
                    return str(cell["source_reference"])
        return None

    # Views ----------------------------------------------------------------

    @property
    def has_records(self) -> bool:
        return bool(self.holdings or self.transfers or self.migrations)

    def coverage_line(self) -> Line:
        missing = sorted(filename for document_id, (_extraction, filename) in self._documents.items() if document_id not in self._record_documents)
        pending = sum(not item.confirmed for item in self.holdings) + sum(not item.confirmed for item in self.transfers)
        parts = []
        if pending:
            parts.append(f"{pending} of the records used are extracted records still awaiting review in Holdings & History.")
        if self._narrowed:
            parts.append("Records for other funds or dates in the selected documents were left out.")
        if missing:
            parts.append("No holdings, transfer, assignment or alias record was extracted from: " + _e(", ".join(missing)) + ".")
        return Line(" ".join(parts) or "All records used are confirmed.")

    def document_ids(self) -> frozenset[str]:
        return frozenset({item.quantity_citation[0] for item in self.holdings} | {item.citation[0] for item in self.transfers} | {item.citation[0] for item in self.aliases})

    def citations(self) -> list[tuple[Citation, str]]:
        """Every cited fact with a one-line description, for provider evidence."""
        facts: list[tuple[Citation, str]] = []
        for holding in sorted(self.holdings, key=lambda item: (item.day, item.account)):
            label = "HISTORICAL REQUIRED EVENT SOURCE (holding record)" if _BLOCK.match(holding.quantity_citation[2]) else "DOCUMENT-BACKED HOLDINGS ROW"
            facts.append((holding.quantity_citation, (
                f"{label}; EVIDENCE: Snapshot {holding.day}; {holding.fund or 'fund not named'}; "
                f"nominal {_grouped(holding.quantity)}; account {holding.account}; ISIN {holding.isin}"
                + ("" if holding.confirmed else "; awaiting review") + "."
            )))
        for transfer in sorted(self.transfers, key=lambda item: item.effective):
            facts.append((transfer.citation, (
                "HISTORICAL REQUIRED EVENT SOURCE (transfer record); EVIDENCE: "
                f"effective {transfer.effective}; {_grouped(transfer.quantity)} nominal ISIN {transfer.isin} from "
                f"{transfer.from_name or 'unnamed fund'} account {transfer.from_account} to {transfer.to_name or 'unnamed fund'} account {transfer.to_account}"
                + (f"; document dated {transfer.issued}" if transfer.issued else "") + "."
            )))
            if transfer.date_citation != transfer.citation:
                facts.append((transfer.date_citation, f"HISTORICAL REQUIRED EVENT SOURCE (effective date); EVIDENCE: effective date {transfer.effective}."))
        statements: dict[tuple[Citation, str, str | None], list[str]] = {}
        for alias in self.aliases:
            statements.setdefault((alias.citation, alias.canonical, alias.account), []).append(alias.alias)
        for (citation, canonical, account), names in statements.items():
            facts.append((citation, (
                "HISTORICAL REQUIRED EVENT SOURCE (alias statement); EVIDENCE: " + " and ".join(f"“{name}”" for name in names)
                + f" {'refers' if len(names) == 1 else 'refer'} to {canonical}" + (f" for account {account}" if account else "") + "."
            )))
        for migration in self.migrations:
            facts.append((migration.citation, (
                f"HISTORICAL REQUIRED EVENT SOURCE (account migration); EVIDENCE: account {migration.old_account} replaced by {migration.new_account}"
                + (f" effective {migration.effective}" if migration.effective else "")
                + (f" for {migration.investor}" if migration.investor else "") + (f", {migration.fund}" if migration.fund else "")
                + "; same fund, no securities moved."
            )))
            if migration.date_citation != migration.citation:
                facts.append((migration.date_citation, f"HISTORICAL REQUIRED EVENT SOURCE (effective date); EVIDENCE: effective date {migration.effective}."))
        for assignment in self.assignments:
            facts.append((assignment.citations[0], "HISTORICAL REQUIRED EVENT SOURCE (assignment record); EVIDENCE: " + assignment.template.replace(" {0}", "").replace("{{", "{").replace("}}", "}")))
        return facts

    def _accounts(self) -> dict[str, _Account]:
        accounts: dict[str, _Account] = {}

        def account(reference: str) -> _Account:
            return accounts.setdefault(normalize_account_reference(reference), _Account(reference))

        def name(entry: _Account, value: str | None, citation: Citation) -> None:
            if not value:
                return
            key = normalize_name(value)
            prefix = re.match(r"^\s*(?P<holder>.+?)(?:\s+[-–—:]\s+|['’]s\s+)(?P<rest>.+)$", value)
            if prefix and normalize_name(prefix.group("holder")) in entry.investors:
                key = normalize_name(prefix.group("rest"))
            if key not in entry.names:
                entry.names[key] = (value, citation)

        for holding in sorted(self.holdings, key=lambda item: item.day):
            entry = account(holding.account)
            entry.first_day = min(entry.first_day, holding.day)
            name(entry, holding.fund, holding.name_citation)
            if holding.investor:
                entry.investors.setdefault(normalize_name(holding.investor), holding.investor)
        for transfer in sorted(self.transfers, key=lambda item: item.effective):
            name(account(transfer.from_account), transfer.from_name, transfer.citation)
            name(account(transfer.to_account), transfer.to_name, transfer.citation)
        for alias in self.aliases:
            if alias.account:
                entry = account(alias.account)
                entry.aliases.append(alias)
                name(entry, alias.canonical, alias.citation)
                name(entry, alias.alias, alias.citation)
        for migration in self.migrations:
            for reference in (migration.old_account, migration.new_account):
                entry = account(reference)
                if migration.effective and entry.first_day == "9999-12-31":
                    entry.first_day = migration.effective
        return accounts

    def _funds(self, accounts: Mapping[str, _Account]) -> list[_Fund]:
        """Group accounts into funds by documented migrations and confirmed fund records.

        A migration links its two accounts unless their recorded investors
        differ or the statement names a different investor; contradictions
        keep them apart.  Accounts already confirmed under one fund record
        (for example by a "same fund" review answer) are grouped too.
        """
        parent = {key: key for key in accounts}

        def root(key: str) -> str:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        applied: dict[str, list[_Migration]] = {}
        for migration in self.migrations:
            old, new = normalize_account_reference(migration.old_account), normalize_account_reference(migration.new_account)
            old_investors, new_investors = set(accounts[old].investors), set(accounts[new].investors)
            stated = normalize_name(migration.investor) if migration.investor else None
            if (old_investors and new_investors and not old_investors & new_investors) or (stated and old_investors and stated not in old_investors):
                continue
            parent[root(new)] = root(old)
            applied.setdefault(old, []).append(migration)
        reviewed: set[str] = set()
        by_vehicle: dict[str, list[str]] = {}
        for key in accounts:
            if key in self._vehicle_of:
                by_vehicle.setdefault(self._vehicle_of[key], []).append(key)
        for keys in by_vehicle.values():
            for key in keys[1:]:
                if root(key) != root(keys[0]):
                    reviewed.add(root(keys[0]))
                    parent[root(key)] = root(keys[0])
        groups: dict[str, list[str]] = {}
        for key in accounts:
            groups.setdefault(root(key), []).append(key)
        funds = []
        for group_root, keys in groups.items():
            keys.sort(key=lambda key: (accounts[key].first_day, accounts[key].display))
            migrations = [migration for key in keys for migration in applied.get(key, [])]
            funds.append(_Fund(keys, sorted(migrations, key=lambda item: item.effective or ""), reviewed_link=root(group_root) in reviewed and not migrations))
        return sorted(funds, key=lambda fund: (accounts[fund.keys[0]].first_day, accounts[fund.keys[0]].display))

    def _fund_label(self, fund: _Fund, accounts: Mapping[str, _Account]) -> str:
        for key in fund.keys:
            for alias in accounts[key].aliases:
                return alias.canonical
        for key in fund.keys:
            if accounts[key].names:
                return self._label(accounts[key])
        return f"account {accounts[fund.keys[0]].display}"

    def _separation_citations(self, accounts: Sequence[str]) -> list[Citation]:
        """Blocks in the documents used that say they do not concern one of ``accounts``."""
        return [citation for citation, text in self._denial_blocks if any(account in text for account in accounts)]

    @staticmethod
    def _label(entry: _Account) -> str:
        for alias in entry.aliases:
            return alias.canonical
        return next(iter(entry.names.values()))[0] if entry.names else f"account {entry.display}"

    # Fund identities -------------------------------------------------------

    def identity_lines(self) -> list[Line]:
        accounts = self._accounts()
        if not any(entry.names for entry in accounts.values()) and not self.migrations:
            return []
        funds = self._funds(accounts)
        lines = [Line(IDENTITY_INTRO)]
        for number, fund in enumerate(funds, start=1):
            label = self._fund_label(fund, accounts)
            investors = list(dict.fromkeys(investor for key in fund.keys for investor in accounts[key].investors.values()))
            investor_text = f" (investor {_e(', '.join(investors))})" if investors else ""
            if len(fund.keys) == 1:
                lines.append(Line(f"{number}. {_e(label)} — account {_e(accounts[fund.keys[0]].display)}{investor_text}"))
            else:
                lines.append(Line(f"{number}. {_e(label)} — accounts {' → '.join(_e(accounts[key].display) for key in fund.keys)}{investor_text}"))
            for migration in fund.migrations:
                cited = (migration.citation,) if migration.date_citation == migration.citation else (migration.citation, migration.date_citation)
                markers = " ".join(f"{{{index}}}" for index in range(len(cited)))
                lines.append(Line(
                    f"   Account migration: account {_e(migration.old_account)} was replaced by {_e(migration.new_account)}"
                    + (f", effective {_day(migration.effective)}" if migration.effective else ", effective date not stated")
                    + f" {markers}. It is the same fund; the account identifier changed and no securities moved.",
                    cited,
                ))
            if fund.reviewed_link:
                lines.append(Line("   These accounts were linked to one fund by a confirmed ClaimFlow review decision, not by a document statement."))
            for key in fund.keys:
                entry = accounts[key]
                recorded = list(entry.names.values())
                if not recorded:
                    continue
                prefix = "   Names recorded for this account: " if len(fund.keys) == 1 else f"   Names recorded for account {_e(entry.display)}: "
                lines.append(Line(prefix + ", ".join(f"“{_e(value)}” {{{index}}}" for index, (value, _citation) in enumerate(recorded)), tuple(citation for _value, citation in recorded)))
                documented = {normalize_name(alias.alias) for alias in entry.aliases} | {normalize_name(alias.canonical) for alias in entry.aliases}
                for alias in entry.aliases:
                    lines.append(Line(f"   Documented as the same fund: “{_e(alias.alias)}” refers to {_e(alias.canonical)} for account {_e(alias.account)} {{0}}.", (alias.citation,)))
                label_key = normalize_name(label)
                for name_key, (value, citation) in entry.names.items():
                    if name_key != label_key and name_key not in documented:
                        lines.append(Line(
                            f"   “{_e(value)}” is recorded under this account {{0}}, but no document used states it is the same fund as {_e(label)}.",
                            (citation,),
                        ))
            if len(fund.keys) == 1 and len(accounts[fund.keys[0]].names) == 1:
                lines.append(Line("   No other name is used for this account."))
        lines.extend(self._shared_name_lines(funds, accounts))
        return lines

    def _shared_name_lines(self, funds: Sequence[_Fund], accounts: Mapping[str, _Account]) -> list[Line]:
        """Explain each name used by more than one fund, and why they stay separate."""
        holdings_by_account: dict[str, _Holding] = {}
        for holding in sorted(self.holdings, key=lambda item: item.day):
            holdings_by_account.setdefault(normalize_account_reference(holding.account), holding)
        by_name: dict[str, list[_Fund]] = {}
        for fund in funds:
            for name_key in {name for key in fund.keys for name in accounts[key].names}:
                by_name.setdefault(name_key, []).append(fund)
        lines: list[Line] = []
        for name_key, sharing in sorted(by_name.items()):
            if len(sharing) < 2:
                continue
            value = next(accounts[key].names[name_key][0] for key in sharing[0].keys if name_key in accounts[key].names)
            parts, cited = [], []
            investor_sets = []
            for fund in sharing:
                references = " / ".join(accounts[key].display for key in fund.keys)
                investors = sorted({investor for key in fund.keys for investor in accounts[key].investors.values()})
                investor_sets.append(frozenset(normalize_name(investor) for investor in investors))
                holding = next((holdings_by_account[key] for key in fund.keys if key in holdings_by_account), None)
                citation = holding.investor_citation if holding is not None and investors else next(accounts[key].names[name_key][1] for key in fund.keys if name_key in accounts[key].names)
                parts.append(f"account {_e(references)}" + (f" held by {_e(', '.join(investors))}" if investors else "") + f" {{{len(cited)}}}")
                cited.append(citation)
            reason = "No document used links these accounts to one fund"
            if all(investor_sets) and len(set(investor_sets)) == len(investor_sets):
                reason = "They belong to different investors and no document used links them"
            text = f"The name “{_e(value)}” is used by separate funds: " + "; ".join(parts) + f". {reason}, so they are kept separate."
            separations = self._separation_citations([accounts[key].display for fund in sharing for key in fund.keys])
            if separations:
                markers = " ".join(f"{{{len(cited) + index}}}" for index in range(len(separations)))
                text += f" A document also states it does not relate to the other fund {markers}."
                cited.extend(separations)
            lines.append(Line(text, tuple(cited)))
        return lines

    # Holdings conflicts ------------------------------------------------------

    def _conflicts(self, accounts: Mapping[str, _Account], funds: Sequence[_Fund]) -> list[tuple[_Fund, str, str, list[_Holding]]]:
        """Competing holdings for one fund, security and as-of date.

        Accounts tied by a documented migration count as one position; other
        accounts are separate positions and never compared with each other.
        """
        position_of: dict[str, str] = {}
        for index, fund in enumerate(funds):
            for key in fund.keys:
                position_of[key] = f"fund-{index}" if fund.migrations else key
        groups: dict[tuple[str, str, str], list[_Holding]] = {}
        for holding in self.holdings:
            key = normalize_account_reference(holding.account)
            groups.setdefault((position_of.get(key, key), holding.isin, holding.day), []).append(holding)
        fund_of = {key: fund for fund in funds for key in fund.keys}
        conflicts = []
        for (_position, isin, day), items in groups.items():
            if len({item.quantity for item in items}) > 1:
                conflicts.append((fund_of[normalize_account_reference(items[0].account)], isin, day, sorted(items, key=lambda item: -item.quantity)))
        return sorted(conflicts, key=lambda item: (item[2], self._fund_label(item[0], accounts), item[1]))

    def _status(self, items: Sequence[_Holding], isin: str, day: str) -> str:
        statuses = {self._conflict_status.get((normalize_account_reference(item.account), isin, day)) for item in items} - {None}
        if "open" in statuses:
            return "unresolved — recorded as an open issue in Issues"
        if statuses == {"resolved"}:
            return "marked resolved in ClaimFlow"
        return "unresolved — not yet recorded as an issue"

    def _expected_balance(self, fund: _Fund, isin: str, day: str, account_key: str) -> tuple[Decimal | None, list[Line], str | None]:
        """The balance the other records imply just before ``day`` for ``account_key``.

        Returns (balance, cited steps, missing-evidence explanation).  Uses the
        latest earlier documented holding in the fund, then migrations and
        transfers dated after it and before ``day``.
        """
        keys = set(fund.keys)
        events: list[tuple[str, int, object]] = []
        snapshots: dict[tuple[str, str], list[_Holding]] = {}
        for holding in self.holdings:
            key = normalize_account_reference(holding.account)
            if key in keys and holding.isin == isin and holding.day < day:
                snapshots.setdefault((key, holding.day), []).append(holding)
        for (key, when), items in snapshots.items():
            events.append((when, 0, (key, items)))
        for migration in fund.migrations:
            if migration.effective and migration.effective < day:
                events.append((migration.effective, 1, migration))
        same_day_transfer = False
        for transfer in self.transfers:
            sides = {normalize_account_reference(transfer.from_account), normalize_account_reference(transfer.to_account)}
            if transfer.isin == isin and sides & keys:
                if transfer.effective < day:
                    events.append((transfer.effective, 2, transfer))
                elif transfer.effective == day:
                    same_day_transfer = True
        balance: dict[str, Decimal | None] = {}
        steps: dict[str, list[Line]] = {}
        for when, _order, event in sorted(events, key=lambda item: (item[0], item[1])):
            if isinstance(event, tuple):
                key, items = event
                values = {item.quantity for item in items}
                if len(values) > 1:
                    balance[key], steps[key] = None, []
                    continue
                balance[key] = items[0].quantity
                steps[key] = [Line(f"{_grouped(items[0].quantity)} documented for account {_e(items[0].account)} on {_day(when)} {{0}}", (items[0].quantity_citation,))]
            elif isinstance(event, _Migration):
                old, new = normalize_account_reference(event.old_account), normalize_account_reference(event.new_account)
                cited = (event.citation,) if event.date_citation == event.citation else (event.citation, event.date_citation)
                markers = " ".join(f"{{{index}}}" for index in range(len(cited)))
                balance[new], steps[new] = balance.get(old), [*steps.get(old, []), Line(
                    f"account migration from {_e(event.old_account)} to {_e(event.new_account)} effective {_day(event.effective)}, which moved no securities {markers}", cited,
                )]
                balance[old] = None
            else:
                transfer = event
                assert isinstance(transfer, _Transfer)
                cited = (transfer.citation,) if transfer.date_citation == transfer.citation else (transfer.citation, transfer.date_citation)
                markers = " ".join(f"{{{index}}}" for index in range(len(cited)))
                for key, outgoing in ((normalize_account_reference(transfer.from_account), True), (normalize_account_reference(transfer.to_account), False)):
                    if key not in keys or balance.get(key) is None:
                        continue
                    balance[key] = balance[key] - transfer.quantity if outgoing else balance[key] + transfer.quantity  # type: ignore[operator]
                    steps[key].append(Line(
                        f"{'less' if outgoing else 'plus'} {_grouped(transfer.quantity)} transferred {'out' if outgoing else 'in'} effective {_day(transfer.effective)} {markers}", cited,
                    ))
        if same_day_transfer:
            return None, [], f"a transfer is dated {_day(day)} itself, so the order of that transfer and the holdings records cannot be established"
        if balance.get(account_key) is None:
            return None, [], f"no earlier documented holding for this position before {_day(day)} (or the earlier records conflict)"
        return balance[account_key], steps[account_key], None

    def conflict_lines(self, *, assess: bool) -> list[Line]:
        """Every holdings conflict in scope; with ``assess``, which value the other records support."""
        accounts = self._accounts()
        funds = self._funds(accounts)
        conflicts = self._conflicts(accounts, funds)
        if not conflicts:
            return [Line("No conflicting holdings records were found in the extracted records of the selected documents.")]
        count = len(conflicts)
        lines = [Line(f"{count} holdings {'conflict was' if count == 1 else 'conflicts were'} found in the selected documents. ClaimFlow keeps every competing value and does not choose between them.")]
        for number, (fund, isin, day, items) in enumerate(conflicts, start=1):
            label = self._fund_label(fund, accounts)
            investors = ", ".join(dict.fromkeys(item.investor for item in items if item.investor)) or ", ".join(
                dict.fromkeys(investor for key in fund.keys for investor in accounts[key].investors.values())
            )
            account_text = " / ".join(dict.fromkeys(item.account for item in items))
            lines.append(Line(
                f"{number}. {_e(label)}" + (f" — investor {_e(investors)}" if investors else "")
                + f" — account {_e(account_text)} — ISIN {_e(isin)} — as of {_day(day)}"
            ))
            for item in items:
                pending = "" if item.confirmed else ", awaiting review"
                lines.append(Line(f"   • {_grouped(item.quantity)} nominal {{0}}{pending}", (item.quantity_citation,)))
            values = sorted({item.quantity for item in items})
            difference = (
                f"{_grouped(values[-1] - values[0])} nominal" if len(values) == 2
                else f"{_grouped(values[-1] - values[0])} nominal between the highest and lowest of {len(values)} values"
            )
            lines.append(Line(f"   Difference: {difference}. Status: {self._status(items, isin, day)}."))
            if not assess:
                continue
            expected, steps, missing = self._expected_balance(fund, isin, day, normalize_account_reference(items[0].account))
            if expected is None:
                lines.append(Line(
                    f"   Which value is better supported cannot be assessed from the records used: {missing}. "
                    "Both values stand until supporting evidence or an authorised decision resolves the conflict."
                ))
                continue
            templates, cited = [], []
            for step in steps:
                offset = len(cited)
                templates.append(re.sub(r"\{(\d+)\}", lambda match: "{" + str(int(match.group(1)) + offset) + "}", step.template))
                cited.extend(step.citations)
            lines.append(Line("   Expected balance from the other records: " + "; ".join(templates) + f" → {_grouped(expected)}.", tuple(cited)))
            matching = [item for item in items if item.quantity == expected]
            others = [item for item in items if item.quantity != expected]
            if matching:
                lines.append(Line(
                    f"   Assessment: {_grouped(expected)} matches that reconstruction, so it has stronger supporting evidence; "
                    + ", ".join(_grouped(item.quantity) for item in others)
                    + " is not explained by any record used. This is an evidence assessment, not a resolution: the "
                    + ", ".join(f"{_grouped(item.quantity)} ({_e(item.quantity_citation[3])})" for item in others)
                    + " value is not overridden, and the conflict stays unresolved until a correction, a reconciliation, "
                    "or an authorised decision resolves it."
                ))
            else:
                lines.append(Line(
                    f"   Assessment: neither value matches the expected {_grouped(expected)}, so the records used do not support "
                    "either one. The conflict stays unresolved until supporting evidence or an authorised decision resolves it."
                ))
        return lines

    # Claim assignments, contextual fund labels ---------------------------------

    def claim_lines(self) -> list[Line]:
        """Who received claim rights, when, and whether securities moved as a result."""
        if not self.assignments:
            return [Line("No claim-right assignment record was extracted from the selected documents, so these documents do not establish a change in legal claim ownership.")]
        lines: list[Line] = []
        for assignment in sorted(self.assignments, key=lambda item: item.effective):
            share = f"{assignment.percentage}% of its" if assignment.percentage else "its"
            cited = [assignment.main_citation] + ([assignment.date_citation] if assignment.date_citation != assignment.main_citation else [])
            markers = " ".join(f"{{{index}}}" for index in range(len(cited)))
            lines.append(Line(
                f"{_e(assignment.assignor)} assigned {_e(share)} legal claim rights under claim {_e(assignment.claim_reference)} "
                f"(ISIN {_e(assignment.isin)}) to {_e(assignment.assignee)}, effective {_day(assignment.effective)} {markers}.",
                tuple(cited),
            ))
            if assignment.issued:
                issue_cited = (assignment.issue_citation,) if assignment.issue_citation else ()
                lines.append(Line(
                    f"   The assignment document is dated {_day(assignment.issued)}" + (" {0}" if issue_cited else "")
                    + "; that is its issue date, not the date the assignment took effect.",
                    issue_cited,
                ))
            if assignment.scope_citations:
                markers = " ".join(f"{{{index}}}" for index in range(len(assignment.scope_citations)))
                lines.append(Line(
                    f"   Scope: the document states that the assignment covers claim rights only and does not move securities or change custody balances {markers}.",
                    assignment.scope_citations,
                ))
            lines.extend(self._securities_after(assignment))
        lines.append(Line(
            "This summarises what the documents record. It is not a view on the assignment's legal validity or enforceability."
            + ("" if all(item.confirmed for item in self.assignments) else " The assignment record is awaiting review in Holdings & History.")
        ))
        return lines

    def _securities_after(self, assignment: _Assignment) -> list[Line]:
        moved = [
            transfer for transfer in self.transfers
            if transfer.isin == assignment.isin and transfer.effective >= assignment.effective
            and normalize_name(transfer.to_name or "") == normalize_name(assignment.assignee)
        ]
        if moved:
            return [Line(
                f"   A securities transfer to {_e(assignment.assignee)} is recorded separately: " + "; ".join(
                    f"{_grouped(item.quantity)} effective {_day(item.effective)} {{{index}}}" for index, item in enumerate(moved)
                ) + ". That transfer, not the assignment, is the evidence of any change in holdings.",
                tuple(item.citation for item in moved),
            )]
        later = [item for item in self.holdings if item.isin == assignment.isin and item.day >= assignment.effective]
        if not later:
            return [Line(f"   Securities: no securities transfer to {_e(assignment.assignee)} was extracted, and no holdings record dated after the assignment is available to show who holds the securities.")]
        latest = max(item.day for item in later)
        rows = sorted((item for item in later if item.day == latest), key=lambda item: item.account)[:6]
        holders = ", ".join(dict.fromkeys(item.investor for item in rows if item.investor)) or "the existing accounts"
        return [Line(
            f"   Securities: no securities transfer to {_e(assignment.assignee)} was extracted. The holdings records dated {_day(latest)} still show the securities in accounts held by {_e(holders)}: "
            + "; ".join(f"{_e(item.account)} {_grouped(item.quantity)} {{{index}}}" for index, item in enumerate(rows))
            + f". On these records, {_e(assignment.assignee)} did not receive securities as a result of the assignment.",
            tuple(item.quantity_citation for item in rows),
        )]

    def resolve_label(self, label: str, investor_hint: str | None = None) -> tuple[str, list[str]] | None:
        """The one fund a participant or fund label refers to: (fund name, account references).

        An exact fund name matches directly.  A role-and-marker label such as
        "Participant A" or "Fund B" matches the single fund whose name ends in
        that marker, among the funds of the investor named in ``investor_hint``
        when given.  Anything ambiguous returns None.
        """
        from .billing_instruction import is_generic_label, participant_marker
        accounts = self._accounts()
        funds = self._funds(accounts)
        key = normalize_name(re.sub(r"\s*\([^)]*\)\s*$", "", label))
        exact = [fund for fund in funds if any(key in accounts[member].names for member in fund.keys)]
        if len(exact) == 1:
            return self._fund_label(exact[0], accounts), [accounts[member].display for member in exact[0].keys]
        if not is_generic_label(label):
            return None
        marker = participant_marker(label)
        hint = normalize_name(investor_hint) if investor_hint else ""
        matches = []
        for fund in funds:
            fund_label = self._fund_label(fund, accounts)
            investors = {name for member in fund.keys for name in accounts[member].investors}
            if hint and not any(investor in hint for investor in investors):
                continue
            if participant_marker(fund_label) == marker:
                matches.append(fund)
        if len(matches) != 1:
            return None
        return self._fund_label(matches[0], accounts), [accounts[member].display for member in matches[0].keys]

    def holding_on(self, accounts: Sequence[str], day: str, quantity: Decimal) -> Citation | None:
        """The citation of a holding of exactly ``quantity`` on ``day`` in one of ``accounts``."""
        wanted = {normalize_account_reference(account) for account in accounts}
        for holding in self.holdings:
            if normalize_account_reference(holding.account) in wanted and holding.day == day and holding.quantity == quantity:
                return holding.quantity_citation
        return None

    # Historical reconstruction ---------------------------------------------

    def reconstruction_lines(self) -> list[Line]:
        accounts = self._accounts()
        funds = self._funds(accounts)
        lines = [Line(RECONSTRUCTION_INTRO)]
        isins: dict[str, set[str]] = {}
        for holding in self.holdings:
            isins.setdefault(normalize_account_reference(holding.account), set()).add(holding.isin)
        for transfer in self.transfers:
            for reference in (transfer.from_account, transfer.to_account):
                isins.setdefault(normalize_account_reference(reference), set()).add(transfer.isin)
        for fund in funds:
            for isin in sorted({isin for key in fund.keys for isin in isins.get(key, set())}):
                lines.extend(self._fund_section(fund, isin, accounts))
        for assignment in self.assignments:
            lines.append(assignment)
        if not self.transfers:
            lines.append(Line("No securities transfer record was extracted from the documents used."))
        return lines

    def _fund_section(self, fund: _Fund, isin: str, accounts: Mapping[str, _Account]) -> list[Line]:
        label = self._fund_label(fund, accounts)
        single = len(fund.keys) == 1
        keys = set(fund.keys)
        if single:
            lines = [Line(f"{_e(label)} — account {_e(accounts[fund.keys[0]].display)}, ISIN {_e(isin)}:")]
        else:
            lines = [Line(f"{_e(label)} — accounts {' → '.join(_e(accounts[key].display) for key in fund.keys)}, ISIN {_e(isin)}:")]
        events: list[tuple[str, int, object]] = []
        snapshots: dict[tuple[str, str], list[_Holding]] = {}
        for holding in self.holdings:
            key = normalize_account_reference(holding.account)
            if key in keys and holding.isin == isin:
                snapshots.setdefault((key, holding.day), []).append(holding)
        for (key, day), items in snapshots.items():
            events.append((day, 0, (key, items)))
        for migration in fund.migrations:
            events.append((migration.effective or "9999-12-31", 1, migration))
        for transfer in self.transfers:
            if transfer.isin == isin and {normalize_account_reference(transfer.from_account), normalize_account_reference(transfer.to_account)} & keys:
                events.append((transfer.effective, 2, transfer))
        balance: dict[str, Decimal | None] = {}
        moved: dict[str, bool] = {}
        snapshot_days = {(key, day) for key, day in snapshots}
        where = (lambda key: "") if single else (lambda key: f"account {_e(accounts[key].display)} ")
        for day, _order, event in sorted(events, key=lambda item: (item[0], item[1])):
            if isinstance(event, _Migration):
                old, new = normalize_account_reference(event.old_account), normalize_account_reference(event.new_account)
                cited = (event.citation,) if event.date_citation == event.citation else (event.citation, event.date_citation)
                markers = " ".join(f"{{{index}}}" for index in range(len(cited)))
                text = (f" • {_day(event.effective)}: account migration — account {_e(event.old_account)} replaced by {_e(event.new_account)} {markers}; "
                        "no securities moved")
                carried = balance.get(old)
                if carried is not None:
                    text += f"; {_grouped(carried)} carried to account {_e(event.new_account)} (inferred)."
                    balance[new], moved[new] = carried, True
                else:
                    text += "; no earlier documented balance to carry."
                balance[old] = None
                lines.append(Line(text, cited))
                continue
            if isinstance(event, tuple):
                key, items = event
                values = sorted({item.quantity for item in items})
                if len(values) > 1:
                    lines.append(Line(
                        f" • {_day(day)}: {where(key)}conflicting records — " + " vs ".join(f"{_grouped(item.quantity)} {{{index}}}" for index, item in enumerate(items))
                        + f" (difference {_grouped(values[-1] - values[0])}); unresolved, no balance is chosen."
                        + self._conflict_hint(balance.get(key), moved.get(key, False), values),
                        tuple(item.quantity_citation for item in items),
                    ))
                    balance[key] = None
                    continue
                documented, previous = values[0], balance.get(key)
                comparison = ""
                if previous is not None and moved.get(key):
                    comparison = (
                        " — consistent with the inferred balance." if previous == documented
                        else f" — differs from the inferred {_grouped(previous)} by {_grouped(abs(documented - previous))}; not explained by the records used."
                    )
                elif previous is not None:
                    comparison = (
                        " — unchanged from the previous documented balance." if previous == documented
                        else f" — changed by {_grouped(abs(documented - previous))} with no transfer in the records used to explain it."
                    )
                pending = "" if all(item.confirmed for item in items) else " (awaiting review)"
                lines.append(Line(f" • {_day(day)}: {where(key)}{_grouped(documented)} documented{pending} {{0}}{comparison}", (items[0].quantity_citation,)))
                balance[key], moved[key] = documented, False
                continue
            transfer = event
            assert isinstance(transfer, _Transfer)
            source, destination = normalize_account_reference(transfer.from_account), normalize_account_reference(transfer.to_account)
            cited = (transfer.citation,) if transfer.date_citation == transfer.citation else (transfer.citation, transfer.date_citation)
            markers = " ".join(f"{{{index}}}" for index in range(len(cited)))
            for key, outgoing in ((source, True), (destination, False)):
                if key not in keys:
                    continue
                other = accounts[destination if outgoing else source]
                other_label = self._fund_label(next((item for item in self._funds(accounts) if (destination if outgoing else source) in item.keys)), accounts)
                direction = f"out to {_e(other_label)} (account {_e(other.display)})" if outgoing else f"in from {_e(other_label)} (account {_e(other.display)})"
                text = f" • {_day(day)}: {where(key)}securities transfer of {_grouped(transfer.quantity)} {direction}, effective {_day(transfer.effective)}"
                if transfer.issued and transfer.issued != transfer.effective:
                    text += f" (notice dated {_day(transfer.issued)})"
                text += f" {markers}"
                if (key, day) in snapshot_days:
                    text += "; a holdings record has the same date, so its order relative to the transfer is unresolved and no balance is inferred."
                    balance[key] = None
                elif balance.get(key) is None:
                    text += "; no earlier documented balance, so no balance is inferred."
                else:
                    balance[key] = balance[key] - transfer.quantity if outgoing else balance[key] + transfer.quantity  # type: ignore[operator]
                    moved[key] = True
                    text += f" → {_grouped(balance[key])} inferred."  # type: ignore[arg-type]
                lines.append(Line(text, cited))
        return lines

    @staticmethod
    def _conflict_hint(inferred: Decimal | None, moved: bool, values: Sequence[Decimal]) -> str:
        if inferred is None or not moved:
            return ""
        matching = [value for value in values if value == inferred]
        return f" The inferred balance {_grouped(inferred)} matches one of the records, which does not by itself resolve the conflict." if matching else ""


def render(lines: Sequence[Line], cite: Callable[[Citation], str | None]) -> tuple[str, list[Citation], bool]:
    """Render lines with ``cite`` tokens.

    A line whose source cannot be cited is left out rather than shown with a
    placeholder; a closing sentence states how many were left out.  Returns
    the text, the citations used and whether nothing was left out.
    """
    rendered: list[str] = []
    used: list[Citation] = []
    omitted = 0
    for line in lines:
        tokens = [cite(citation) for citation in line.citations]
        if any(token is None for token in tokens):
            omitted += 1
            continue
        for citation in line.citations:
            if citation not in used:
                used.append(citation)
        rendered.append(line.template.format(*tokens) if line.citations else line.template)
    if omitted:
        rendered.append(
            f"{omitted} {'step was' if omitted == 1 else 'steps were'} left out because the supporting source did not fit "
            "in this bounded request; narrow the question by fund or date to include them."
        )
    return "\n".join(rendered), used, omitted == 0
