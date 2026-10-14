"""Safe configuration of the per-user managed storage root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class StorageConfigurationError(RuntimeError):
    """Raised when the trusted Electron-provided storage root is unsafe to use."""


@dataclass(frozen=True)
class StoragePaths:
    """All managed directories rooted at one trusted absolute location."""

    root: Path
    database: Path
    documents: Path
    exports: Path
    backups: Path
    logs: Path

    @property
    def database_file(self) -> Path:
        return self.database / "claims.sqlite"

    @classmethod
    def from_root(cls, configured_root: str | Path) -> "StoragePaths":
        root = Path(configured_root).expanduser()
        if not root.is_absolute() or root == Path(root.anchor):
            raise StorageConfigurationError("Storage root must be a non-root absolute path.")

        # Electron owns this argument. Do not accept a symlink as the final managed root.
        if root.exists() and root.is_symlink():
            raise StorageConfigurationError("Storage root must not be a symbolic link.")

        return cls(
            root=root,
            database=root / "database",
            documents=root / "documents",
            exports=root / "exports",
            backups=root / "backups",
            logs=root / "logs",
        )

    def initialize_directories(self) -> None:
        """Create the managed layout safely and repeatedly without touching user files."""

        self._create_private_directory(self.root)
        for directory in (self.database, self.documents, self.exports, self.backups, self.logs):
            self._create_private_directory(directory)

    @staticmethod
    def _create_private_directory(directory: Path) -> None:
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise StorageConfigurationError("A managed storage path is not a safe directory.")
        else:
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)

        # POSIX permissions are best-effort; Windows ACLs remain platform-managed.
        if os.name != "nt":
            try:
                directory.chmod(0o700)
            except OSError as error:
                raise StorageConfigurationError("Could not secure a managed storage directory.") from error

    def document_path_for(self, document_id: str) -> Path:
        """Return a generated document path, rejecting any traversal-like identifier."""

        if not document_id or any(character in document_id for character in ("/", "\\", "\x00")):
            raise StorageConfigurationError("Managed document identifier is invalid.")

        candidate = self.documents / document_id
        if candidate.parent != self.documents:
            raise StorageConfigurationError("Managed document path escaped its directory.")
        return candidate
