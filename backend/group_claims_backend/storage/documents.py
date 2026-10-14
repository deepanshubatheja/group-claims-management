"""SQLite persistence for immutable managed source-document metadata."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentRecord:
    id: str
    original_filename: str
    managed_relative_path: str
    content_sha256: str
    file_size_bytes: int
    imported_at: str


class DocumentMetadataRepository:
    """Narrow repository for source-document metadata only."""

    def find_by_hash(self, connection: sqlite3.Connection, content_sha256: str) -> DocumentRecord | None:
        row = connection.execute(
            """
            SELECT id, original_filename, managed_relative_path, content_sha256,
                   file_size_bytes, imported_at
            FROM source_documents
            WHERE content_sha256 = ?
            """,
            (content_sha256,),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def insert(self, connection: sqlite3.Connection, record: DocumentRecord) -> None:
        connection.execute(
            """
            INSERT INTO source_documents (
                id, original_filename, managed_relative_path, content_sha256,
                file_size_bytes, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.original_filename,
                record.managed_relative_path,
                record.content_sha256,
                record.file_size_bytes,
                record.imported_at,
            ),
        )

    def managed_relative_paths(self, connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute("SELECT managed_relative_path FROM source_documents").fetchall()
        return {str(row["managed_relative_path"]) for row in rows}

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            id=str(row["id"]),
            original_filename=str(row["original_filename"]),
            managed_relative_path=str(row["managed_relative_path"]),
            content_sha256=str(row["content_sha256"]),
            file_size_bytes=int(row["file_size_bytes"]),
            imported_at=str(row["imported_at"]),
        )
