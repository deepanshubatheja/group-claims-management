"""Application entry point and command dispatch for the local backend."""

from __future__ import annotations

import logging
import sys
from typing import BinaryIO

from .protocol import ProtocolServer
from .services.health import get_health_status
from .services.document_import import DocumentImportError, DocumentImportService
from .storage.service import StorageService


def configure_logging() -> None:
    """Configure operational logging without writing protocol data to stdout."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def main(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    storage_root: str | None = None,
) -> int:
    """Serve the restricted local protocol until stdin is closed."""

    configure_logging()
    storage = StorageService(storage_root)
    storage.initialize()
    def no_params(handler):
        def wrapped(params):
            if params != {}:
                raise DocumentImportError("invalid_params", "This command does not accept parameters.")
            return handler()
        return wrapped
    try:
        documents = DocumentImportService(storage)
        commands = {
            "health.check": no_params(get_health_status), "storage.status": no_params(storage.status),
            "documents.import": lambda params: documents.import_authorized_paths(params.get("paths")),
            "documents.list": no_params(documents.list_documents),
            "documents.content": lambda params: documents.content(
                params.get("documentId"), params.get("offset"), params.get("limit"), params.get("sheetName"),
                params.get("rowStart"), params.get("rowLimit"), params.get("columnStart"),
                params.get("columnLimit"), params.get("pageNumber"),
            ),
            "documents.retry_parse": lambda params: documents.retry_parse(params.get("documentId")),
        }
    except DocumentImportError:
        commands = {"health.check": no_params(get_health_status), "storage.status": no_params(storage.status)}
    server = ProtocolServer(
        commands
    )
    server.serve(
        stdin if stdin is not None else sys.stdin.buffer,
        stdout if stdout is not None else sys.stdout.buffer,
    )
    return 0
