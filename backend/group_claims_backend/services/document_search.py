"""Derived local full-text search over current deterministic extractions.

The index exists only to choose *which* stored cells or PDF text blocks are
worth considering for a bounded question.  It is never a source of evidence:
callers re-resolve every hit against ``extracted_content`` or
``pdf_text_blocks`` before it can be sent or cited.  Nothing here contacts a
provider.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

# The trigram tokenizer cannot match substrings shorter than three characters.
_MIN_TERM_LENGTH = 3
_MAX_TERMS = 96


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    extraction_id: str
    source_kind: str
    source_reference: str
    text: str
    sheet_name: str | None
    worksheet_index: int | None
    row_number: int | None
    column_number: int | None
    page_number: int | None
    block_index: int | None


@dataclass(frozen=True)
class IndexState:
    item_count: int
    unavailable_page_count: int


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    # True when at least one document had more matches than the bounded
    # per-document or total hit limit allowed into the result.
    truncated: bool
    # Documents whose matches exceeded the per-document limit.
    capped_document_ids: frozenset[str] = frozenset()


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


class DocumentSearchIndex:
    """Lazily indexes one current extraction per document and searches it."""

    @staticmethod
    def ensure_indexed(
        connection: sqlite3.Connection, extractions: Mapping[str, str],
    ) -> dict[str, IndexState]:
        """Index each ``extraction_id -> document_id`` pair not yet indexed.

        Call inside a write transaction.  Index rows for an older extraction
        of the same document are pruned so a reparse never leaves two
        competing versions searchable.
        """
        if not extractions:
            return {}
        extraction_ids = list(extractions)
        states: dict[str, IndexState] = {
            str(row["extraction_id"]): IndexState(int(row["item_count"]), int(row["unavailable_page_count"]))
            for row in connection.execute(
                f"SELECT extraction_id, item_count, unavailable_page_count FROM document_search_index "
                f"WHERE extraction_id IN ({_placeholders(extraction_ids)})",
                extraction_ids,
            ).fetchall()
        }
        document_ids = sorted(set(extractions.values()))
        stale = connection.execute(
            f"SELECT extraction_id FROM document_search_index WHERE source_document_id IN ({_placeholders(document_ids)}) "
            f"AND extraction_id NOT IN ({_placeholders(extraction_ids)})",
            [*document_ids, *extraction_ids],
        ).fetchall()
        for row in stale:
            DocumentSearchIndex._remove_extraction(connection, str(row["extraction_id"]))
        for extraction_id, document_id in extractions.items():
            if extraction_id not in states:
                states[extraction_id] = DocumentSearchIndex._index_extraction(connection, document_id, extraction_id)
        return states

    @staticmethod
    def search(
        connection: sqlite3.Connection,
        extraction_ids: Sequence[str],
        terms: Iterable[str],
        *,
        per_document_limit: int,
        total_limit: int,
    ) -> SearchResult:
        """Return matching sources, ranked per document then interleaved.

        Interleaving by per-document rank keeps one large workbook from
        crowding every other document in the scope out of a bounded result.
        """
        usable = sorted({term for term in terms if len(term) >= _MIN_TERM_LENGTH})[:_MAX_TERMS]
        if not extraction_ids or not usable:
            return SearchResult((), False)
        query = " OR ".join('"' + term.replace('"', '""') + '"' for term in usable)
        rows = connection.execute(
            f"SELECT * FROM ("
            f" SELECT *, ROW_NUMBER() OVER (PARTITION BY source_document_id ORDER BY score, rowid) AS position,"
            f" COUNT(*) OVER (PARTITION BY source_document_id) AS document_matches FROM ("
            f"  SELECT rowid, text, source_document_id, extraction_id, source_kind, source_reference, sheet_name,"
            f"  worksheet_index, row_number, column_number, page_number, block_index,"
            f"  bm25(document_search_fts) AS score FROM document_search_fts"
            f"  WHERE document_search_fts MATCH ? AND extraction_id IN ({_placeholders(extraction_ids)})"
            f" )"
            f") WHERE position <= ? ORDER BY position, source_document_id LIMIT ?",
            [query, *extraction_ids, per_document_limit, total_limit + 1],
        ).fetchall()
        capped = frozenset(
            str(row["source_document_id"]) for row in rows if int(row["document_matches"]) > per_document_limit
        )
        truncated = len(rows) > total_limit or bool(capped)
        hits = tuple(
            SearchHit(
                document_id=str(row["source_document_id"]),
                extraction_id=str(row["extraction_id"]),
                source_kind=str(row["source_kind"]),
                source_reference=str(row["source_reference"]),
                text=str(row["text"] or ""),
                sheet_name=row["sheet_name"] if isinstance(row["sheet_name"], str) else None,
                worksheet_index=row["worksheet_index"] if isinstance(row["worksheet_index"], int) else None,
                row_number=row["row_number"] if isinstance(row["row_number"], int) else None,
                column_number=row["column_number"] if isinstance(row["column_number"], int) else None,
                page_number=row["page_number"] if isinstance(row["page_number"], int) else None,
                block_index=row["block_index"] if isinstance(row["block_index"], int) else None,
            )
            for row in rows[:total_limit]
        )
        return SearchResult(hits, truncated, capped)

    @staticmethod
    def remove_document(connection: sqlite3.Connection, document_id: str) -> None:
        """Delete every derived index row for one removed document."""
        connection.execute("DELETE FROM document_search_fts WHERE source_document_id = ?", (document_id,))
        connection.execute("DELETE FROM document_search_index WHERE source_document_id = ?", (document_id,))

    @staticmethod
    def _remove_extraction(connection: sqlite3.Connection, extraction_id: str) -> None:
        connection.execute("DELETE FROM document_search_fts WHERE extraction_id = ?", (extraction_id,))
        connection.execute("DELETE FROM document_search_index WHERE extraction_id = ?", (extraction_id,))

    @staticmethod
    def _index_extraction(connection: sqlite3.Connection, document_id: str, extraction_id: str) -> IndexState:
        has_pdf_blocks = connection.execute(
            "SELECT 1 FROM pdf_page_extractions WHERE extraction_id = ? LIMIT 1", (extraction_id,),
        ).fetchone() is not None
        # A coordinate-aware PDF attempt also stores a shortened whole-page
        # row.  Index its precise text blocks only, so one passage is not
        # retrieved twice; legacy page-only attempts keep their page rows.
        cells = connection.execute(
            "INSERT INTO document_search_fts (text, source_document_id, extraction_id, source_kind, source_reference, "
            "sheet_name, worksheet_index, row_number, column_number, page_number, block_index) "
            "SELECT COALESCE(value_text, '') || ' ' || COALESCE(formula_text, ''), ?, extraction_id, content_kind, "
            "source_reference, sheet_name, worksheet_index, row_number, column_number, page_number, NULL "
            "FROM extracted_content WHERE extraction_id = ? AND is_empty = 0 AND (content_kind <> 'pdf_page' OR ? = 0)",
            (document_id, extraction_id, int(has_pdf_blocks)),
        ).rowcount
        blocks = connection.execute(
            "INSERT INTO document_search_fts (text, source_document_id, extraction_id, source_kind, source_reference, "
            "sheet_name, worksheet_index, row_number, column_number, page_number, block_index) "
            "SELECT b.text, ?, p.extraction_id, 'pdf_text_block', 'page ' || p.page_number || ' block ' || b.block_index, "
            "NULL, NULL, NULL, NULL, p.page_number, b.block_index "
            "FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
            "WHERE p.extraction_id = ? AND b.text <> ''",
            (document_id, extraction_id),
        ).rowcount
        unavailable_pages = int(connection.execute(
            "SELECT COUNT(*) FROM pdf_page_extractions WHERE extraction_id = ? AND extraction_method = 'unavailable'",
            (extraction_id,),
        ).fetchone()[0])
        state = IndexState(max(cells, 0) + max(blocks, 0), unavailable_pages)
        connection.execute(
            "INSERT INTO document_search_index (extraction_id, source_document_id, item_count, unavailable_page_count, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (extraction_id, document_id, state.item_count, state.unavailable_page_count, datetime.now(UTC).isoformat()),
        )
        return state
