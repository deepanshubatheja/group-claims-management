"""Safe managed copies of user-authorized source documents."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import StoragePaths
from .database import Database
from .documents import DocumentMetadataRepository, DocumentRecord

_COPY_CHUNK_BYTES = 1024 * 1024


class VaultImportError(RuntimeError):
    """Raised for safe, recoverable document-import failures."""


class VaultSourceError(VaultImportError):
    """Raised when the authorized source file is unsuitable for import."""


class VaultStorageError(VaultImportError):
    """Raised when a managed copy or its metadata cannot be persisted safely."""


class VaultImportResult:
    """Result of a successful or deduplicated immutable document import."""

    def __init__(self, document: DocumentRecord, duplicate: bool) -> None:
        self.document = document
        self.duplicate = duplicate


class FileVaultService:
    """Copies regular files into the vault and records their immutable metadata."""

    def __init__(
        self,
        paths: StoragePaths,
        database: Database,
        repository: DocumentMetadataRepository | None = None,
    ) -> None:
        self._paths = paths
        self._database = database
        self._repository = repository if repository is not None else DocumentMetadataRepository()

    def import_file(self, authorized_source_path: str | Path) -> VaultImportResult:
        """Import one explicitly authorized regular file without changing its source bytes."""

        source = Path(authorized_source_path)
        original_filename = self._validate_source(source)
        temporary_path = self._paths.documents / f".{uuid.uuid4()}.tmp"
        final_path: Path | None = None
        moved_final = False

        try:
            content_sha256, file_size_bytes = self._copy_to_temporary(source, temporary_path)
            document_id = str(uuid.uuid4())
            final_path = self._paths.document_path_for(document_id)
            record = DocumentRecord(
                id=document_id,
                original_filename=original_filename,
                managed_relative_path=f"documents/{document_id}",
                content_sha256=content_sha256,
                file_size_bytes=file_size_bytes,
                imported_at=datetime.now(UTC).isoformat(),
            )

            with self._database.transaction() as connection:
                existing = self._repository.find_by_hash(connection, content_sha256)
                if existing is not None:
                    return VaultImportResult(existing, duplicate=True)

                if final_path.exists() or final_path.is_symlink():
                    raise VaultStorageError("A managed document path unexpectedly exists.")
                os.replace(temporary_path, final_path)
                moved_final = True
                self._set_private_file_permissions(final_path)
                self._repository.insert(connection, record)

            return VaultImportResult(record, duplicate=False)
        except VaultImportError:
            raise
        except OSError as error:
            raise VaultStorageError("The managed document copy could not be created.") from error
        except Exception as error:
            raise VaultStorageError("The document metadata could not be saved.") from error
        finally:
            self._remove_file_if_present(temporary_path)
            if moved_final and final_path is not None:
                try:
                    with self._database.connection() as connection:
                        persisted = self._repository.find_by_hash(connection, content_sha256)
                    if persisted is None:
                        self._remove_file_if_present(final_path)
                except Exception:
                    self._remove_file_if_present(final_path)

    def recover_incomplete_imports(self) -> None:
        """Remove only generated unreferenced vault files left by an interrupted import."""

        try:
            with self._database.connection() as connection:
                known_paths = self._repository.managed_relative_paths(connection)

            for candidate in self._paths.documents.iterdir():
                relative_path = f"documents/{candidate.name}"
                if self._is_temporary_name(candidate.name) or (
                    self._is_generated_document_id(candidate.name) and relative_path not in known_paths
                ):
                    if not (candidate.is_symlink() or candidate.is_file()):
                        raise VaultStorageError("A managed vault entry has an unsafe file type.")
                    candidate.unlink()
        except OSError as error:
            raise VaultStorageError("Interrupted document imports could not be recovered safely.") from error

    @staticmethod
    def _validate_source(source: Path) -> str:
        try:
            source_stat = source.lstat()
        except FileNotFoundError as error:
            raise VaultSourceError("The selected source file does not exist.") from error
        except OSError as error:
            raise VaultSourceError("The selected source file cannot be inspected.") from error

        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise VaultSourceError("The selected source must be a regular file, not a link.")
        if source_stat.st_mode & 0o444 == 0:
            raise VaultSourceError("The selected source file is not readable.")
        if source.name in ("", ".", ".."):
            raise VaultSourceError("The selected source file has an invalid name.")
        return source.name

    @staticmethod
    def _copy_to_temporary(source: Path, destination: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        total_bytes = 0
        source_descriptor: int | None = None

        try:
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            source_descriptor = os.open(source, os.O_RDONLY | no_follow)
            opened_stat = os.fstat(source_descriptor)
            expected_stat = source.lstat()
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_dev != expected_stat.st_dev
                or opened_stat.st_ino != expected_stat.st_ino
            ):
                raise VaultSourceError("The selected source file changed during validation.")

            with os.fdopen(source_descriptor, "rb", closefd=True) as input_file:
                source_descriptor = None
                with destination.open("xb") as output_file:
                    while chunk := input_file.read(_COPY_CHUNK_BYTES):
                        digest.update(chunk)
                        output_file.write(chunk)
                        total_bytes += len(chunk)
                    output_file.flush()
                    os.fsync(output_file.fileno())
        except VaultImportError:
            raise
        except OSError as error:
            raise VaultSourceError("The selected source file cannot be read safely.") from error
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)

        return digest.hexdigest(), total_bytes

    @staticmethod
    def _set_private_file_permissions(path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o600)

    @staticmethod
    def _is_generated_document_id(value: str) -> bool:
        try:
            return str(uuid.UUID(value)) == value
        except ValueError:
            return False

    @classmethod
    def _is_temporary_name(cls, value: str) -> bool:
        return value.startswith(".") and value.endswith(".tmp") and cls._is_generated_document_id(
            value[1:-4]
        )

    @staticmethod
    def _remove_file_if_present(path: Path) -> None:
        try:
            if path.exists() or path.is_symlink():
                if path.is_symlink() or path.is_file():
                    path.unlink()
        except OSError:
            # Cleanup is best-effort; callers still receive the controlled storage error.
            pass
