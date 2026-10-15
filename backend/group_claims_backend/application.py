"""Application entry point and command dispatch for the local backend."""

from __future__ import annotations

import logging
import sys
from typing import BinaryIO

from .protocol import ProtocolServer
from .services.health import get_health_status
from .services.document_import import DocumentImportError, DocumentImportService
from .services.ai import AIService, AIServiceError
from .services.ai_analysis import AIAnalysisError, AIAnalysisService
from .services.matter_review import MatterReviewError, MatterReviewService
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
        ai = AIService(storage)
        analysis = AIAnalysisService(storage)
        matter_review = MatterReviewService(storage, analysis)
        commands = {
            "health.check": no_params(get_health_status), "storage.status": no_params(storage.status),
            "documents.import": lambda params: documents.import_authorized_paths(params.get("paths")),
            "documents.list": no_params(documents.list_documents),
            "documents.remove": lambda params: documents.remove_document(params.get("documentId")),
            "documents.content": lambda params: documents.content(
                params.get("documentId"), params.get("offset"), params.get("limit"), params.get("sheetName"),
                params.get("rowStart"), params.get("rowLimit"), params.get("columnStart"),
                params.get("columnLimit"), params.get("pageNumber"),
            ),
            "documents.retry_parse": lambda params: documents.retry_parse(params.get("documentId")),
            "ai.status": no_params(ai.status),
            # The connection-check service validates the empty mapping itself;
            # unlike status(), it intentionally accepts the protocol params.
            "ai.check_connection": ai.check_connection,
            "ai.set_authorization": ai.set_authorization,
            "ai.analysis_preview": analysis.preview,
            "ai.analyze_document": analysis.start,
            "ai.analysis_status": analysis.job_status,
            "ai.analysis_findings": analysis.findings,
            "ai.question_preview": analysis.question_preview,
            "ai.ask_question": analysis.ask_question,
            "ai.question_status": analysis.question_status,
            "matter.review": matter_review.review,
            "matter.billing_preview": matter_review.billing_preview,
            "matter.analysis_batch_preview": matter_review.analysis_batch_preview,
            "matter.start_analysis_batch": matter_review.start_analysis_batch,
            "matter.analysis_batch_status": matter_review.analysis_batch_status,
            "matter.question_preview": matter_review.question_preview,
            "matter.ask_question": matter_review.ask_question,
            "matter.question_status": matter_review.question_status,
        }
    except (DocumentImportError, AIServiceError, AIAnalysisError, MatterReviewError):
        commands = {"health.check": no_params(get_health_status), "storage.status": no_params(storage.status)}
    server = ProtocolServer(
        commands
    )
    server.serve(
        stdin if stdin is not None else sys.stdin.buffer,
        stdout if stdout is not None else sys.stdout.buffer,
    )
    return 0
