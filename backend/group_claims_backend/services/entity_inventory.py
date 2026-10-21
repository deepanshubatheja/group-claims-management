"""Local, evidence-linked entity inventory for Matter Review listing questions.

An inventory question ("list all investors and subfunds") cannot be answered
from a few top-ranked excerpts, and sending every document to a provider is
neither bounded nor authorized.  This module therefore examines the indexed
extraction of every eligible document locally and assigns a role only where
the document itself states it:

* a spreadsheet column heading such as "Investor" or "Subfund";
* an explicit label line such as "Custodian: Example Bank";
* an explicit phrase such as "Fund B, a sub-fund of Umbrella SICAV";
* an earlier evidence-validated AI review proposal, kept provisional and with
  its original, broader proposal type.

Organisation names without such a label are not classified.  Co-occurrence
in one spreadsheet row is reported as a documented association, never as a
legal relationship, ownership, claimant status, or invoicing relationship.
Nothing here contacts an AI provider.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .ai_analysis import AIAnalysisService
from .document_search import DocumentSearchIndex, SearchHit
from .question_intent import (
    ROLE_BROKER, ROLE_CLAIMANT, ROLE_CUSTODIAN, ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY, ROLE_OTHER,
    ROLE_SUBFUND, ROLE_UNKNOWN, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND,
)

# Bounded local work.  Exceeding any of these is reported as partial coverage.
MAX_HITS_PER_DOCUMENT = 2_000
MAX_TOTAL_HITS = 40_000
MAX_TABLE_ROWS = 1_000
MAX_TABLE_CELLS = 60_000
MAX_ENTITIES = 150
MAX_EVIDENCE_PER_ENTITY = 4
MAX_CITATIONS = 60
MAX_RELATIONSHIPS = 120

ROLE_LABELS: Mapping[str, str] = {
    ROLE_INVESTOR: "Investor",
    ROLE_VEHICLE: "Investment vehicle",
    ROLE_SUBFUND: "Subfund",
    ROLE_CUSTODIAN: "Custodian",
    ROLE_BROKER: "Broker/dealer",
    ROLE_CLAIMANT: "Legal claimant",
    ROLE_OTHER: "Other organization",
    ROLE_UNKNOWN: "Unknown role",
    ROLE_INVESTOR_OR_ENTITY: "Investor or legal entity",
    ROLE_VEHICLE_OR_SUBFUND: "Investment vehicle or subfund",
}

# Document wording that explicitly names a role (English and German).
_LABEL_ROLES: Mapping[str, str] = {
    "investor": ROLE_INVESTOR, "investorin": ROLE_INVESTOR, "anleger": ROLE_INVESTOR,
    "subfund": ROLE_SUBFUND, "sub-fund": ROLE_SUBFUND, "sub fund": ROLE_SUBFUND, "teilfonds": ROLE_SUBFUND,
    "compartment": ROLE_SUBFUND,
    "investment vehicle": ROLE_VEHICLE, "vehicle": ROLE_VEHICLE, "fund": ROLE_VEHICLE, "fonds": ROLE_VEHICLE,
    "umbrella fund": ROLE_VEHICLE, "umbrella": ROLE_VEHICLE, "parent fund": ROLE_VEHICLE, "parent vehicle": ROLE_VEHICLE,
    "dachfonds": ROLE_VEHICLE,
    "custodian": ROLE_CUSTODIAN, "custodian bank": ROLE_CUSTODIAN, "depotbank": ROLE_CUSTODIAN,
    "depositary": ROLE_CUSTODIAN, "verwahrstelle": ROLE_CUSTODIAN,
    "broker": ROLE_BROKER, "dealer": ROLE_BROKER, "broker-dealer": ROLE_BROKER, "broker/dealer": ROLE_BROKER,
    "claimant": ROLE_CLAIMANT, "plaintiff": ROLE_CLAIMANT, "kläger": ROLE_CLAIMANT, "klägerin": ROLE_CLAIMANT,
    "anspruchsteller": ROLE_CLAIMANT,
    "organisation": ROLE_OTHER, "organization": ROLE_OTHER, "company": ROLE_OTHER, "firm": ROLE_OTHER,
    "institution": ROLE_OTHER,
    "entity": ROLE_UNKNOWN, "legal entity": ROLE_UNKNOWN, "counterparty": ROLE_UNKNOWN, "party": ROLE_UNKNOWN,
}
# Headings that themselves state a structural relationship to a subfund.
_RELATION_HEADINGS = frozenset({"umbrella fund", "umbrella", "parent fund", "parent vehicle", "dachfonds"})
_LABEL_ALTERNATION = "|".join(re.escape(label) for label in sorted(_LABEL_ROLES, key=len, reverse=True))
_HEADER = re.compile(
    rf"^(?:name\s+of\s+(?:the\s+)?)?(?P<label>{_LABEL_ALTERNATION})(?:\s*(?:name|bezeichnung|\(name\)))?\s*:?$"
)
_LABEL_LINE = re.compile(
    rf"^\s*(?P<label>{_LABEL_ALTERNATION})(?:\s+name)?\s*[:–—]\s*(?P<value>\S.{{0,118}}?)\s*$", re.IGNORECASE,
)
# A spreadsheet label cell whose value sits in the next cell of the same row
# ("Investor / legal entity" | "Northstar Investors LP").  Compound labels must
# name exactly one specific role.
_PAIR_LABEL = re.compile(rf"^(?P<labels>(?:{_LABEL_ALTERNATION})(?:\s*(?:/|\bor\b|&)\s*(?:{_LABEL_ALTERNATION}))*)\s*:?$")
# Column-heading words that can follow a role heading in a two-column header
# row; such a neighbour is a heading, not a labelled value.
_HEADING_WORDS = frozenset({
    "account", "isin", "quantity", "amount", "nominal", "currency", "date", "as of date", "units", "unit", "holding",
    "holdings", "position", "status", "note", "notes", "name", "value", "total", "security", "konto", "menge", "datum",
})
_INVESTOR_SIDE = frozenset({ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY})
_HOLDING_SIDE = frozenset({ROLE_SUBFUND, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND})
_SUBFUND_OF = re.compile(
    r"(?P<child>[A-Z][\w&.'\- ]{1,80}?)\s*,?\s+(?:is\s+)?(?:a|the|ein|der)\s+(?:sub-?\s?fund|teilfonds)\s+"
    r"(?:of|des|der)\s+(?P<parent>[A-Z][\w&.'\- ]{1,80}?)(?=[,.;:\n)]|$)"
)
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_DATE_OR_AMOUNT = re.compile(r"^[\d\s.,:/\-+%()€$£]+$")
# Compatible broad/narrow pairs are not a conflict (an AI "investor or legal
# entity" finding agrees with a document's "Investor" heading).
_SPECIFIC_ROLES = frozenset({ROLE_INVESTOR, ROLE_VEHICLE, ROLE_SUBFUND, ROLE_CUSTODIAN, ROLE_BROKER, ROLE_CLAIMANT, ROLE_OTHER})
_COMPATIBLE = {
    frozenset({ROLE_VEHICLE, ROLE_SUBFUND}),  # a subfund is also a vehicle in many structures
}


@dataclass(frozen=True)
class InventoryDocument:
    document_id: str
    filename: str
    extraction_id: str


@dataclass(frozen=True)
class Mention:
    document_id: str
    extraction_id: str
    filename: str
    source_reference: str
    snippet: str
    role: str
    basis: str
    status: str  # "document_label" or "provisional_ai_finding"


@dataclass(frozen=True)
class Relation:
    document_id: str
    filename: str
    subject: str
    subject_role: str
    subject_reference: str
    related: str
    related_role: str
    related_reference: str
    # "explicit_statement" (document wording), "explicit_heading" (a column
    # heading such as "Umbrella fund"), "document_section" (one labelled
    # investor above the table that lists the subfunds), or "same_row"
    # (association only).  None of these establishes legal ownership.
    kind: str
    basis: str


@dataclass
class _Entity:
    name: str
    mentions: list[Mention] = field(default_factory=list)
    related: dict[tuple[str, str], str] = field(default_factory=dict)  # (role, key) -> display name

    @property
    def roles(self) -> list[str]:
        return sorted({mention.role for mention in self.mentions})


@dataclass(frozen=True)
class InventoryResult:
    answer: str
    payload: Mapping[str, object]
    citations: tuple[tuple[str, str, str, str], ...]
    evidence_document_ids: frozenset[str]
    partial: bool


def entity_key(name: str) -> str:
    """Merge only identical names: case, spacing and surrounding punctuation may differ."""
    return " ".join(name.casefold().strip(" \t\"'“”‘’.,;:()[]").split())


def _clean_value(value: str, *, prose: bool = False) -> str | None:
    text = " ".join(value.split()).strip(" \"'“”‘’;,")
    # In running text a final period usually ends the sentence; keep it
    # when the name is an abbreviation such as "Example S.A.".
    if prose and text.endswith(".") and "." not in text[:-1]:
        text = text[:-1].rstrip()
    if not 2 <= len(text) <= 120 or sum(character.isalpha() for character in text) < 2:
        return None
    if _DATE_OR_AMOUNT.fullmatch(text) or _ISIN.fullmatch(text) or _HEADER.fullmatch(text.casefold()):
        return None
    return text


class EntityInventoryBuilder:
    def __init__(self, connection: sqlite3.Connection, documents: Sequence[InventoryDocument]) -> None:
        self._connection = connection
        self._documents = {document.extraction_id: document for document in documents}
        self._entities: dict[str, _Entity] = {}
        self._documents_with_labels: set[str] = set()
        self._partial_documents: set[str] = set()
        self._cells_scanned = 0
        self._relations: list[Relation] = []
        # Label/value pairs above tables, and subfund values per table, by sheet.
        self._section_labels: dict[tuple[str, str, object, str], list[tuple[int, str, str, str, str, str]]] = {}
        self._table_values: dict[tuple[str, str, object, str, int], list[tuple[int, str, str, str]]] = {}

    def build(self, requested_roles: frozenset[str], *, relationships: bool = False) -> InventoryResult:
        extraction_ids = list(self._documents)
        search = DocumentSearchIndex.search(
            self._connection, extraction_ids, [*_LABEL_ROLES, "sub-fund of", "subfund of"],
            per_document_limit=MAX_HITS_PER_DOCUMENT, total_limit=MAX_TOTAL_HITS,
        )
        self._partial_documents |= set(search.capped_document_ids)
        if search.truncated and not search.capped_document_ids:
            # The total cap was reached: documents not seen at all are partial.
            seen = {hit.document_id for hit in search.hits}
            self._partial_documents |= {document.document_id for document in self._documents.values()} - seen
        headers: dict[tuple[str, str, object, str, int], dict[int, tuple[str, str, str]]] = {}
        for hit in search.hits:
            if hit.extraction_id not in self._documents:
                continue
            if self._label_pair(hit):
                continue
            header = _HEADER.fullmatch(" ".join(hit.text.casefold().split()))
            if header and hit.sheet_name is not None and hit.row_number is not None and hit.column_number is not None:
                table = (hit.extraction_id, hit.source_kind, hit.worksheet_index, hit.sheet_name, hit.row_number)
                headers.setdefault(table, {})[hit.column_number] = (
                    _LABEL_ROLES[header.group("label")], " ".join(hit.text.split()), hit.source_reference,
                )
                self._documents_with_labels.add(hit.document_id)
                continue
            self._label_lines(hit)
        for table, columns in headers.items():
            self._table_rows(table, columns)
        self._section_relations(headers)
        self._ai_proposals()
        result = self._result(requested_roles)
        return self._relationship_result(result) if relationships else result

    # -- evidence sources ----------------------------------------------

    def _mention(self, extraction_id: str, reference: str, name: str, role: str, basis: str, status: str) -> _Entity | None:
        document = self._documents[extraction_id]
        evidence = AIAnalysisService._source_evidence(self._connection, extraction_id, reference)
        if evidence is None:
            return None
        canonical_reference, source_text = evidence
        key = entity_key(name)
        if not key:
            return None
        entity = self._entities.setdefault(key, _Entity(name))
        mention = Mention(
            document.document_id, extraction_id, document.filename, canonical_reference,
            AIAnalysisService._evidence_excerpt(source_text), role, basis, status,
        )
        # Repeated mentions of one entity in one source location add nothing.
        if not any(
            (existing.document_id, existing.source_reference, existing.role) == (mention.document_id, mention.source_reference, mention.role)
            for existing in entity.mentions
        ):
            entity.mentions.append(mention)
        return entity

    def _label_lines(self, hit: SearchHit) -> None:
        for line in hit.text.splitlines() or [hit.text]:
            match = _LABEL_LINE.match(line)
            if match is None:
                continue
            value = _clean_value(match.group("value"), prose=True)
            if value is None:
                continue
            label = " ".join(match.group("label").casefold().split())
            self._documents_with_labels.add(hit.document_id)
            self._mention(
                hit.extraction_id, hit.source_reference, value, _LABEL_ROLES[label],
                f"labelled “{match.group('label').strip()}:”", "document_label",
            )
        for match in _SUBFUND_OF.finditer(hit.text):
            child, parent = _clean_value(match.group("child"), prose=True), _clean_value(match.group("parent"), prose=True)
            if child is None or parent is None:
                continue
            self._documents_with_labels.add(hit.document_id)
            child_entity = self._mention(hit.extraction_id, hit.source_reference, child, ROLE_SUBFUND, "described as a sub-fund", "document_label")
            parent_entity = self._mention(hit.extraction_id, hit.source_reference, parent, ROLE_VEHICLE, "named as the parent of a sub-fund", "document_label")
            if child_entity is not None and parent_entity is not None:
                child_entity.related[(ROLE_VEHICLE, entity_key(parent))] = parent
                parent_entity.related[(ROLE_SUBFUND, entity_key(child))] = child
                reference = hit.source_reference
                document = self._documents[hit.extraction_id]
                self._relations.append(Relation(
                    document.document_id, document.filename, child, ROLE_SUBFUND, reference, parent, ROLE_VEHICLE,
                    reference, "explicit_statement", f"the source states “{' '.join(match.group(0).split())}”",
                ))

    def _table_rows(self, table: tuple[str, str, object, str, int], columns: Mapping[int, tuple[str, str, str]]) -> None:
        """Read values below explicit role headings; same-row values are associated."""
        extraction_id, content_kind, worksheet_index, sheet_name, header_row = table
        document_id = self._documents[extraction_id].document_id
        remaining = MAX_TABLE_CELLS - self._cells_scanned
        if remaining <= 0:
            self._partial_documents.add(document_id)
            return
        column_numbers = sorted(columns)
        placeholders = ",".join("?" for _ in column_numbers)
        rows = self._connection.execute(
            f"SELECT row_number, column_number, source_reference, value_text FROM extracted_content "
            f"WHERE extraction_id = ? AND content_kind = ? AND worksheet_index IS ? AND sheet_name = ? "
            f"AND row_number > ? AND row_number <= ? AND column_number IN ({placeholders}) AND is_empty = 0 "
            f"ORDER BY row_number, column_number LIMIT ?",
            (extraction_id, content_kind, worksheet_index, sheet_name, header_row, header_row + MAX_TABLE_ROWS,
             *column_numbers, remaining + 1),
        ).fetchall()
        self._cells_scanned += len(rows)
        if len(rows) > remaining:
            self._partial_documents.add(document_id)
            rows = rows[:remaining]
        ended: set[int] = set()
        by_row: dict[int, list[tuple[str, str, str, str, str]]] = {}
        for row in rows:
            column = int(row["column_number"])
            text = str(row["value_text"] or "")
            if column in ended:
                continue
            if _HEADER.fullmatch(" ".join(text.casefold().split())):
                ended.add(column)  # a new table starts in this column
                continue
            value = _clean_value(text)
            if value is None:
                continue
            role, header_text, header_reference = columns[column]
            by_row.setdefault(int(row["row_number"]), []).append((value, role, str(row["source_reference"]), f"column heading “{header_text}” ({header_reference})", header_text.casefold()))
        filename = self._documents[extraction_id].filename
        self._table_values[table] = [
            (row_number, value, role, reference)
            for row_number, values in by_row.items() for value, role, reference, _basis, _heading in values
        ]
        for row_number, values in by_row.items():
            entities = []
            for value, role, reference, basis, heading in values:
                entity = self._mention(extraction_id, reference, value, role, basis, "document_label")
                if entity is not None:
                    entities.append((entity, role, value, reference, heading))
            for entity, _role, value, _reference, _heading in entities:
                for other, other_role, other_value, _other_reference, _other_heading in entities:
                    if other is not entity:
                        entity.related[(other_role, entity_key(other_value))] = other_value
            for index, (_entity, role, value, reference, heading) in enumerate(entities):
                for _other, other_role, other_value, other_reference, other_heading in entities[index + 1:]:
                    explicit = (role == ROLE_SUBFUND and other_heading in _RELATION_HEADINGS) or (
                        other_role == ROLE_SUBFUND and heading in _RELATION_HEADINGS
                    )
                    self._relations.append(Relation(
                        document_id, filename, value, role, reference, other_value, other_role, other_reference,
                        "explicit_heading" if explicit else "same_row",
                        (f"row {row_number}: the column heading names the relationship" if explicit
                         else f"row {row_number}: both names appear in the same row under their column headings"),
                    ))

    def _label_pair(self, hit: SearchHit) -> bool:
        """Read a spreadsheet label cell whose value is the next cell of a two-cell row.

        Only a row holding exactly the label and one value qualifies, so a
        header row ("Investor | Subfund | ISIN") is never read as a pair.
        """
        if hit.sheet_name is None or hit.row_number is None or hit.column_number is None:
            return False
        normalized = " ".join(hit.text.casefold().split())
        match = _PAIR_LABEL.fullmatch(normalized)
        if match is None:
            return False
        parts = [part.strip() for part in re.split(r"/|\bor\b|&", match.group("labels")) if part.strip()]
        specific = {_LABEL_ROLES[part] for part in parts if part in _LABEL_ROLES} - {ROLE_UNKNOWN, ROLE_OTHER}
        if len(specific) != 1:
            return False
        row = self._connection.execute(
            "SELECT column_number, value_text, source_reference FROM extracted_content WHERE extraction_id = ? "
            "AND content_kind = ? AND worksheet_index IS ? AND sheet_name = ? AND row_number = ? AND is_empty = 0 "
            "ORDER BY column_number LIMIT 3",
            (hit.extraction_id, hit.source_kind, hit.worksheet_index, hit.sheet_name, hit.row_number),
        ).fetchall()
        if len(row) != 2 or int(row[0]["column_number"]) != hit.column_number:
            return False
        raw = " ".join(str(row[1]["value_text"] or "").split())
        value = _clean_value(raw)
        if value is None or raw.casefold().rstrip(":") in _HEADING_WORDS:
            return False
        (role,) = specific
        label_text = " ".join(hit.text.split()).rstrip(":")
        value_reference = str(row[1]["source_reference"])
        if self._mention(
            hit.extraction_id, value_reference, value, role,
            f"labelled “{label_text}” in the adjacent cell {hit.source_reference}", "document_label",
        ) is None:
            return False
        self._documents_with_labels.add(hit.document_id)
        self._section_labels.setdefault((hit.extraction_id, hit.source_kind, hit.worksheet_index, hit.sheet_name), []).append(
            (hit.row_number, role, value, value_reference, label_text, hit.source_reference),
        )
        return True

    def _section_relations(self, headers: Mapping[tuple[str, str, object, str, int], Mapping[int, tuple[str, str, str]]]) -> None:
        """Associate an investor label with the subfund rows of the table directly below it.

        The label must precede that table with no other table header in
        between, and the table must have no investor column of its own.  The
        result is a documented association on the sheet, not legal ownership.
        """
        header_rows: dict[tuple[str, str, object, str], list[int]] = {}
        for extraction_id, content_kind, worksheet_index, sheet_name, header_row in headers:
            header_rows.setdefault((extraction_id, content_kind, worksheet_index, sheet_name), []).append(header_row)
        for table, columns in headers.items():
            extraction_id, content_kind, worksheet_index, sheet_name, header_row = table
            if any(role in _INVESTOR_SIDE for role, _text, _reference in columns.values()):
                continue
            sheet = (extraction_id, content_kind, worksheet_index, sheet_name)
            labels = [label for label in self._section_labels.get(sheet, []) if label[0] < header_row and label[1] in _INVESTOR_SIDE]
            if not labels:
                continue
            label_row, label_role, investor, investor_reference, label_text, label_reference = max(labels)
            if any(label_row < other < header_row for other in header_rows[sheet]):
                continue
            document = self._documents[extraction_id]
            investor_entity = self._entities.get(entity_key(investor))
            for _row_number, value, role, reference in self._table_values.get(table, []):
                if role not in _HOLDING_SIDE:
                    continue
                subject = self._entities.get(entity_key(value))
                if subject is not None:
                    subject.related[(label_role, entity_key(investor))] = investor
                if investor_entity is not None:
                    investor_entity.related[(role, entity_key(value))] = value
                self._relations.append(Relation(
                    document.document_id, document.filename, value, role, reference, investor, label_role, investor_reference,
                    "document_section",
                    f"the sheet labels “{label_text}” ({label_reference}) above the table headed in row {header_row}, "
                    "which lists this subfund",
                ))

    def _ai_proposals(self) -> None:
        """Reuse evidence-validated AI review names for the current extraction."""
        extraction_ids = list(self._documents)
        placeholders = ",".join("?" for _ in extraction_ids)
        rows = self._connection.execute(
            f"SELECT j.extraction_id, p.proposal_type, p.field_name, p.value_json, p.source_reference "
            f"FROM ai_proposals p JOIN ai_processing_jobs j ON j.id = p.job_id "
            f"WHERE p.validation_status = 'validated' AND p.is_unknown = 0 AND j.status = 'completed' "
            f"AND p.proposal_type IN ('investor_legal_entity', 'investment_vehicle_subfund') "
            f"AND j.extraction_id IN ({placeholders}) "
            f"AND j.id = (SELECT newer.id FROM ai_processing_jobs newer WHERE newer.source_document_id = j.source_document_id "
            f"AND newer.extraction_id = j.extraction_id AND newer.status = 'completed' ORDER BY newer.created_at DESC, newer.id DESC LIMIT 1)",
            extraction_ids,
        ).fetchall()
        for row in rows:
            field_name = str(row["field_name"]).casefold()
            if "name" not in field_name:
                continue
            try:
                value = json.loads(row["value_json"]) if row["value_json"] else None
            except json.JSONDecodeError:
                continue
            name = _clean_value(value) if isinstance(value, str) else None
            if name is None:
                continue
            if row["proposal_type"] == "investor_legal_entity":
                role = ROLE_INVESTOR_OR_ENTITY
            else:
                role = ROLE_SUBFUND if "sub" in field_name or "teilfonds" in field_name else ROLE_VEHICLE_OR_SUBFUND
            self._mention(
                str(row["extraction_id"]), str(row["source_reference"]), name, role,
                f"earlier AI review finding ({row['proposal_type']}.{row['field_name']})", "provisional_ai_finding",
            )

    # -- result --------------------------------------------------------

    @staticmethod
    def _conflict(roles: Sequence[str]) -> bool:
        specific = sorted(set(roles) & _SPECIFIC_ROLES)
        if len(specific) < 2:
            return False
        return not all(frozenset({first, second}) in _COMPATIBLE for index, first in enumerate(specific) for second in specific[index + 1:])

    def _result(self, requested_roles: frozenset[str]) -> InventoryResult:
        listed: list[_Entity] = []
        other_counts: dict[str, int] = {}
        for entity in self._entities.values():
            roles = entity.roles
            if set(roles) & requested_roles:
                listed.append(entity)
            else:
                for role in roles:
                    other_counts[role] = other_counts.get(role, 0) + 1
        role_order = list(ROLE_LABELS)
        listed.sort(key=lambda entity: (min(role_order.index(role) for role in entity.roles), entity.name.casefold()))
        omitted = max(len(listed) - MAX_ENTITIES, 0)
        listed = listed[:MAX_ENTITIES]

        entities_payload: list[dict[str, object]] = []
        citations: list[tuple[str, str, str, str]] = []
        evidence_documents: set[str] = set()
        role_counts: dict[str, int] = {}
        conflicts = 0
        for entity in listed:
            roles = entity.roles
            for role in roles:
                role_counts[role] = role_counts.get(role, 0) + 1
            conflict = self._conflict(roles)
            conflicts += int(conflict)
            uncertainty: list[str] = []
            if conflict:
                by_role = {role: sorted({mention.filename for mention in entity.mentions if mention.role == role}) for role in roles}
                uncertainty.append("Conflicting role evidence: " + "; ".join(
                    f"{ROLE_LABELS[role]} in {', '.join(files)}" for role, files in by_role.items()
                ) + ". The role is unresolved.")
            if any(mention.status == "provisional_ai_finding" for mention in entity.mentions):
                uncertainty.append("Includes an earlier provisional AI review finding.")
            if set(roles) <= {ROLE_INVESTOR_OR_ENTITY, ROLE_VEHICLE_OR_SUBFUND, ROLE_UNKNOWN}:
                uncertainty.append("The source does not state a narrower role.")
            documents = sorted({mention.document_id for mention in entity.mentions})
            evidence_documents |= set(documents)
            # Show evidence from as many different documents as possible.
            evidence: list[Mention] = []
            for document_id in documents:
                evidence.extend([mention for mention in entity.mentions if mention.document_id == document_id][:1])
            evidence.extend(mention for mention in entity.mentions if mention not in evidence)
            evidence = evidence[:MAX_EVIDENCE_PER_ENTITY]
            for mention in evidence:
                if len(citations) < MAX_CITATIONS:
                    citation = (mention.document_id, mention.extraction_id, mention.source_reference, mention.filename)
                    if citation not in citations:
                        citations.append(citation)
            entities_payload.append({
                "name": entity.name,
                "roles": roles,
                "related": [
                    {"name": name, "role": role}
                    for (role, _key), name in sorted(entity.related.items(), key=lambda item: (item[0][0], item[1].casefold()))
                ][:8],
                "evidence": [{
                    "documentId": mention.document_id, "filename": mention.filename,
                    "sourceReference": mention.source_reference, "evidenceSnippet": mention.snippet,
                    "role": mention.role, "basis": mention.basis, "status": mention.status,
                } for mention in evidence],
                "documentCount": len(documents),
                "mentionCount": len(entity.mentions),
                "uncertainty": " ".join(uncertainty) or None,
            })

        eligible = len(self._documents)
        partial = bool(self._partial_documents)
        coverage = {
            "eligibleDocuments": eligible,
            "searchedDocuments": eligible,
            "documentsWithRoleLabels": len(self._documents_with_labels),
            "documentsWithListedEntities": len(evidence_documents),
            "partial": partial,
            "partialDocuments": [
                {"documentId": document.document_id, "filename": document.filename}
                for document in self._documents.values() if document.document_id in self._partial_documents
            ],
            "entitiesOmitted": omitted,
        }
        payload = {
            "requestedRoles": sorted(requested_roles),
            "entities": entities_payload,
            "otherRoleCounts": dict(sorted(other_counts.items())),
            "coverage": coverage,
            "relationships": [],
        }
        return InventoryResult(
            answer=self._answer(role_counts, other_counts, coverage, conflicts, len(entities_payload)),
            payload=payload, citations=tuple(citations), evidence_document_ids=frozenset(evidence_documents), partial=partial,
        )

    def _relationship_result(self, inventory: InventoryResult) -> InventoryResult:
        """Investor/vehicle/subfund pairs, each with the exact cell for both names."""
        holding_side = {ROLE_SUBFUND, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND}
        investor_side = {ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY}
        pairs: list[Relation] = []
        for relation in self._relations:
            roles = {relation.subject_role, relation.related_role}
            if roles & holding_side and roles & investor_side or (relation.kind != "same_row" and ROLE_SUBFUND in roles):
                # Present the subfund/vehicle first, then its counterpart.
                if relation.subject_role not in holding_side or (relation.subject_role == ROLE_VEHICLE and relation.related_role == ROLE_SUBFUND):
                    relation = Relation(
                        relation.document_id, relation.filename, relation.related, relation.related_role,
                        relation.related_reference, relation.subject, relation.subject_role, relation.subject_reference,
                        relation.kind, relation.basis,
                    )
                pairs.append(relation)
        unique = list(dict.fromkeys(pairs))[:MAX_RELATIONSHIPS]
        citations: list[tuple[str, str, str, str]] = []
        extraction_by_document = {document.document_id: extraction for extraction, document in self._documents.items()}
        for relation in unique:
            for reference in (relation.subject_reference, relation.related_reference):
                citation = (relation.document_id, extraction_by_document[relation.document_id], reference, relation.filename)
                if citation not in citations and len(citations) < MAX_CITATIONS:
                    citations.append(citation)
        explicit = sum(relation.kind in {"explicit_statement", "explicit_heading"} for relation in unique)
        section = sum(relation.kind == "document_section" for relation in unique)
        documents = sorted({relation.filename for relation in unique})
        if not unique:
            answer = (
                f"No investor–subfund or vehicle–subfund pairing with labelled names was found in the {len(self._documents)} "
                "searchable document(s) used. Names are paired only when both appear under explicit role headings or in an "
                "explicit statement; nothing is inferred from name similarity."
            )
        else:
            answer = (
                f"Found {len(unique)} pairing(s) in {', '.join(documents)}. {explicit} rest on an explicit statement or "
                f"relationship heading; {section} on an investor label above the table that lists the subfunds; "
                f"{len(unique) - explicit - section} are row-level associations only. These are documented associations in "
                "the source layout. None of them by itself proves ownership, separate legal personality, a legal "
                "relationship, claimant status, or invoicing responsibility. Each pairing shows the exact cell of both names."
            )
        if inventory.partial:
            answer += " Coverage is partial: at least one document exceeded the local scanning limit."
        payload = dict(inventory.payload)
        payload["relationships"] = [{
            "documentId": relation.document_id, "filename": relation.filename,
            "subject": relation.subject, "subjectRole": relation.subject_role, "subjectReference": relation.subject_reference,
            "related": relation.related, "relatedRole": relation.related_role, "relatedReference": relation.related_reference,
            "kind": relation.kind, "basis": relation.basis,
        } for relation in unique]
        return InventoryResult(
            answer=answer, payload=payload, citations=tuple(citations),
            evidence_document_ids=frozenset(relation.document_id for relation in unique), partial=inventory.partial,
        )

    @staticmethod
    def _answer(role_counts: Mapping[str, int], other_counts: Mapping[str, int], coverage: Mapping[str, object], conflicts: int, listed: int) -> str:
        eligible = coverage["eligibleDocuments"]
        if listed == 0:
            text = (
                f"No explicitly labelled entities of the requested kind were found after searching all {eligible} searchable "
                f"document{'s' if eligible != 1 else ''} in scope. Organisation names without a role label (such as a column "
                "heading or “Investor:” label) are not classified. Ask about a specific name or document, or run AI review "
                "on the relevant documents to add provisional findings."
            )
        else:
            counts = ", ".join(f"{count} × {ROLE_LABELS[role].lower()}" for role, count in role_counts.items())
            text = (
                f"Searched all {eligible} searchable document{'s' if eligible != 1 else ''} in scope; {listed} "
                f"entit{'ies' if listed != 1 else 'y'} with the requested roles appear in {coverage['documentsWithListedEntities']} "
                f"of them ({counts}). Roles come only from document labels, column headings, explicit sub-fund wording, "
                "or earlier evidence-validated AI review findings, and remain provisional. Appearing in the same row is "
                "shown as an association only; it does not establish ownership, legal personality, claimant status, or "
                "invoicing relationships."
            )
        if conflicts:
            text += f" {conflicts} entit{'ies have' if conflicts != 1 else 'y has'} conflicting role evidence and remain unresolved."
        if other_counts:
            text += " Also labelled but not requested: " + ", ".join(
                f"{count} × {ROLE_LABELS[role].lower()}" for role, count in other_counts.items()
            ) + "."
        if coverage["partial"]:
            text += (
                f" Coverage is partial: {len(coverage['partialDocuments'])} document(s) exceeded the local scanning limit, "  # type: ignore[arg-type]
                "so this list may be incomplete for them. Ask about those documents with Selected documents only."
            )
        if coverage["entitiesOmitted"]:
            text += f" {coverage['entitiesOmitted']} further entities were found but not listed; narrow the question to see them."
        return text
