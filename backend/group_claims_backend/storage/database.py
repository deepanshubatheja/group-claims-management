"""Small SQLite connection and transaction manager for local application data."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class DatabaseConnectionError(RuntimeError):
    """Raised when a SQLite database cannot be opened safely."""


class Database:
    """Creates short-lived SQLite connections with required safety pragmas."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=5,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise DatabaseConnectionError("The local database could not be opened.") from error

        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Use an explicit transaction and always roll it back on failure."""

        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
