"""SQLite storage and file-vault tests using temporary synthetic files only."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from group_claims_backend.storage.config import StorageConfigurationError, StoragePaths
from group_claims_backend.storage.documents import DocumentMetadataRepository
from group_claims_backend.storage.database import Database
from group_claims_backend.storage.migrations import MIGRATIONS, Migration, MigrationRunner
from group_claims_backend.storage.service import StorageService
from group_claims_backend.storage.vault import FileVaultService, VaultSourceError, VaultStorageError


class FailingDocumentRepository(DocumentMetadataRepository):
    def insert(self, connection: sqlite3.Connection, record: object) -> None:
        raise sqlite3.OperationalError("synthetic metadata failure")


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "app-data"
        self.service = StorageService(str(self.root))
        self.status = self.service.initialize()
        self.assertEqual(self.status.status, "ready")
        self.assertIsNotNone(self.service.paths)
        self.assertIsNotNone(self.service.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @property
    def paths(self) -> StoragePaths:
        assert self.service.paths is not None
        return self.service.paths

    def vault(self, repository: DocumentMetadataRepository | None = None) -> FileVaultService:
        assert self.service.database is not None
        return FileVaultService(self.paths, self.service.database, repository)

    def source_file(self, name: str, contents: bytes) -> Path:
        source = Path(self.temporary_directory.name) / name
        source.write_bytes(contents)
        return source

    def test_first_run_creates_expected_directories_and_schema(self) -> None:
        self.assertEqual(self.status.schema_version, MIGRATIONS[-1].version)
        self.assertTrue(all(directory.is_dir() for directory in (
            self.paths.database,
            self.paths.documents,
            self.paths.exports,
            self.paths.backups,
            self.paths.logs,
        )))
        self.assertTrue(self.paths.database_file.is_file())

    def test_repeated_initialization_preserves_existing_data(self) -> None:
        assert self.service.database is not None
        with self.service.database.transaction() as connection:
            connection.execute(
                "INSERT INTO legal_entities (id, display_name, entity_type, created_at) VALUES (?, ?, ?, ?)",
                ("entity-1", "Example Entity", "organization", "2026-10-14T00:00:00+00:00"),
            )

        restarted = StorageService(str(self.root))
        status = restarted.initialize()
        self.assertEqual(status.status, "ready")
        assert restarted.database is not None
        with restarted.database.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM legal_entities").fetchone()[0]
        self.assertEqual(count, 1)

    def test_foreign_keys_are_enforced(self) -> None:
        assert self.service.database is not None
        with self.assertRaises(sqlite3.IntegrityError):
            with self.service.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO investors (id, legal_entity_id, created_at) VALUES (?, ?, ?)",
                    ("investor-1", "missing-entity", "2026-10-14T00:00:00+00:00"),
                )

    def test_transaction_rolls_back_on_failure(self) -> None:
        assert self.service.database is not None
        with self.assertRaises(RuntimeError):
            with self.service.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO legal_entities (id, display_name, entity_type, created_at) VALUES (?, ?, ?, ?)",
                    ("rollback-entity", "Rollback", "organization", "2026-10-14T00:00:00+00:00"),
                )
                raise RuntimeError("synthetic rollback")

        with self.service.database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM legal_entities WHERE id = ?", ("rollback-entity",)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_schema_migration_version_tracking_and_failure_rollback(self) -> None:
        assert self.service.database is not None
        with self.service.database.connection() as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertEqual(version, MIGRATIONS[-1].version)

        failed_database = StorageService(str(Path(self.temporary_directory.name) / "failed-migration"))
        self.assertEqual(failed_database.initialize().status, "ready")
        assert failed_database.database is not None

        def invalid_migration(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE partial_migration (id INTEGER PRIMARY KEY)")
            connection.execute("THIS IS NOT VALID SQL")

        next_version = MIGRATIONS[-1].version + 1
        with self.assertRaises(Exception):
            MigrationRunner(failed_database.database, (Migration(next_version, invalid_migration),)).initialize()
        with failed_database.database.connection() as connection:
            partial = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'partial_migration'"
            ).fetchone()
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertIsNone(partial)
        self.assertEqual(version, MIGRATIONS[-1].version)

    def test_reconciliation_migration_resumes_when_schema_exists_without_its_version_record(self) -> None:
        """An interrupted pre-version-record upgrade must not block app startup."""
        assert self.service.database is not None
        with self.service.database.transaction() as connection:
            connection.execute(
                "INSERT INTO legal_entities (id, display_name, entity_type, created_at) VALUES (?, ?, ?, ?)",
                ("preexisting-entity", "Existing Entity", "organization", "2026-10-22T00:00:00+00:00"),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version = ?", (MIGRATIONS[-1].version,))

        restarted = StorageService(str(self.root))
        status = restarted.initialize()

        self.assertEqual(status.status, "ready")
        self.assertEqual(status.schema_version, MIGRATIONS[-1].version)
        assert restarted.database is not None
        with restarted.database.connection() as connection:
            entity = connection.execute(
                "SELECT display_name FROM legal_entities WHERE id = ?", ("preexisting-entity",)
            ).fetchone()
        self.assertEqual(entity["display_name"], "Existing Entity")

    def test_search_index_repair_migration_restores_missing_derived_tables(self) -> None:
        """A v10 version record without its derived tables must be repairable."""
        legacy_root = Path(self.temporary_directory.name) / "missing-search-index"
        legacy_paths = StoragePaths.from_root(legacy_root)
        legacy_paths.initialize_directories()
        legacy_database = Database(legacy_paths.database_file)
        # This models the affected persisted state: migrations through v11
        # are recorded, but the interrupted v10 cache schema is absent.
        # Keep the fixture at the historical v11 boundary: v12 repairs the
        # derived search index and v13 adds reconciliation provenance.
        self.assertEqual(MigrationRunner(legacy_database, MIGRATIONS[:11]).initialize(), 11)
        with legacy_database.transaction() as connection:
            connection.execute("DROP TABLE document_search_fts")
            connection.execute("DROP TABLE document_search_index")

        self.assertEqual(MigrationRunner(legacy_database).initialize(), MIGRATIONS[-1].version)
        with legacy_database.connection() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                )
            }
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertIn("document_search_fts", tables)
        self.assertIn("document_search_index", tables)
        self.assertEqual(version, MIGRATIONS[-1].version)

    def test_existing_version_one_database_upgrades_without_resetting_data(self) -> None:
        legacy_root = Path(self.temporary_directory.name) / "legacy-app-data"
        legacy_paths = StoragePaths.from_root(legacy_root)
        legacy_paths.initialize_directories()
        legacy_database = Database(legacy_paths.database_file)
        self.assertEqual(MigrationRunner(legacy_database, MIGRATIONS[:1]).initialize(), 1)
        with legacy_database.transaction() as connection:
            connection.execute(
                "INSERT INTO legal_entities (id, display_name, entity_type, created_at) VALUES (?, ?, ?, ?)",
                ("legacy-entity", "Legacy Entity", "organization", "2026-10-14T00:00:00+00:00"),
            )

        self.assertEqual(MigrationRunner(legacy_database).initialize(), MIGRATIONS[-1].version)
        with legacy_database.connection() as connection:
            entity = connection.execute(
                "SELECT display_name FROM legal_entities WHERE id = ?", ("legacy-entity",)
            ).fetchone()
            column_names = {
                row["name"] for row in connection.execute("PRAGMA table_info(investment_vehicles)")
            }
        self.assertEqual(entity["display_name"], "Legacy Entity")
        self.assertIn("vehicle_type", column_names)

    def test_corrupt_database_is_not_replaced(self) -> None:
        corrupt_root = Path(self.temporary_directory.name) / "corrupt-app-data"
        database_directory = corrupt_root / "database"
        database_directory.mkdir(parents=True)
        database_file = database_directory / "claims.sqlite"
        original_bytes = b"not a sqlite database"
        database_file.write_bytes(original_bytes)

        status = StorageService(str(corrupt_root)).initialize()
        self.assertEqual(status.status, "error")
        self.assertEqual(database_file.read_bytes(), original_bytes)

    def test_storage_root_and_managed_document_paths_reject_unsafe_values(self) -> None:
        with self.assertRaises(StorageConfigurationError):
            StoragePaths.from_root("relative-storage")
        with self.assertRaises(StorageConfigurationError):
            self.paths.document_path_for("../claims.sqlite")

    def test_successful_vault_import_preserves_source_and_metadata(self) -> None:
        contents = b"synthetic legal document\nline two\n"
        source = self.source_file("source report.pdf", contents)
        before = source.read_bytes()

        result = self.vault().import_file(source)

        self.assertFalse(result.duplicate)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(result.document.original_filename, "source report.pdf")
        self.assertEqual(result.document.content_sha256, hashlib.sha256(contents).hexdigest())
        self.assertEqual(result.document.file_size_bytes, len(contents))
        managed = self.paths.root / result.document.managed_relative_path
        self.assertEqual(managed.read_bytes(), contents)
        self.assertEqual(managed.parent, self.paths.documents)

    def test_duplicate_import_reuses_the_existing_document_record(self) -> None:
        source = self.source_file("duplicate.csv", b"header,value\nexample,1\n")
        first = self.vault().import_file(source)
        second = self.vault().import_file(source)

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.document.id, first.document.id)
        self.assertEqual(len(list(self.paths.documents.iterdir())), 1)

    def test_missing_unreadable_and_symlink_sources_are_rejected(self) -> None:
        with self.assertRaises(VaultSourceError):
            self.vault().import_file(Path(self.temporary_directory.name) / "missing.pdf")

        unreadable = self.source_file("unreadable.pdf", b"test")
        unreadable.chmod(0)
        try:
            with self.assertRaises(VaultSourceError):
                self.vault().import_file(unreadable)
        finally:
            unreadable.chmod(0o600)

        target = self.source_file("target.pdf", b"test")
        link = Path(self.temporary_directory.name) / "linked.pdf"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("The current filesystem does not support symbolic-link tests.")
        with self.assertRaises(VaultSourceError):
            self.vault().import_file(link)

    def test_database_failure_cleans_up_managed_files(self) -> None:
        source = self.source_file("metadata-failure.pdf", b"synthetic content")
        with self.assertRaises(VaultStorageError):
            self.vault(FailingDocumentRepository()).import_file(source)

        self.assertEqual(list(self.paths.documents.iterdir()), [])

    def test_startup_recovers_only_unreferenced_generated_vault_files(self) -> None:
        generated_orphan = self.paths.documents / "edce70f9-18a4-4f60-bef5-1e17be343ee2"
        interrupted_temporary = self.paths.documents / ".c97da555-0b81-4bbb-a4b7-6c270486dcef.tmp"
        unexpected_file = self.paths.documents / "do-not-delete.txt"
        generated_orphan.write_bytes(b"orphan")
        interrupted_temporary.write_bytes(b"temporary")
        unexpected_file.write_bytes(b"unmanaged")

        restarted = StorageService(str(self.root))
        self.assertEqual(restarted.initialize().status, "ready")

        self.assertFalse(generated_orphan.exists())
        self.assertFalse(interrupted_temporary.exists())
        self.assertTrue(unexpected_file.exists())

    def test_source_filename_is_metadata_not_a_managed_path(self) -> None:
        source = self.source_file("summary ✓.pdf", b"unicode filename")
        result = self.vault().import_file(source)

        self.assertEqual(result.document.original_filename, "summary ✓.pdf")
        self.assertNotIn(result.document.original_filename, result.document.managed_relative_path)

    def test_concurrent_duplicate_imports_create_one_managed_document(self) -> None:
        source = self.source_file("concurrent.pdf", b"same bytes for both threads")
        vault = self.vault()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _ignored: vault.import_file(source), range(2)))

        self.assertEqual({result.document.id for result in results}, {results[0].document.id})
        self.assertEqual(sum(result.duplicate for result in results), 1)
        self.assertEqual(len(list(self.paths.documents.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
