"""Read-only consolidated review and deterministic billing preview services.

This module deliberately operates on provisional, evidence-validated AI findings
only.  It does not create or update holdings, claims, billing agreements, or
invoices.  A billing result is an in-memory calculation using assumptions the
user enters for the current review.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Mapping, Sequence

from .ai import AIServiceError
from .ai_analysis import AIAnalysisError, AIAnalysisService
from ..storage.service import StorageService

MAX_DOCUMENTS = 20
MAX_FINDINGS = 60
MAX_QUESTION_SOURCE_ITEMS = 20
# The strict answer schema needs an opaque key for every citable source.  Too
# many keys can consume the full development request budget before any actual
# evidence is included.  Keep the list intentionally short so every selected
# key has room for its literal local excerpt.
MAX_QUESTION_CITATION_SOURCES = 12
MAX_RECONCILIATION_EXCERPTS = 20
MAX_RECONCILIATION_SOURCE_SCAN = 2400
MAX_ALLOCATION_ROWS = 50
# A batch is an explicit, paid action.  Keep it deliberately small for the
# development workflow rather than turning a folder import into an automatic
# cloud upload.  The aggregate cost ceiling is independently checked below.
# A lawyer commonly reviews a complete matter rather than a handful of files.
# This remains an explicit, sequential, cost-capped action; it is never tied
# to document import or selection.
MAX_BATCH_DOCUMENTS = 20
DEFAULT_MAX_BATCH_COST_USD = Decimal("0.20")
_MAX_BATCH_COST_USD = Decimal("1.00")
_UUID = re.compile(r"^[a-f0-9-]{36}$", re.IGNORECASE)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
_MONEY = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")
_CENT = Decimal("0.01")


class MatterReviewError(ValueError):
    """Raised for controlled, user-correctable review or preview input errors."""

    code = "matter_review_invalid"


@dataclass(frozen=True)
class AllocationRow:
    identifier: str
    label: str
    eligible_amount: Decimal


@dataclass
class BatchDocument:
    document_id: str
    filename: str
    status: str = "pending"
    analysis_job_id: str | None = None
    error_code: str | None = None


@dataclass
class AnalysisBatch:
    id: str
    status: str
    documents: list[BatchDocument]
    estimated_cost_usd: str | None
    cost_limit_usd: str
    created_at: str


@dataclass
class MatterQuestion:
    id: str
    document_ids: tuple[str, ...]
    status: str
    scope: Mapping[str, object]
    answer: str | None = None
    unknown: bool = False
    citations: tuple[Mapping[str, str], ...] = ()
    error_code: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    actual_cost_estimate_usd: str | None = None


class MatterReviewService:
    """Retrieves bounded provisional findings and calculates a non-persistent preview."""

    def __init__(self, storage: StorageService, analysis: AIAnalysisService | None = None) -> None:
        if storage.database is None:
            raise MatterReviewError("Local storage is unavailable.")
        self._database = storage.database
        self._analysis = analysis or AIAnalysisService(storage)
        self._batches: dict[str, AnalysisBatch] = {}
        self._batch_lock = threading.Lock()
        self._questions: dict[str, MatterQuestion] = {}
        self._question_lock = threading.Lock()

    def review(self, params: object) -> Mapping[str, object]:
        document_ids = self._document_ids(params)
        placeholders = ",".join("?" for _ in document_ids)
        with self._database.connection() as connection:
            documents = connection.execute(
                f"SELECT id, original_filename, file_format, imported_at, processing_status "
                f"FROM source_documents WHERE id IN ({placeholders}) ORDER BY imported_at DESC, id DESC",
                document_ids,
            ).fetchall()
            if len(documents) != len(document_ids):
                raise MatterReviewError("One or more selected documents are unavailable.")

            latest_jobs = connection.execute(
                f"SELECT j.id, j.source_document_id, j.summary_text, j.summary_unknown, j.summary_citations_json "
                f"FROM ai_processing_jobs j "
                f"WHERE j.status = 'completed' AND j.id = ("
                f"SELECT newer.id FROM ai_processing_jobs newer "
                f"WHERE newer.source_document_id = j.source_document_id AND newer.status = 'completed' "
                f"ORDER BY newer.created_at DESC, newer.id DESC LIMIT 1) "
                f"AND j.source_document_id IN ({placeholders})",
                document_ids,
            ).fetchall()
            jobs_by_document = {str(row["source_document_id"]): row for row in latest_jobs}
            job_ids = [str(row["id"]) for row in latest_jobs]
            findings: Sequence[sqlite3.Row] = ()
            if job_ids:
                job_placeholders = ",".join("?" for _ in job_ids)
                findings = connection.execute(
                    f"SELECT p.id, p.job_id, j.source_document_id, p.proposal_type, p.field_name, "
                    f"p.value_json, p.is_unknown, p.source_reference, p.evidence_snippet "
                    f"FROM ai_proposals p JOIN ai_processing_jobs j ON j.id = p.job_id "
                    f"WHERE p.validation_status = 'validated' AND p.job_id IN ({job_placeholders}) "
                    f"ORDER BY j.source_document_id, p.proposal_type, p.field_name, p.created_at, p.id "
                    f"LIMIT ?",
                    [*job_ids, MAX_FINDINGS + 1],
                ).fetchall()

        document_rows = []
        summaries = []
        missing_analysis = []
        for document in documents:
            document_id = str(document["id"])
            job = jobs_by_document.get(document_id)
            document_rows.append({
                "id": document_id,
                "filename": document["original_filename"],
                "format": document["file_format"],
                "importedAt": document["imported_at"],
                "analysisAvailable": job is not None,
            })
            if job is None:
                missing_analysis.append(document_id)
                continue
            summary = self._safe_json(job["summary_citations_json"])
            summaries.append({
                "documentId": document_id,
                "text": self._bounded_text(job["summary_text"], 900),
                "unknown": bool(job["summary_unknown"]),
                "citations": summary if isinstance(summary, list) else [],
            })

        limited_findings = findings[:MAX_FINDINGS]
        finding_rows = [{
            "id": row["id"],
            "documentId": row["source_document_id"],
            "proposalType": row["proposal_type"],
            "fieldName": row["field_name"],
            "value": self._bounded_value(self._safe_json(row["value_json"])),
            "unknown": bool(row["is_unknown"]),
            "sourceReference": row["source_reference"],
            "evidenceSnippet": self._bounded_text(row["evidence_snippet"], 280),
        } for row in limited_findings]
        return {
            "documents": document_rows,
            "summaries": summaries,
            "findings": finding_rows,
            "summary": {
                "selectedDocuments": len(document_rows),
                "analyzedDocuments": len(latest_jobs),
                "documentsWithoutCompletedAnalysis": missing_analysis,
                "provisionalFindings": len(finding_rows),
                "findingsTruncated": len(findings) > MAX_FINDINGS,
            },
        }

    def billing_preview(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"feePool", "currency", "rows"}:
            raise MatterReviewError("Provide a fee pool, currency, and allocation rows.")
        fee_pool = self._money(params["feePool"], "The fee pool must be a positive amount with up to two decimal places.")
        if fee_pool <= 0:
            raise MatterReviewError("The fee pool must be greater than zero.")
        currency = params["currency"]
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise MatterReviewError("Use a three-letter uppercase currency code.")
        rows = self._allocation_rows(params["rows"])
        total = sum((row.eligible_amount for row in rows), Decimal("0"))
        if total <= 0:
            raise MatterReviewError("At least one eligible basis must be greater than zero.")

        pool_cents = int((fee_pool / _CENT).to_integral_exact())
        raw_cents = [(row, Decimal(pool_cents) * row.eligible_amount / total) for row in rows]
        allocated_cents = {row.identifier: int(value.to_integral_value(rounding=ROUND_DOWN)) for row, value in raw_cents}
        residual = pool_cents - sum(allocated_cents.values())
        # Largest-remainder allocation deterministically reconciles exactly to
        # the user-entered fee pool.  Identifier is the stable tie-breaker.
        for row, _value in sorted(raw_cents, key=lambda item: (-(item[1] - item[1].to_integral_value(rounding=ROUND_DOWN)), item[0].identifier))[:residual]:
            allocated_cents[row.identifier] += 1

        allocations = []
        for row in rows:
            fee = Decimal(allocated_cents[row.identifier]) * _CENT
            allocations.append({
                "id": row.identifier,
                "label": row.label,
                "eligibleAmount": self._decimal_string(row.eligible_amount),
                "share": self._decimal_string(row.eligible_amount / total, places=6),
                "allocatedFee": self._decimal_string(fee, places=2),
                "currency": currency,
            })
        return {
            "feePool": self._decimal_string(fee_pool, places=2),
            "currency": currency,
            "totalEligibleAmount": self._decimal_string(total),
            "allocatedTotal": self._decimal_string(sum((Decimal(item["allocatedFee"]) for item in allocations), Decimal("0")), places=2),
            "allocations": allocations,
            "assumptions": [
                "This is a non-persistent calculation preview, not an invoice or approved billing record.",
                "The fee pool, currency, and eligible bases were entered for this preview and were not inferred from documents or AI findings.",
                "Allocation uses eligible basis / total eligible basis × fee pool, rounded by the largest-remainder method so allocations reconcile exactly to the fee pool.",
                "Legal claimant, claim eligibility, tax, currency conversion, and invoice-recipient decisions remain unresolved unless separately confirmed.",
            ],
        }

    def analysis_batch_preview(self, params: object) -> Mapping[str, object]:
        """Prepare local-only per-document analysis scopes and aggregate cost.

        This intentionally calls only ``AIAnalysisService.preview``.  It does
        not contact a provider, create analysis jobs, or transfer content.
        """
        document_ids = self._batch_document_ids(params, require_acknowledgement=False)
        documents = self._source_documents(document_ids)
        results: list[dict[str, object]] = []
        estimated_total = Decimal("0")
        all_ready = True
        global_authorization = False
        configured = False
        for document in documents:
            document_id = str(document["id"])
            try:
                preview = self._analysis.preview({"documentId": document_id})
                cost = preview.get("cost")
                estimated_cost = cost.get("estimatedCostUsd") if isinstance(cost, dict) else None
                within_document_limit = bool(cost.get("withinLimit")) if isinstance(cost, dict) else False
                if isinstance(estimated_cost, str):
                    estimated_total += Decimal(estimated_cost)
                else:
                    all_ready = False
                ready = bool(preview.get("configured")) and within_document_limit and isinstance(estimated_cost, str)
                all_ready = all_ready and ready
                global_authorization = bool(preview.get("globalAuthorization"))
                configured = bool(preview.get("configured"))
                results.append({
                    "documentId": document_id,
                    "filename": str(document["original_filename"]),
                    "ready": ready,
                    "errorCode": None,
                    "scope": preview.get("scope"),
                    "cost": cost,
                })
            except (AIServiceError, ValueError, InvalidOperation) as error:
                all_ready = False
                code = error.code if isinstance(error, AIServiceError) else "ANALYSIS_PREVIEW_UNAVAILABLE"
                results.append({
                    "documentId": document_id,
                    "filename": str(document["original_filename"]),
                    "ready": False,
                    "errorCode": code,
                    "scope": None,
                    "cost": None,
                })

        limit = self._batch_cost_limit()
        return {
            "documents": results,
            "globalAuthorization": global_authorization,
            "configured": configured,
            "estimatedCostUsd": self._decimal_string(estimated_total, places=6),
            "costLimitUsd": self._decimal_string(limit, places=2),
            "withinCostLimit": estimated_total <= limit,
            "ready": all_ready and estimated_total <= limit and global_authorization and configured,
            "maxDocuments": MAX_BATCH_DOCUMENTS,
            "maxRequestsPerDocument": 1,
            "automaticRetries": 0,
        }

    def start_analysis_batch(self, params: object) -> Mapping[str, object]:
        """Start one explicitly authorized, sequential batch of document jobs."""
        document_ids = self._batch_document_ids(params, require_acknowledgement=True)
        preview = self.analysis_batch_preview({"documentIds": document_ids})
        if not preview["globalAuthorization"]:
            raise MatterReviewError("Enable cloud-processing authorization in Settings before starting a batch analysis.")
        if not preview["configured"]:
            raise MatterReviewError("OpenAI is not configured for this backend process.")
        if not preview["withinCostLimit"]:
            raise MatterReviewError("The combined estimate exceeds the development batch cost limit.")
        if not preview["ready"]:
            raise MatterReviewError("One or more selected documents cannot be prepared for analysis.")

        document_by_id = {str(item["documentId"]): item for item in preview["documents"] if isinstance(item, dict)}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        batch = AnalysisBatch(
            id=str(uuid.uuid4()),
            status="queued",
            documents=[BatchDocument(document_id, str(document_by_id[document_id]["filename"])) for document_id in document_ids],
            estimated_cost_usd=str(preview["estimatedCostUsd"]),
            cost_limit_usd=str(preview["costLimitUsd"]),
            created_at=now,
        )
        with self._batch_lock:
            active_documents = {
                entry.document_id
                for active in self._batches.values()
                if active.status in {"queued", "running"}
                for entry in active.documents
            }
            if active_documents.intersection(document_ids):
                raise MatterReviewError("A selected document is already being analyzed as part of another batch.")
            self._batches[batch.id] = batch

        # An audit event records the explicit multi-document authorization
        # without storing document content or a prompt.
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (id, event_type, target_type, target_id, actor_type, occurred_at, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()), "ai_matter_batch_authorized", "matter_analysis_batch", batch.id,
                    "user", now,
                    json.dumps({"document_count": len(document_ids), "provider": "openai", "payload_logged": False}),
                ),
            )
        worker = threading.Thread(target=self._run_batch, args=(batch.id,), daemon=True, name=f"group-claims-batch-{batch.id[:8]}")
        worker.start()
        return self.analysis_batch_status({"batchId": batch.id})

    def analysis_batch_status(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"batchId"} or not isinstance(params["batchId"], str) or not _UUID.fullmatch(params["batchId"]):
            raise MatterReviewError("The batch analysis reference is invalid.")
        with self._batch_lock:
            batch = self._batches.get(params["batchId"])
            if batch is None:
                raise MatterReviewError("The batch analysis is unavailable. Start a new review after restarting the application.")
            return self._batch_result(batch)

    def question_preview(self, params: object) -> Mapping[str, object]:
        """Plan a bounded cross-document question without a provider request."""
        document_ids, question, follow_up = self._matter_question_params(params, acknowledged=False)
        plan = self._matter_question_plan(document_ids, question, follow_up)
        self._analysis._refresh_configuration()
        configuration = self._analysis._configuration
        controls = self._analysis._cost_controls
        estimate = controls.estimate_cost(plan["inputTokens"], plan["maxOutputTokens"])
        estimated_cost = self._analysis._cost_string(estimate)
        within_limit = estimate is not None and estimate <= controls.max_estimated_cost_usd
        return {
            "provider": configuration.provider,
            "model": configuration.model,
            "globalAuthorization": self._analysis._global_authorized(),
            "configured": configuration.state() == "configured_not_live_tested",
            "scope": plan["scope"],
            "cost": {
                "estimatedInputTokens": plan["inputTokens"],
                "maxOutputTokens": plan["maxOutputTokens"],
                "estimatedCostUsd": estimated_cost,
                "limitUsd": self._analysis._cost_string(controls.max_estimated_cost_usd),
                "withinLimit": within_limit,
                "maxRequestAttempts": controls.max_request_attempts,
            },
        }

    def ask_question(self, params: object) -> Mapping[str, object]:
        """Start one consent-gated, in-memory matter question.

        Unlike document analysis, this never writes a proposed domain record;
        question text and answer remain in memory for the current backend run.
        """
        document_ids, question, follow_up = self._matter_question_params(params, acknowledged=True)
        preview_params: dict[str, object] = {"documentIds": document_ids, "question": question}
        if follow_up is not None:
            preview_params["followUp"] = follow_up
        preview = self.question_preview(preview_params)
        if not preview["globalAuthorization"]:
            raise MatterReviewError("Enable cloud-processing authorization in Settings before asking a consolidated question.")
        if not preview["configured"]:
            raise MatterReviewError("OpenAI is not configured for this backend process.")
        cost = preview["cost"]
        if not isinstance(cost, dict) or not cost["withinLimit"]:
            raise MatterReviewError("This question exceeds the current development safety limit.")
        plan = self._matter_question_plan(document_ids, question, follow_up)
        with self._question_lock:
            if any(job.status in {"queued", "running"} for job in self._questions.values()):
                raise MatterReviewError("Another consolidated question is already being answered.")
            job = MatterQuestion(
                id=str(uuid.uuid4()), document_ids=tuple(document_ids), status="queued", scope=plan["scope"]
            )
            self._questions[job.id] = job
        worker = threading.Thread(
            target=self._run_matter_question,
            args=(job.id, question, plan), daemon=True, name=f"group-claims-matter-question-{job.id[:8]}"
        )
        worker.start()
        return self.question_status({"questionId": job.id})

    def question_status(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"questionId"} or not isinstance(params["questionId"], str) or not _UUID.fullmatch(params["questionId"]):
            raise MatterReviewError("The consolidated question reference is invalid.")
        with self._question_lock:
            job = self._questions.get(params["questionId"])
            if job is None:
                raise MatterReviewError("The consolidated question is unavailable after a backend restart.")
            return {
                "questionId": job.id,
                "status": job.status,
                "answer": job.answer,
                "unknown": job.unknown,
                "citations": list(job.citations),
                "errorCode": job.error_code,
                "model": job.model,
                "scope": dict(job.scope),
                "usage": {
                    "inputTokens": job.input_tokens,
                    "outputTokens": job.output_tokens,
                    "totalTokens": job.total_tokens,
                    "actualCostEstimateUsd": job.actual_cost_estimate_usd,
                },
            }

    def _run_matter_question(self, question_id: str, question: str, plan: Mapping[str, object]) -> None:
        with self._question_lock:
            job = self._questions.get(question_id)
            if job is None:
                return
            job.status = "running"
        try:
            self._analysis._refresh_configuration()
            if not self._analysis._global_authorized():
                raise AIAnalysisError("AI_AUTHORIZATION_WITHDRAWN", "Cloud-processing authorization was withdrawn.")
            configuration = self._analysis._configuration
            if configuration.provider != "openai" or not configuration.credential_configured:
                raise AIAnalysisError("AI_PROVIDER_NOT_CONFIGURED", "OpenAI is not configured.")
            result = self._analysis._new_provider(int(plan["maxOutputTokens"])).answer_question(
                question, str(plan["context"]), tuple(plan["citationKeys"])
            )
            local_fallback = plan.get("localFallback")
            # A comparison across a selected matter must never be presented
            # as comprehensive when the bounded prompt could include evidence
            # from only a subset of those documents.  Prefer the precise,
            # cited local coverage statement even if a model attempts a broad
            # answer from the partial context.
            coverage_incomplete_comparison = plan.get("comparisonCoverageComplete") is False
            if (result.unknown or not result.answer or coverage_incomplete_comparison) and isinstance(local_fallback, tuple):
                answer, keys = local_fallback
                result = type(result)(
                    answer, False,
                    tuple({"sourceReference": key, "evidenceSnippet": ""} for key in keys),
                    result.model, result.input_tokens, result.output_tokens, result.total_tokens,
                )
            citations = self._validate_matter_citations(result.citations, plan["citationTargets"])
            if not citations:
                raise AIAnalysisError("AI_ANSWER_EVIDENCE_UNSUPPORTED", "The answer did not include supported local evidence.")
            controls = self._analysis._cost_controls
            actual_cost = controls.estimate_cost(result.input_tokens, result.output_tokens) if result.input_tokens is not None and result.output_tokens is not None else None
            with self._question_lock:
                job = self._questions.get(question_id)
                if job is None:
                    return
                job.status = "completed"
                job.answer = result.answer
                job.unknown = result.unknown
                job.citations = tuple(citations)
                job.model = result.model
                job.input_tokens = result.input_tokens
                job.output_tokens = result.output_tokens
                job.total_tokens = result.total_tokens
                job.actual_cost_estimate_usd = self._analysis._cost_string(actual_cost)
        except AIServiceError as error:
            with self._question_lock:
                job = self._questions.get(question_id)
                if job is not None:
                    job.status = "failed"
                    job.error_code = error.code
        except Exception:
            with self._question_lock:
                job = self._questions.get(question_id)
                if job is not None:
                    job.status = "failed"
                    job.error_code = "AI_PROVIDER_FAILURE"

    def _matter_question_plan(
        self,
        document_ids: Sequence[str],
        question: str,
        follow_up: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        documents = self._source_documents(document_ids)
        names = {str(row["id"]): str(row["original_filename"]) for row in documents}
        placeholders = ",".join("?" for _ in document_ids)
        with self._database.connection() as connection:
            summary_rows = connection.execute(
                f"SELECT j.source_document_id, j.extraction_id, j.summary_text, j.summary_unknown, j.summary_citations_json "
                f"FROM ai_processing_jobs j WHERE j.status = 'completed' "
                f"AND j.id = (SELECT newer.id FROM ai_processing_jobs newer "
                f"WHERE newer.source_document_id = j.source_document_id AND newer.status = 'completed' "
                f"ORDER BY newer.created_at DESC, newer.id DESC LIMIT 1) "
                f"AND j.source_document_id IN ({placeholders}) ORDER BY j.source_document_id",
                list(document_ids),
            ).fetchall()
            rows = connection.execute(
                f"SELECT p.source_reference, p.evidence_snippet, p.proposal_type, p.field_name, p.value_json, p.is_unknown, "
                f"j.source_document_id, j.extraction_id FROM ai_proposals p JOIN ai_processing_jobs j ON j.id = p.job_id "
                f"WHERE p.validation_status = 'validated' AND j.status = 'completed' "
                f"AND j.id = (SELECT newer.id FROM ai_processing_jobs newer WHERE newer.source_document_id = j.source_document_id "
                f"AND newer.status = 'completed' ORDER BY newer.created_at DESC, newer.id DESC LIMIT 1) "
                f"AND j.source_document_id IN ({placeholders}) ORDER BY j.source_document_id, p.created_at, p.id LIMIT 41",
                list(document_ids),
            ).fetchall()

            # A completed document analysis can contain a useful short
            # provisional summary (for example, an explicitly recorded
            # transfer) even where its individual proposals are sparse.  A
            # summary enters the next question only through one of its
            # previously validated source citations, and the literal source
            # text is still the evidence passed to the provider.
            candidates: list[tuple[str, str, str, str, str]] = []
            reconciliation_candidates: list[tuple[str, str, str, str, str]] = []
            seen_sources: set[tuple[str, str]] = set()

            def add_candidate(
                document_id: str, extraction_id: str, reference: str, description: str,
                *, prioritize_for_question: bool = False,
            ) -> None:
                evidence = self._analysis._source_evidence(connection, extraction_id, reference)
                if evidence is None:
                    return
                canonical_reference, source_text = evidence
                identity = (document_id, canonical_reference)
                if identity in seen_sources:
                    return
                excerpt = self._analysis._evidence_excerpt(source_text)
                if not excerpt:
                    return
                seen_sources.add(identity)
                target = reconciliation_candidates if prioritize_for_question else candidates
                target.append((document_id, extraction_id, canonical_reference, names[document_id],
                               f"{description}; EVIDENCE: {excerpt}"))

            for row in summary_rows:
                if bool(row["summary_unknown"]):
                    continue
                summary = self._bounded_text(row["summary_text"], 420)
                citations = self._safe_json(row["summary_citations_json"])
                if not summary or not isinstance(citations, list):
                    continue
                for citation in citations:
                    reference = citation.get("sourceReference") if isinstance(citation, Mapping) else None
                    if isinstance(reference, str) and reference.strip():
                        add_candidate(
                            str(row["source_document_id"]), str(row["extraction_id"]), reference,
                            f"EARLIER PROVISIONAL SUMMARY: {summary}",
                        )
                        # One verified citation per summary keeps the
                        # question context bounded across many documents.
                        break

            for row in rows:
                source_document_id = str(row["source_document_id"])
                value = "Unknown" if bool(row["is_unknown"]) else self._safe_json(row["value_json"])
                add_candidate(
                    source_document_id, str(row["extraction_id"]), str(row["source_reference"]),
                    f"FINDING: {row['proposal_type']}.{row['field_name']} = {value if value is not None else 'Unknown'}",
                )

            # A prior preliminary analysis may have concentrated on holdings
            # details.  Add a small set of deterministic source excerpts for
            # questions about explicit legal events *or* factual changes in
            # subfund/account/position snapshots.  The latter must not be
            # misclassified as a litigation-status question merely because it
            # contains the word "subfund".  This is still bounded and each
            # excerpt is independently re-resolved before display.
            extraction_documents = {
                str(row["extraction_id"]): str(row["source_document_id"])
                for row in summary_rows
            }
            if extraction_documents:
                extraction_ids = list(extraction_documents)
                extraction_placeholders = ",".join("?" for _ in extraction_ids)
                raw_sources = connection.execute(
                    f"SELECT extraction_id, source_reference, content_kind, sheet_name, row_number, "
                    f"COALESCE(value_text, '') || ' ' || "
                    f"COALESCE(formula_text, '') AS text FROM extracted_content "
                    f"WHERE extraction_id IN ({extraction_placeholders}) AND is_empty = 0 "
                    f"ORDER BY extraction_id, worksheet_index, sheet_name, row_number, column_number LIMIT ?",
                    [*extraction_ids, MAX_RECONCILIATION_SOURCE_SCAN],
                ).fetchall()
                raw_sources = [*raw_sources, *connection.execute(
                    f"SELECT p.extraction_id, 'page ' || p.page_number || ' block ' || b.block_index AS source_reference, "
                    f"'pdf_text_block' AS content_kind, NULL AS sheet_name, NULL AS row_number, b.text "
                    f"FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                    f"WHERE p.extraction_id IN ({extraction_placeholders}) AND b.text <> '' "
                    f"ORDER BY p.extraction_id, p.page_number, b.block_index LIMIT ?",
                    [*extraction_ids, MAX_RECONCILIATION_SOURCE_SCAN],
                ).fetchall()]
                # A follow-up may say "those" or "that holding".  The prior
                # user question improves source selection; the prior answer is
                # never treated as evidence.
                retrieval_question = question if follow_up is None else f"{follow_up['question']}\n{question}"
                change_question = (
                    self._is_change_question(retrieval_question)
                    and not self._is_legal_status_question(retrieval_question)
                )
                snapshot_rows: set[tuple[str, str, int]] = set()
                if change_question:
                    # Spreadsheet values are stored one cell at a time.  If a
                    # heading or an event-bearing cell is relevant, include
                    # that row and the next two rows so the model sees the
                    # associated account, quantity, date, or identifier.  It
                    # still receives and cites individual stored cells only.
                    for source in raw_sources:
                        row_number = source["row_number"]
                        sheet_name = source["sheet_name"]
                        source_text = str(source["text"] or "")
                        if (
                            isinstance(row_number, int)
                            and isinstance(sheet_name, str)
                            and (
                                self._is_snapshot_heading(source_text)
                                or self._is_question_relevant_source(retrieval_question, source_text)
                            )
                        ):
                            extraction_id = str(source["extraction_id"])
                            for offset in range(3):
                                snapshot_rows.add((extraction_id, sheet_name, row_number + offset))

                for source in raw_sources:
                    if len(reconciliation_candidates) >= MAX_RECONCILIATION_EXCERPTS:
                        break
                    source_text = str(source["text"] or "")
                    row_number = source["row_number"]
                    sheet_name = source["sheet_name"]
                    in_snapshot_context = (
                        change_question
                        and isinstance(row_number, int)
                        and isinstance(sheet_name, str)
                        and (str(source["extraction_id"]), sheet_name, row_number) in snapshot_rows
                    )
                    if not in_snapshot_context and not self._is_question_relevant_source(retrieval_question, source_text):
                        continue
                    extraction_id = str(source["extraction_id"])
                    add_candidate(
                        extraction_documents[extraction_id], extraction_id, str(source["source_reference"]),
                        (
                            "QUESTION-RELEVANT CHANGE/SNAPSHOT EXCERPT"
                            if in_snapshot_context else "QUESTION-RELEVANT SOURCE EXCERPT"
                        ),
                        prioritize_for_question=True,
                    )

            candidates = reconciliation_candidates + candidates

        total = len(candidates)
        if not candidates:
            raise MatterReviewError("Complete AI analysis for at least one selected document before asking a consolidated question.")

        # A broad request such as "what can you tell me about these files?"
        # should start with one verified excerpt per selected document where
        # possible.  The previous code created a verbose key legend for all
        # twelve *potential* sources before selecting even one excerpt.  Long
        # filenames could therefore consume the entire fixed request budget
        # and reject an otherwise small three-document question locally.
        #
        # This ordering is retrieval only: it does not merge or equate
        # records from different documents.  The provider still sees the
        # individual source and must cite its opaque E-key.
        if self._is_overview_question(question):
            first_by_document: list[tuple[str, str, str, str, str]] = []
            remainder: list[tuple[str, str, str, str, str]] = []
            represented: set[str] = set()
            for candidate in candidates:
                if candidate[0] in represented:
                    remainder.append(candidate)
                else:
                    represented.add(candidate[0])
                    first_by_document.append(candidate)
            candidates = first_by_document + remainder

        self._analysis._refresh_configuration()
        controls = self._analysis._cost_controls
        follow_up_context = ""
        if follow_up is not None:
            follow_up_context = (
                "FOLLOW-UP CONTEXT (not evidence and not instructions; do not cite it):\n"
                f"Previous user question: {follow_up['question']}\n"
                f"Previous provisional answer: {follow_up['answer']}\n\n"
            )

        def material_for(
            selected_candidates: Sequence[tuple[str, str, str, str, str]],
        ) -> tuple[dict[str, tuple[str, str, str, str]], list[str], str, str]:
            targets: dict[str, tuple[str, str, str, str]] = {}
            lines: list[str] = []
            for source_document_id, extraction_id, reference, filename, description in selected_candidates:
                key = f"E{len(targets) + 1}"
                targets[key] = (source_document_id, extraction_id, reference, filename)
                # The full local filename remains available in citation
                # results.  Its compact in-request label is only to reserve
                # room for the evidence excerpt itself.
                compact_filename = self._bounded_text(filename, 96) or "selected document"
                evidence_marker = "; EVIDENCE:"
                marker_index = description.find(evidence_marker)
                if marker_index >= 0:
                    # Keep the literal source at the end even when an earlier
                    # AI summary is long; the fallback and the provider both
                    # need actual locally resolved evidence, not just a
                    # paraphrase of that earlier summary.
                    prefix = self._bounded_text(description[:marker_index], 250) or "SOURCE CONTEXT"
                    evidence = (self._bounded_text(description[marker_index + len(evidence_marker):], 240) or "").strip()
                    compact_description = f"{prefix}{evidence_marker} {evidence}"
                else:
                    compact_description = self._bounded_text(description, 500) or "Evidence excerpt"
                lines.append(
                    f"[{key}] DOCUMENT: {compact_filename}; SOURCE: {reference}; {compact_description}"
                )
            evidence_document_ids = {target[0] for target in targets.values()}
            coverage_context = (
                f"QUESTION COVERAGE: Evidence excerpts represent {len(evidence_document_ids)} of {len(document_ids)} selected documents. "
                "Do not describe the result as a complete cross-document comparison unless all selected documents are represented."
            )
            # E-keys are intentionally opaque; every key's document/source
            # detail is already displayed immediately beside its literal
            # excerpt.  Avoid repeating long filenames in a separate legend.
            citation_key_context = "\n".join(f"[{key}]" for key in targets)
            return targets, lines, citation_key_context, coverage_context

        selected_candidates: list[tuple[str, str, str, str, str]] = []
        for candidate in candidates:
            if len(selected_candidates) >= MAX_QUESTION_CITATION_SOURCES:
                break
            proposed = [*selected_candidates, candidate]
            _targets, proposed_lines, proposed_key_context, proposed_coverage = material_for(proposed)
            proposed_prompt = self._analysis._question_prompt(
                question,
                proposed_key_context,
                "",
                f"{follow_up_context}{proposed_coverage}\n" + "\n".join(proposed_lines),
            )
            if len(proposed_prompt) <= controls.max_input_characters:
                selected_candidates.append(candidate)

        selected_targets, selected, citation_key_context, coverage_context = material_for(selected_candidates)
        if not selected_targets:
            raise MatterReviewError("The selected evidence is too large for the current bounded question limit.")
        evidence_document_ids = {target[0] for target in selected_targets.values()}
        comparison_question = self._is_comparison_question(question)
        comparison_coverage_complete = not comparison_question or len(evidence_document_ids) == len(document_ids)
        selected_context = "\n".join(selected)
        context = (
            f"CITATION KEYS (return an E-key only):\n{citation_key_context}\n\n"
            f"{follow_up_context}{coverage_context}\n\nEVIDENCE-LINKED PROVISIONAL FINDINGS:\n{selected_context}"
        )
        prompt = self._analysis._question_prompt(question, citation_key_context, "", f"{follow_up_context}{coverage_context}\n{selected_context}")
        return {
            "context": context,
            "citationKeys": tuple(selected_targets),
            "citationTargets": selected_targets,
            # Fallback only reports literal local observations or a precise
            # limitation. It cannot create a legal conclusion, participant,
            # billing allocation, or inferred relationship.
            "localFallback": self._local_matter_question_fallback(
                question, selected, selected_targets,
                selected_document_count=len(document_ids), evidence_document_count=len(evidence_document_ids),
            ),
            "comparisonCoverageComplete": comparison_coverage_complete,
            "inputTokens": controls.estimate_tokens(len(prompt)),
            "maxOutputTokens": min(800, controls.max_output_tokens),
            "scope": {
                "sourceItems": len(selected),
                "totalSourceItems": total,
                "excludedSourceItems": max(total - len(selected), 0),
                "truncated": total > len(selected),
                "documentsSelected": len(document_ids),
                "documentsWithEvidence": len(evidence_document_ids),
                "comparisonCoverageComplete": comparison_coverage_complete,
            },
        }

    def _local_matter_question_fallback(
        self,
        question: str,
        lines: Sequence[str],
        targets: Mapping[str, tuple[str, str, str, str]],
        *,
        selected_document_count: int,
        evidence_document_count: int,
    ) -> tuple[str, tuple[str, ...]] | None:
        """Return a cited local answer when a provider yields an empty/unknown result.

        This prevents a valid evidence set becoming a generic UI rejection.
        The outcome remains deliberately limited to what the locally stored
        excerpts establish; it is not a substitute for legal review.
        """
        evidence: list[tuple[str, str]] = []
        for line in lines:
            match = re.match(r"^\[([^\]]+)\].*?EVIDENCE:\s*(.*)$", line)
            if match is None:
                continue
            key, text = match.group(1), " ".join(match.group(2).split())
            if key in targets and text:
                evidence.append((key, text))
        if not evidence:
            return None

        legal_status = self._is_legal_status_question(question)
        change = self._is_change_question(question) and not legal_status
        comparison_question = self._is_comparison_question(question)
        if comparison_question and evidence_document_count < selected_document_count:
            excerpts = "; ".join(f"“{self._analysis._bounded_question_excerpt(text)}”" for _key, text in evidence[:2])
            return (
                f"This question requests a comparison across {selected_document_count} selected documents, but the bounded "
                f"evidence set represents only {evidence_document_count}. It cannot support a complete all-document comparison. "
                f"The cited excerpts currently available are: {excerpts}.",
                tuple(key for key, _text in evidence[:2]),
            )
        if legal_status:
            explicit = [item for item in evidence if self._is_explicit_legal_status_source(item[1])]
            if explicit:
                chosen = explicit[:2]
                excerpts = "; ".join(f"“{self._analysis._bounded_question_excerpt(text)}”" for _key, text in chosen)
                return (
                    "The cited material contains express litigation, settlement, withdrawal, or rights-related language: "
                    f"{excerpts}. This is a provisional factual observation and requires legal review of scope and effect.",
                    tuple(key for key, _text in chosen),
                )
            return (
                "No explicit litigation participation, withdrawal, settlement, or assignment of rights is identified "
                "in the cited selected evidence. Holdings or account records alone do not establish that legal status.",
                (evidence[0][0],),
            )
        if change:
            explicit = [item for item in evidence if self._analysis._has_explicit_change_event(item[1])]
            if explicit:
                chosen = explicit[:2]
                excerpts = "; ".join(f"“{self._analysis._bounded_question_excerpt(text)}”" for _key, text in chosen)
                return (
                    "The selected documents record explicit change-related information: "
                    f"{excerpts}. This is a factual observation only; it does not itself establish a legal transfer, "
                    "claim assignment, litigation-status change, or billing entitlement.",
                    tuple(key for key, _text in chosen),
                )
            return (
                "The cited selected evidence records holdings, account, quantity, date, or subfund snapshots, but no "
                "explicit event can be concluded from the available excerpt set alone. Compare the dated records before "
                "treating a snapshot difference as a transfer or entitlement change.",
                tuple(key for key, _text in evidence[:2]),
            )
        if self._is_overview_question(question):
            chosen = evidence[:3]
            excerpts = "; ".join(
                f"“{self._analysis._bounded_question_excerpt(text)}”" for _key, text in chosen
            )
            coverage = (
                "all selected documents" if evidence_document_count == selected_document_count
                else f"{evidence_document_count} of {selected_document_count} selected documents"
            )
            return (
                f"The cited material provides this provisional overview of {coverage}: {excerpts}. "
                "This reports only the linked records and does not establish legal status, claim transfer, or billing entitlement.",
                tuple(key for key, _text in chosen),
            )
        # A last, evidence-linked limitation is clearer than the renderer's
        # generic unknown card and gives the reviewer the exact local material
        # that needs to be supplemented for a more specific answer.
        return (
            "The cited selected evidence does not contain enough directly linked information to answer this question "
            "reliably. Review the supporting source excerpt or select the related dated documents for comparison.",
            (evidence[0][0],),
        )

    @staticmethod
    def _is_explicit_legal_status_source(source_text: str) -> bool:
        text = source_text.casefold()
        return any(term in text for term in (
            "litigation", "claim", "assignment", "assigned", "cession", "withdraw", "settlement",
            "abtret", "anspruch", "klage", "verfahren", "vergleich", "austritt", "beteilig",
        ))

    @staticmethod
    def _is_comparison_question(question: str) -> bool:
        query = question.casefold()
        return any(term in query for term in (
            "compare", "comparison", "across", "each isin", "for each isin", "each available date",
            "quantities differ", "which quantities differ", "differences between", "difference between",
        ))

    @staticmethod
    def _is_overview_question(question: str) -> bool:
        """Recognise a request for a document-by-document factual overview.

        This affects source selection only.  It never permits the service to
        infer ownership, legal status, entitlement, or a billing rule.
        """
        query = " ".join(question.casefold().split())
        return any(phrase in query for phrase in (
            "what all", "tell me about", "what can you tell", "summar", "overview",
            "what is in", "what does", "recorded here", "records are there",
        ))

    def _validate_matter_citations(
        self, citations: Sequence[Mapping[str, str]], targets: object
    ) -> list[Mapping[str, str]]:
        if not isinstance(targets, dict):
            return []
        validated: list[Mapping[str, str]] = []
        with self._database.connection() as connection:
            for citation in citations:
                key = citation.get("sourceReference") if isinstance(citation, dict) else None
                target = targets.get(key) if isinstance(key, str) else None
                if not isinstance(target, tuple) or len(target) != 4:
                    continue
                document_id, extraction_id, reference, filename = target
                evidence = self._analysis._source_evidence(connection, extraction_id, reference)
                if evidence is None:
                    continue
                canonical_reference, source_text = evidence
                excerpt = self._analysis._evidence_excerpt(source_text)
                if excerpt:
                    validated.append({
                        "documentId": document_id,
                        "filename": filename,
                        "sourceReference": canonical_reference,
                        "evidenceSnippet": excerpt,
                    })
        return validated

    @staticmethod
    def _is_question_relevant_source(question: str, source_text: str) -> bool:
        """Select only bounded source material likely to bear on the question.

        This is retrieval, not a legal conclusion.  The provider must still
        cite the exact local excerpt and the response remains provisional.
        """
        query = question.casefold()
        text = source_text.casefold()
        legal_status_question = MatterReviewService._is_legal_status_question(question)
        if legal_status_question:
            terms = {
                "litig", "claim", "right", "assign", "transfer", "withdraw", "settle", "mandate", "participat",
                "entitle", "abtret", "übertrag", "uebertrag", "klage", "anspruch", "verfahren", "vergleich",
                "austritt", "beteilig", "beend", "kündig", "kuendig",
            }
        elif MatterReviewService._is_change_question(question):
            terms = {
                "change", "changed", "transfer", "trade", "transaction", "movement", "purchase", "sale",
                "sell", "redeem", "redemption", "liquidat", "merge", "split", "delete", "remov", "close",
                "subfund", "sub-fund", "fund", "account", "holding", "position", "quantity", "share",
                "unit", "isin", "date", "übertrag", "uebertrag", "änder", "aender", "bestand", "konto",
                "verkauf", "kauf", "tilgung", "liquid", "teilfonds", "teil-fonds", "fonds", "depot",
                "anteil", "anteile", "wertpapier", "zuteil", "transaktion", "rückzahlung", "rueckzahlung",
                "abschreib", "herabsetz", "umtausch", "kündigung", "kuendigung",
            }
        else:
            terms = {token for token in re.findall(r"[a-z0-9]{5,}", query)}
        return bool(terms) and any(term in text for term in terms)

    @staticmethod
    def _is_legal_status_question(question: str) -> bool:
        query = question.casefold()
        return any(token in query for token in (
            "litigation", "participat", "claim", "right", "assign", "withdraw", "settle",
        ))

    @staticmethod
    def _is_change_question(question: str) -> bool:
        query = question.casefold()
        return any(token in query for token in (
            "change", "changed", "difference", "different", "transfer", "movement", "transaction",
            "trade", "holding", "position", "quantity", "merge", "liquidat", "delete", "remov",
            "increase", "decrease", "reduction", "addition",
            "übertrag", "uebertrag", "änder", "aender", "bestand", "konto",
        ))

    @staticmethod
    def _is_snapshot_heading(source_text: str) -> bool:
        text = source_text.casefold().strip()
        return any(term in text for term in (
            "subfund", "sub-fund", "fund", "account", "holder", "investor", "security", "isin",
            "holding", "position", "quantity", "shares", "units", "date", "transaction", "trade",
            "konto", "bestand", "menge", "wertpapier", "datum", "teilfonds", "teil-fonds", "fonds", "depot",
        ))

    @staticmethod
    def _matter_question_params(
        params: object, acknowledged: bool,
    ) -> tuple[list[str], str, Mapping[str, str] | None]:
        expected = {"documentIds", "question", "acknowledged"} if acknowledged else {"documentIds", "question"}
        if not isinstance(params, dict) or not expected.issubset(params) or not set(params).issubset(expected | {"followUp"}):
            raise MatterReviewError("The consolidated question request is invalid.")
        if acknowledged and params.get("acknowledged") is not True:
            raise MatterReviewError("Confirm external processing before asking a consolidated question.")
        question = params.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 600:
            raise MatterReviewError("Enter a question of no more than 600 characters.")
        follow_up = params.get("followUp")
        if follow_up is not None:
            if (
                not isinstance(follow_up, Mapping)
                or set(follow_up) != {"question", "answer"}
                or not isinstance(follow_up.get("question"), str)
                or not isinstance(follow_up.get("answer"), str)
                or not follow_up["question"].strip()
                or not follow_up["answer"].strip()
                or len(follow_up["question"]) > 600
                or len(follow_up["answer"]) > 1200
            ):
                raise MatterReviewError("The follow-up context is invalid.")
            follow_up = {
                "question": follow_up["question"].strip(),
                "answer": follow_up["answer"].strip(),
            }
        document_ids = MatterReviewService._batch_document_ids({"documentIds": params.get("documentIds")}, require_acknowledgement=False)
        return document_ids, question.strip(), follow_up

    def _run_batch(self, batch_id: str) -> None:
        with self._batch_lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return
            batch.status = "running"
        for entry in batch.documents:
            with self._batch_lock:
                entry.status = "running"
            try:
                # ``start`` rechecks global authorization and cost just before
                # each job starts; the AI service rechecks consent immediately
                # before its one external request.
                started = self._analysis.start({"documentId": entry.document_id, "acknowledged": True})
                job_id = started.get("jobId")
                if not isinstance(job_id, str):
                    raise MatterReviewError("The backend returned an invalid document analysis job.")
                with self._batch_lock:
                    entry.analysis_job_id = job_id
                while True:
                    status = self._analysis.job_status({"jobId": job_id})
                    value = status.get("status")
                    if value in {"completed", "failed"}:
                        error_code = status.get("errorCode") if isinstance(status.get("errorCode"), str) else None
                        with self._batch_lock:
                            entry.status = str(value)
                            entry.error_code = error_code
                        if value == "failed" and error_code in {
                            "AI_AUTHORIZATION_WITHDRAWN", "AI_AUTHORIZATION_REQUIRED",
                            "AI_AUTHENTICATION_FAILED", "AI_PERMISSION_DENIED", "AI_RATE_LIMITED",
                        }:
                            self._block_pending(batch, error_code)
                            self._finish_batch(batch)
                            return
                        break
                    time.sleep(0.2)
            except AIServiceError as error:
                with self._batch_lock:
                    entry.status = "failed"
                    entry.error_code = error.code
                # Withdrawal is a hard stop: never begin a later transfer.
                if error.code in {"AI_AUTHORIZATION_WITHDRAWN", "AI_AUTHORIZATION_REQUIRED"}:
                    self._block_pending(batch, error.code)
                    self._finish_batch(batch)
                    return
            except Exception:
                with self._batch_lock:
                    entry.status = "failed"
                    entry.error_code = "BATCH_DOCUMENT_FAILURE"
        self._finish_batch(batch)

    def _block_pending(self, batch: AnalysisBatch, code: str) -> None:
        """Prevent a known credential/authorization failure becoming a retry loop."""
        with self._batch_lock:
            for pending in batch.documents:
                if pending.status == "pending":
                    pending.status = "blocked"
                    pending.error_code = code

    def _finish_batch(self, batch: AnalysisBatch) -> None:
        with self._batch_lock:
            batch.status = "completed" if all(entry.status == "completed" for entry in batch.documents) else "partial"

    @staticmethod
    def _batch_result(batch: AnalysisBatch) -> Mapping[str, object]:
        completed = sum(entry.status == "completed" for entry in batch.documents)
        return {
            "batchId": batch.id,
            "createdAt": batch.created_at,
            "status": batch.status,
            "estimatedCostUsd": batch.estimated_cost_usd,
            "costLimitUsd": batch.cost_limit_usd,
            "documents": [
                {"documentId": entry.document_id, "filename": entry.filename, "status": entry.status,
                 "analysisJobId": entry.analysis_job_id, "errorCode": entry.error_code}
                for entry in batch.documents
            ],
            "summary": {"total": len(batch.documents), "completed": completed,
                        "failed": sum(entry.status == "failed" for entry in batch.documents),
                        "pending": sum(entry.status in {"pending", "running"} for entry in batch.documents)},
        }

    def _source_documents(self, document_ids: Sequence[str]) -> Sequence[sqlite3.Row]:
        placeholders = ",".join("?" for _ in document_ids)
        with self._database.connection() as connection:
            documents = connection.execute(
                f"SELECT id, original_filename FROM source_documents WHERE id IN ({placeholders}) AND processing_status = 'parsed'",
                list(document_ids),
            ).fetchall()
        if len(documents) != len(document_ids):
            raise MatterReviewError("Select parsed documents only for consolidated AI analysis.")
        return documents

    @staticmethod
    def _batch_document_ids(params: object, require_acknowledgement: bool) -> list[str]:
        expected = {"documentIds", "acknowledged"} if require_acknowledgement else {"documentIds"}
        if not isinstance(params, dict) or set(params) != expected:
            raise MatterReviewError("Select documents for a consolidated AI analysis.")
        if require_acknowledgement and params.get("acknowledged") is not True:
            raise MatterReviewError("Confirm that extracted content from each selected document will be sent to OpenAI.")
        document_ids = params.get("documentIds")
        if not isinstance(document_ids, list) or not 1 <= len(document_ids) <= MAX_BATCH_DOCUMENTS:
            raise MatterReviewError(f"Select between one and {MAX_BATCH_DOCUMENTS} documents for one batch analysis.")
        if not all(isinstance(value, str) and _UUID.fullmatch(value) for value in document_ids) or len(set(document_ids)) != len(document_ids):
            raise MatterReviewError("Each selected document must have a unique valid reference.")
        return list(document_ids)

    @staticmethod
    def _batch_cost_limit() -> Decimal:
        raw = os.getenv("GROUP_CLAIMS_AI_MAX_BATCH_ESTIMATED_COST_USD", "").strip()
        try:
            value = Decimal(raw) if raw else DEFAULT_MAX_BATCH_COST_USD
        except InvalidOperation:
            return DEFAULT_MAX_BATCH_COST_USD
        return value if Decimal("0") < value <= _MAX_BATCH_COST_USD else DEFAULT_MAX_BATCH_COST_USD

    @staticmethod
    def _document_ids(params: object) -> list[str]:
        if not isinstance(params, dict) or set(params) != {"documentIds"} or not isinstance(params["documentIds"], list):
            raise MatterReviewError("Select one or more documents for the matter review.")
        document_ids = params["documentIds"]
        if not 1 <= len(document_ids) <= MAX_DOCUMENTS or not all(isinstance(value, str) and _UUID.fullmatch(value) for value in document_ids):
            raise MatterReviewError("Select up to 20 valid documents for the matter review.")
        if len(set(document_ids)) != len(document_ids):
            raise MatterReviewError("A document can be selected only once.")
        return list(document_ids)

    @staticmethod
    def _allocation_rows(value: object) -> list[AllocationRow]:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_ALLOCATION_ROWS:
            raise MatterReviewError("Provide between one and 50 allocation rows.")
        rows: list[AllocationRow] = []
        identifiers: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"id", "label", "eligibleAmount"}:
                raise MatterReviewError("Each allocation row must include an identifier, label, and eligible basis.")
            identifier, label = item["id"], item["label"]
            if not isinstance(identifier, str) or not identifier or len(identifier) > 80 or identifier in identifiers:
                raise MatterReviewError("Allocation rows need unique identifiers.")
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > 160:
                raise MatterReviewError("Each allocation row needs a short label.")
            amount = MatterReviewService._decimal(item["eligibleAmount"], "Eligible bases must be non-negative decimal amounts.")
            identifiers.add(identifier)
            rows.append(AllocationRow(identifier, label.strip(), amount))
        return rows

    @staticmethod
    def _decimal(value: object, message: str) -> Decimal:
        if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
            raise MatterReviewError(message)
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise MatterReviewError(message) from error

    @staticmethod
    def _money(value: object, message: str) -> Decimal:
        if not isinstance(value, str) or not _MONEY.fullmatch(value):
            raise MatterReviewError(message)
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise MatterReviewError(message) from error

    @staticmethod
    def _decimal_string(value: Decimal, places: int | None = None) -> str:
        if places is not None:
            value = value.quantize(Decimal(1).scaleb(-places))
        return format(value, "f")

    @staticmethod
    def _safe_json(value: object) -> object:
        if not isinstance(value, str):
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _bounded_text(value: object, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        return value[:maximum]

    @staticmethod
    def _bounded_value(value: object) -> object:
        if isinstance(value, str):
            return value[:240]
        if value is None or isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            return value
        # The matter view is deliberately an overview rather than a second
        # unbounded store of model output.  The original proposal remains in
        # SQLite, linked to its source document and evidence.
        return "Structured value"
