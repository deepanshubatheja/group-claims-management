"""Application entry point and command dispatch for the local backend."""

from __future__ import annotations

import logging
import threading
import sys
from typing import BinaryIO

from .protocol import ProtocolServer
from .services.health import get_health_status
from .services.document_import import DocumentImportError, DocumentImportService
from .services.ai import AIService, AIServiceError
from .services.ai_analysis import AIAnalysisError, AIAnalysisService
from .services.codex import CodexManager
from .services.matter_review import MatterReviewError, MatterReviewService
from .services.reconciliation import ReconciliationError, ReconciliationService
from .services.billing import BillingError, BillingService
from .storage.service import StorageService


def _start_stale_reread(reconciliation: ReconciliationService) -> None:
    """Read imported documents again when the reading rules have changed.

    Runs once in the background so startup and requests are not delayed.  It
    only stages candidates for review; failures are logged without content.
    """
    def run() -> None:
        try:
            reconciliation.reread_stale()
        except Exception:
            logging.getLogger(__name__).exception("Background re-read of imported documents failed.")

    threading.Thread(target=run, name="claimflow-stale-reread", daemon=True).start()


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
        codex = CodexManager(storage.paths.root) if storage.paths is not None else None
        ai = AIService(storage, codex_manager=codex)
        analysis = AIAnalysisService(storage, codex_manager=codex)
        matter_review = MatterReviewService(storage, analysis)
        reconciliation = ReconciliationService(storage)
        billing = BillingService(storage)
        _start_stale_reread(reconciliation)
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
            "ai.codex_connect": ai.codex_connect,
            "ai.codex_disconnect": ai.codex_disconnect,
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
            "reconciliation.stage": reconciliation.stage,
            "reconciliation.generate": reconciliation.generate,
            "reconciliation.candidate": reconciliation.candidate,
            "reconciliation.candidates": reconciliation.candidates,
            "reconciliation.confirm": reconciliation.confirm,
            "reconciliation.reject": reconciliation.reject,
            "reconciliation.apply_policy": reconciliation.apply_policy,
            "reconciliation.holdings_as_of": reconciliation.holdings_as_of,
            "reconciliation.holdings_timeline": reconciliation.holdings_timeline,
            "reconciliation.transfer_history": reconciliation.transfer_history,
            "reconciliation.claim_assignment_history": reconciliation.claim_assignment_history,
            "reconciliation.open_discrepancies": reconciliation.open_discrepancies,
            "reconciliation.result": reconciliation.reconciliation_result,
            "reconciliation.identity_questions": reconciliation.identity_questions,
            "reconciliation.resolve_identity_question": reconciliation.resolve_identity_question,
            "reconciliation.reread_documents": reconciliation.reread_documents,
            "billing.generate": billing.generate,
            "billing.candidate": billing.candidate,
            "billing.candidates": billing.candidates,
            "billing.approve_rule": billing.approve_rule,
            "billing.rule": billing.rule_details,
            "billing.preview": billing.preview,
            "billing.calculate": billing.calculate,
            "billing.runs": billing.runs,
            "billing.run": billing.run,
            "billing.compare_runs": billing.compare_runs,
        }
    except (DocumentImportError, AIServiceError, AIAnalysisError, MatterReviewError, ReconciliationError, BillingError):
        commands = {"health.check": no_params(get_health_status), "storage.status": no_params(storage.status)}
    server = ProtocolServer(
        commands
    )
    try:
        server.serve(
            stdin if stdin is not None else sys.stdin.buffer,
            stdout if stdout is not None else sys.stdout.buffer,
        )
    finally:
        if "codex" in locals() and codex is not None:
            codex.close()
    return 0
