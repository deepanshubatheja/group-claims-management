"""Storage initialization and non-sensitive status reporting."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .config import StorageConfigurationError, StoragePaths
from .database import Database, DatabaseConnectionError
from .migrations import MigrationError, MigrationRunner
from .vault import FileVaultService, VaultStorageError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageStatus:
    status: str
    database_status: str
    schema_version: int | None
    directories_available: bool

    def as_protocol_result(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "databaseStatus": self.database_status,
            "schemaVersion": self.schema_version,
            "directoriesAvailable": self.directories_available,
        }


class StorageService:
    """Initializes the local storage boundary once before it can report readiness."""

    def __init__(self, configured_root: str | None) -> None:
        self._configured_root = configured_root
        self.paths: StoragePaths | None = None
        self.database: Database | None = None
        self.vault: FileVaultService | None = None
        self._status = StorageStatus("initializing", "unavailable", None, False)

    def initialize(self) -> StorageStatus:
        if self._configured_root is None:
            self._status = StorageStatus("error", "unavailable", None, False)
            return self._status

        try:
            paths = StoragePaths.from_root(self._configured_root)
            paths.initialize_directories()
            database = Database(paths.database_file)
            schema_version = MigrationRunner(database).initialize()
            vault = FileVaultService(paths, database)
            vault.recover_incomplete_imports()
            self.paths = paths
            self.database = database
            self.vault = vault
            self._status = StorageStatus("ready", "connected", schema_version, True)
        except (StorageConfigurationError, DatabaseConnectionError, MigrationError, VaultStorageError):
            # This stays in backend-only operational diagnostics; no source
            # document content or provider payload is included.
            logger.exception("Local storage initialization failed.")
            self.paths = None
            self.database = None
            self.vault = None
            self._status = StorageStatus("error", "unavailable", None, False)

        return self._status

    def status(self) -> Mapping[str, object]:
        return self._status.as_protocol_result()
