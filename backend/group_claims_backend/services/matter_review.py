"""Read-only consolidated review and deterministic billing preview services.

This module deliberately operates on provisional, evidence-linked material.
It does not create or update holdings, claims, billing agreements, or invoices.
Billing remains an in-memory calculation using either user-entered assumptions
or one explicitly approved instruction stored in the selected documents.
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
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Mapping, Sequence

from .ai import AIServiceError
from .ai_analysis import AIAnalysisError, AIAnalysisService
from .billing_allocation import (
    InstructionBlock, Participant as BillingParticipant, ScopeDocument as BillingScopeDocument, allocation_payload,
    comparison_date as billing_comparison_date, cross_check as billing_cross_check, exclusions as billing_exclusions,
    instruction_isin, missing_inputs as billing_missing_inputs, split_label as split_participant_label,
)
from .document_search import DocumentSearchIndex, SearchHit
from .document_references import CandidateDocument, DocumentConstraint, question_dates, resolve_document_references
from .entity_inventory import EntityInventoryBuilder, InventoryDocument
from .holdings_comparison import HoldingsComparison, HoldingsDocument, HoldingsResult, grouped_quantity
from .holdings_reconstruction import (
    Subject as HoldingsSubject, answer_text as reconstruction_text, billing_effect as reconstruction_billing_effect,
    gather as gather_holdings_evidence, reconstruct as reconstruct_holdings,
)
from .question_intent import (
    INTENT_CALCULATION, INTENT_COMPARISON, INTENT_HOLDINGS_COMPARISON, INTENT_HOLDINGS_RECONSTRUCTION, INTENT_INVENTORY,
    INTENT_LEGAL_STATUS, INTENT_RELATIONSHIP,
    ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY, ROLE_SUBFUND, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND,
    QuestionIntent, classify_question, is_holdings_reconstruction, lookup_identifiers, primary_request_text,
    question_entity_names,
)
from ..storage.service import StorageService

# ClaimFlow is a single-user local workspace without a separate matter table.
# The workspace is therefore one implicit matter.  Every scoped chat request
# still names it explicitly so the backend boundary is already matter-aware.
WORKSPACE_MATTER_ID = "workspace"
MAX_DOCUMENTS = 20
# All-document questions search every eligible document locally, but only a
# bounded, interleaved set of matching sources is considered for the prompt.
MAX_SEARCH_HITS_PER_DOCUMENT = 40
MAX_SEARCH_HITS = 400
MAX_SNAPSHOT_ROW_EXPANSIONS = 60
MAX_NEIGHBOUR_CONTEXT_CHARACTERS = 160
SHORT_EXCERPT_CHARACTERS = 80
_SCOPE_MODES = frozenset({"all", "selected"})
_MATTER_ID = re.compile(r"^[a-z0-9-]{1,64}$")
_DOCUMENT_CONSENT_EVENT = "ai_matter_question_document_authorized"
_UNKNOWN_FOLLOW_UP_ANSWER = "The selected evidence does not establish an answer to this question."
# Question filler words that otherwise match unrelated text in every document.
_GENERIC_QUESTION_WORDS = frozenset({
    "which", "where", "there", "their", "these", "those", "about", "across", "mention", "mentioned", "mentions",
    "upload", "uploaded", "document", "documents", "files", "record", "records", "recorded", "please", "could",
    "would", "should", "every", "other", "named", "listed", "contain", "contains", "include", "includes",
    "provide", "information", "details", "anything",
})
_SPREADSHEET_EXTENSIONS = frozenset({"xlsx", "xls", "csv"})
# Subjects one reconstruction question may cover.
MAX_RECONSTRUCTION_SUBJECTS = 3
# A legal-status question that refers to recorded holding movements.  Bare
# years are not a cue; grouped amounts such as "400,000" are.
_HOLDINGS_MOVEMENT_CUE = re.compile(
    r"\b(?:holdings?|positions?|quantit(?:y|ies)|nominal|increas\w*|decreas\w*|reduc\w*|rose|fell|movements?|bestand)\b"
    r"|\b\d{1,3}(?:[,.' ]\d{3})+\b"
)
# Rights-related wording that, outside a document's own caveat, would need
# interpretation rather than a local statement.
_LEGAL_EVENT_TERMS = ("assign", "abtret", "claim right", "legal right", "litigation", "withdraw", "settle", "anspruch")
_UNAVAILABLE_REASONS = {
    "imported": "not_parsed",
    "failed": "extraction_failed",
    "unsupported": "unsupported",
}
_SNAPSHOT_HEADING_TERMS = frozenset({
    "subfund", "sub-fund", "fund", "account", "holder", "investor", "security", "isin",
    "holding", "position", "quantity", "shares", "units", "date", "transaction", "trade",
    "konto", "bestand", "menge", "wertpapier", "datum", "teilfonds", "teil-fonds", "fonds", "depot",
})
MAX_FINDINGS = 60
MAX_QUESTION_SOURCE_ITEMS = 20
# The strict answer schema needs an opaque key for every citable source.  Too
# many keys can consume the full development request budget before any actual
# evidence is included.  Keep the list intentionally short so every selected
# key has room for its literal local excerpt.
MAX_QUESTION_CITATION_SOURCES = 12
MAX_RECONCILIATION_EXCERPTS = 20
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


@dataclass(frozen=True)
class DocumentBillingAnswer:
    """A non-persistent calculation and its evidence-derived instruction context."""

    answer: str
    citations: tuple[tuple[str, str, str, str], ...]
    instruction_context: str
    # The structured allocation shown by the answer card (allocation requests only).
    payload: Mapping[str, object] | None = None


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
    cost_limit_usd: str | None
    created_at: str


@dataclass(frozen=True)
class ScopedDocument:
    """One matter document and whether its current extraction can be searched."""

    document_id: str
    filename: str
    extraction_id: str | None
    unavailable_reason: str | None
    incomplete: bool = False
    previously_authorized: bool = False


@dataclass(frozen=True)
class DocumentScope:
    """A backend-resolved, explicit question scope within one matter."""

    matter_id: str
    mode: str
    documents: tuple[ScopedDocument, ...]

    @property
    def searchable(self) -> tuple[ScopedDocument, ...]:
        return tuple(document for document in self.documents if document.unavailable_reason is None)

    @property
    def document_ids(self) -> frozenset[str]:
        return frozenset(document.document_id for document in self.documents)


@dataclass(frozen=True)
class MatterQuestionRequest:
    matter_id: str
    mode: str
    selected_document_ids: tuple[str, ...]
    question: str
    follow_up_question_id: str | None
    consented_document_ids: frozenset[str]


@dataclass(frozen=True)
class LegalHoldingsEvidence:
    """Dated holding records behind a legal-status question about holding movements."""

    payload: Mapping[str, object]
    citations: tuple[tuple[str, str, str, str], ...]
    # One compact, document-backed excerpt pinned first on the provider path.
    candidate: tuple[str, str, str, str, str]
    # Set when the scope holds only the two workbooks and they contain no
    # rights-related wording beyond their own caveat: answered locally.
    local_answer: "LocalAnswer | None"


@dataclass(frozen=True)
class LocalAnswer:
    """An answer produced entirely from local records; nothing is transmitted."""

    intent: str
    answer: str
    citations: tuple[tuple[str, str, str, str], ...]
    model: str
    evidence_document_ids: frozenset[str] = frozenset()
    partial: bool = False
    inventory: Mapping[str, object] | None = None
    holdings: Mapping[str, object] | None = None


@dataclass
class MatterQuestion:
    id: str
    document_ids: tuple[str, ...]
    status: str
    scope: Mapping[str, object]
    matter_id: str = WORKSPACE_MATTER_ID
    question: str = ""
    document_scope: Mapping[str, object] | None = None
    # Documents whose content shaped this answer.  A follow-up may reuse the
    # answer as context only when all of them remain in the new scope.
    context_document_ids: frozenset[str] = frozenset()
    intent: str = "general"
    # A locally built entity inventory (listing and relationship questions).
    inventory: Mapping[str, object] | None = None
    # A locally calculated dated holdings comparison.
    holdings: Mapping[str, object] | None = None
    # A document-backed pro-rata allocation.
    billing: Mapping[str, object] | None = None
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

    def _document_billing_answer(
        self, documents: Sequence[ScopedDocument], question: str, *, intent: str | None = None,
    ) -> DocumentBillingAnswer | None:
        """Calculate only from an expressly approved local billing instruction.

        This is intentionally narrower than the configurable preview: it does
        not infer a fee pool, participants, eligibility, or a legal effect
        from holdings, assignments, or an AI summary.  It is useful when the
        selected material itself supplies all of those inputs, such as the
        synthetic matter supplied with ClaimFlow.
        """
        if intent is None and not self._is_document_billing_question(question):
            return None
        extraction_documents = {
            str(document.extraction_id): document.document_id
            for document in documents if document.extraction_id is not None
        }
        with self._database.connection() as connection:
            # Use the local index to find only documents that mention the
            # instruction's required inputs, rather than reading every PDF
            # block in a large all-document scope.
            matches = DocumentSearchIndex.search(
                connection, list(extraction_documents), ("fee pool", "allocation rule"),
                per_document_limit=1, total_limit=len(extraction_documents),
            )
            extraction_ids = sorted({hit.extraction_id for hit in matches.hits})
            if not extraction_ids:
                return None
            placeholders = ",".join("?" for _ in extraction_ids)
            sources = connection.execute(
                f"SELECT d.id AS document_id, d.original_filename, e.id AS extraction_id, p.page_number, "
                f"'page ' || p.page_number || ' block ' || b.block_index AS source_reference, b.text "
                f"FROM source_documents d JOIN document_extractions e ON e.source_document_id = d.id "
                f"JOIN pdf_page_extractions p ON p.extraction_id = e.id "
                f"JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                f"WHERE e.id IN ({placeholders}) AND e.status = 'parsed' AND b.text <> '' "
                f"ORDER BY d.id, p.page_number, b.block_index",
                extraction_ids,
            ).fetchall()

        pages: dict[tuple[str, str, str, int], list[sqlite3.Row]] = {}
        for source in sources:
            page_identity = (
                str(source["document_id"]), str(source["extraction_id"]),
                str(source["original_filename"]), int(source["page_number"]),
            )
            pages.setdefault(page_identity, []).append(source)
        instructions: list[tuple[list[sqlite3.Row], str, Decimal, list[AllocationRow], str]] = []
        incomplete_pages: list[list[sqlite3.Row]] = []
        for page_sources in pages.values():
            source_text = "\n".join(str(source["text"]) for source in page_sources)
            parsed = self._parse_document_billing_instruction(source_text)
            if parsed is None:
                folded = " ".join(source_text.split()).casefold()
                if "eligible participant" in folded and ("fee pool" in folded or "allocation rule" in folded):
                    incomplete_pages.append(page_sources)
                continue
            currency, fee_pool, rows, period = parsed
            instructions.append((page_sources, currency, fee_pool, rows, period))
        intent = intent or self._document_billing_intent(question)
        # Multiple valid instructions could represent conflicting billing
        # agreements.  Refuse to choose between them automatically.  An
        # allocation request says so locally, as it does for an instruction
        # that lacks a required input, instead of letting a model calculate.
        if len(instructions) != 1:
            if intent != "allocation" or not (instructions or incomplete_pages):
                return None
            return self._incomplete_billing_answer(instructions, incomplete_pages)
        page_sources, currency, fee_pool, rows, period = instructions[0]
        source_text = "\n".join(str(source["text"]) for source in page_sources)
        allocations = self._allocate_pro_rata(fee_pool, rows)
        total_eligible = sum((row.eligible_amount for row in rows), Decimal("0"))
        allocations_text = "; ".join(
            f"{row.label}: {currency} {fee:,.2f}"
            for row, fee in allocations
        )
        excluded = billing_exclusions(source_text)
        exclusion = "".join(f" {item.phrase}" for item in excluded)
        blocks = [
            InstructionBlock(
                str(source["document_id"]), str(source["extraction_id"]), str(source["original_filename"]),
                str(source["source_reference"]), str(source["text"]),
            )
            for source in page_sources
        ]
        payload: dict[str, object] | None = None
        cross_citations: list[tuple[str, str, str, str]] = []
        if intent == "allocation":
            on_date, date_basis = billing_comparison_date(source_text, period)
            participants = [
                BillingParticipant(*split_participant_label(row.label), "eligible", row.eligible_amount) for row in rows
            ] + [BillingParticipant(item.name, item.identifier, "excluded") for item in excluded]
            with self._database.connection() as connection:
                checks = billing_cross_check(
                    connection,
                    [
                        BillingScopeDocument(
                            document.document_id, document.filename, str(document.extraction_id),
                            document.filename.rsplit(".", 1)[-1].casefold() in _SPREADSHEET_EXTENSIONS,
                        )
                        for document in documents if document.extraction_id is not None
                    ],
                    blocks[0].document_id, participants, on_date, date_basis, instruction_isin(source_text),
                )
            payload = allocation_payload(
                blocks, currency=currency, fee_pool=fee_pool, period=period,
                allocations=[(row.label, row.eligible_amount, fee) for row, fee in allocations],
                excluded=excluded, checks=checks,
            )
            cross_citations = [
                *checks.citations,
                *(
                    (str(entry["source"]["documentId"]), str(entry["extractionId"]),  # type: ignore[index]
                     str(entry["source"]["reference"]), str(entry["source"]["filename"]))  # type: ignore[index]
                    for entry in checks.undetermined
                ),
            ]
        # Cite the blocks that state the instruction's inputs; a parser may
        # preserve an instruction as one large page block, which remains a
        # valid precise citation when field-level blocks are not available.
        cited_references = {
            source["reference"] for source in self._payload_sources(payload) if source["documentId"] == blocks[0].document_id
        } if payload is not None else set()
        citation_sources = [
            source for source in page_sources
            if str(source["source_reference"]) in cited_references or re.match(
                r"^\s*(Fee\s+pool|Allocation\s+rule|Eligible\s+participant|Excluded\s+participant)\b",
                str(source["text"]), flags=re.IGNORECASE,
            )
        ]
        if not citation_sources:
            citation_sources = page_sources
        if intent == "verification":
            steps = "; ".join(
                f"{row.label}: {self._document_decimal(row.eligible_amount)} / "
                f"{self._document_decimal(total_eligible)} × {currency} {fee_pool:,.2f} = {currency} {fee:,.2f}"
                for row, fee in allocations
            )
            answer = (
                f"To verify this provisional billing for {period}, first confirm the approved fee pool "
                f"({currency} {fee_pool:,.2f}) and the stated rule: eligible nominal amount / total eligible "
                f"nominal amount × fee pool. The eligible total is {self._document_decimal(total_eligible)}. "
                f"Recompute: {steps}. The allocations total {currency} {fee_pool:,.2f}.{exclusion} "
                "Also confirm that the instruction remains applicable to the relevant legal parties and period; "
                "this calculation alone is not an invoice or a legal-entitlement determination."
            )
        elif intent == "explanation":
            answer = (
                "No. The same allocation is returned only when the question asks about the same approved "
                f"pro-rata instruction for {period}, because its inputs are fixed: {currency} {fee_pool:,.2f} "
                f"and eligible nominal amounts totaling {self._document_decimal(total_eligible)}. "
                "A verification question should receive the calculation steps; a question about a different "
                "period, fee pool, participant, or legal entitlement needs matching document evidence. "
                "This remains a local calculation preview, not an invoice or a legal-entitlement determination."
            )
        elif intent == "allocation":
            undetermined = [
                str(participant["name"]) for participant in payload["participants"]  # type: ignore[index, union-attr]
                if participant["status"] == "undetermined"
            ] if payload is not None else []
            conflicts = [
                check for check in payload["crossChecks"]  # type: ignore[index, union-attr]
                if check["status"] == "conflict"
            ] if payload is not None else []
            answer = (
                f"Using the approved document-backed pro-rata instruction for {period}, the provisional allocation "
                f"of {currency} {fee_pool:,.2f} is {allocations_text}. "
                "The instruction applies eligible nominal amount divided by total eligible nominal amount, with "
                f"currency rounding to reconcile to the fee pool.{exclusion}"
                + (
                    f" Not named in the instruction, so eligibility is undetermined: {', '.join(undetermined)}."
                    if undetermined else ""
                )
                + "".join(
                    f" Unresolved discrepancy: {check['source']['filename']} ({check['source']['reference']}) records "  # type: ignore[index]
                    f"{self._document_decimal(Decimal(str(check['recordedAmount'])))} for {check['participant']} as of "
                    f"{check['asOfDate']}, while the instruction uses {self._document_decimal(Decimal(str(check['instructionAmount'])))}; "
                    "the instruction amount was used unchanged."
                    for check in conflicts
                )
                + " This is a local calculation preview, not an invoice or a determination of legal entitlement."
            )
        else:
            participant_labels = ", ".join(row.label for row in rows)
            answer = (
                f"The selected material contains one approved document-backed pro-rata instruction for {period}. "
                f"It sets a {currency} {fee_pool:,.2f} fee pool and uses eligible nominal amount divided by total "
                f"eligible nominal amount for the listed eligible participants: {participant_labels}.{exclusion} "
                "Ask to calculate the provisional allocation to receive the documented amounts, or ask how to "
                "verify the billing for the recomputation steps. This is a local calculation preview, not an "
                "invoice or a determination of legal entitlement."
            )
        instruction_context = (
            f"Approved pro-rata billing instruction. Billing period: {period}. Fee pool: {currency} {fee_pool:,.2f}. "
            "Allocation rule: eligible nominal amount / total eligible nominal amount × fee pool, with currency "
            "rounding to reconcile to the fee pool. Eligible participants: "
            + "; ".join(
                f"{row.label} has eligible nominal amount {self._document_decimal(row.eligible_amount)}"
                for row in rows
            )
            + "."
            + exclusion
            + " This is an instruction and evidence context, not an invoice or a legal-entitlement determination."
        )
        return DocumentBillingAnswer(
            answer=answer,
            citations=tuple(
                (
                    str(source["document_id"]), str(source["extraction_id"]),
                    str(source["source_reference"]), str(source["original_filename"]),
                )
                for source in citation_sources
            ) + tuple(dict.fromkeys(cross_citations)),
            instruction_context=instruction_context,
            payload=payload,
        )

    def _incomplete_billing_answer(
        self,
        instructions: Sequence[tuple[list[sqlite3.Row], str, Decimal, list[AllocationRow], str]],
        incomplete_pages: Sequence[list[sqlite3.Row]],
    ) -> DocumentBillingAnswer:
        """Say why no allocation was calculated; never choose or complete an instruction."""
        pages = [page for page, *_inputs in instructions] if instructions else list(incomplete_pages)
        filenames = sorted({str(page[0]["original_filename"]) for page in pages})
        missing: list[str] = []
        issues: list[str] = []
        if len(instructions) > 1:
            issues.append(
                f"{len(instructions)} approved billing instructions were found ({', '.join(filenames)}). "
                "ClaimFlow does not choose between billing instructions."
            )
            answer = (
                f"No allocation was calculated: {len(instructions)} approved billing instructions were found "
                f"({', '.join(filenames)}), and ClaimFlow does not choose between them. Narrow the question to one "
                "instruction document."
            )
        elif len(pages) == 1:
            missing = billing_missing_inputs("\n".join(str(source["text"]) for source in pages[0])) or [
                "inputs in a form that could be read exactly"
            ]
            answer = (
                f"No allocation was calculated: the billing instruction in {filenames[0]} does not state "
                + "; ".join(missing) + ". No amount was assumed for the missing inputs."
            )
        else:
            issues.append(
                f"{len(pages)} pages look like billing instructions ({', '.join(filenames)}), but none states every "
                "input a document-backed allocation needs."
            )
            answer = (
                f"No allocation was calculated: {len(pages)} pages look like billing instructions, but none states "
                "every required input. No amount was assumed for the missing inputs."
            )
        sources = [source for page in pages for source in page]
        blocks = [
            InstructionBlock(
                str(source["document_id"]), str(source["extraction_id"]), str(source["original_filename"]),
                str(source["source_reference"]), str(source["text"]),
            )
            for source in pages[0]
        ]
        single = instructions[0] if len(instructions) == 1 else None
        payload = allocation_payload(
            blocks, currency=single[1] if single else None, fee_pool=single[2] if single else None,
            period=single[4] if single else None, missing=missing, issues=issues,
        )
        cited = [
            source for source in sources
            if re.match(r"^\s*(Fee\s+pool|Allocation\s+rule|Eligible\s+participant|Billing\s+period)\b", str(source["text"]), flags=re.IGNORECASE)
        ] or sources
        return DocumentBillingAnswer(
            answer=answer,
            citations=tuple(
                (
                    str(source["document_id"]), str(source["extraction_id"]),
                    str(source["source_reference"]), str(source["original_filename"]),
                )
                for source in cited
            ),
            instruction_context="",
            payload=payload,
        )

    @staticmethod
    def _payload_sources(payload: Mapping[str, object]) -> list[Mapping[str, str]]:
        """Every instruction source a billing payload points to."""
        sources: list[object] = [
            (payload.get(key) or {}).get("source")  # type: ignore[union-attr]
            for key in ("approvedBy", "investorGroup", "period", "feePool", "rule")
        ]
        sources += [participant.get("source") for participant in payload.get("participants", [])]  # type: ignore[union-attr]
        return [source for source in sources if isinstance(source, Mapping)]

    @staticmethod
    def _document_billing_intent(question: str) -> str:
        """Choose a response form without treating a billing keyword as a calculation request."""
        query = question.casefold()
        if any(term in query for term in ("verify", "verification", "validate", "validation", "audit", "reconcile", "check the billing")):
            return "verification"
        if "what do you mean" in query or (
            "same" in query and any(term in query for term in ("answer", "output", "result", "response"))
        ) or any(term in query for term in (
            "why the same", "why is it the same", "repeat the allocation", "repeating the allocation",
        )):
            return "explanation"
        if any(term in query for term in (
            "calculate", "calculation", "allocate", "allocation", "how much", "show me the amount",
            "give me the amount", "generate the allocation", "prepare the allocation", "split the fee",
        )):
            return "allocation"
        return "overview"

    @staticmethod
    def _document_decimal(value: Decimal) -> str:
        """Render a document amount without inventing decimal precision."""
        return f"{value:,.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _parse_document_billing_instruction(
        source_text: str,
    ) -> tuple[str, Decimal, list[AllocationRow], str] | None:
        """Recognise one explicit instruction without turning document prose into policy."""
        normalized = " ".join(source_text.split())
        folded = normalized.casefold()
        if not all(term in folded for term in (
            "approved", "fee pool", "allocation rule", "eligible participant",
            "eligible nominal amount / total eligible nominal amount",
        )):
            return None
        money = re.search(r"\b([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b", source_text)
        period = re.search(r"Billing\s+period\s*\n\s*([^\n]+)", source_text, flags=re.IGNORECASE)
        participants = re.findall(
            r"Eligible\s+participant(?:\s+\d+)?\s*\n\s*([^\n(]+?)\s*\(([^)]+)\)\s*:\s*([0-9][0-9,]*(?:\.[0-9]{1,6})?)",
            source_text, flags=re.IGNORECASE,
        )
        if money is None or period is None or not participants:
            return None
        try:
            fee_pool = Decimal(money.group(2).replace(",", ""))
            rows = [
                AllocationRow(f"document-{index}", f"{name.strip()} ({account.strip()})", Decimal(amount.replace(",", "")))
                for index, (name, account, amount) in enumerate(participants, start=1)
            ]
        except InvalidOperation:
            return None
        if fee_pool <= 0 or any(row.eligible_amount <= 0 for row in rows):
            return None
        return money.group(1), fee_pool, rows, period.group(1).strip()

    @staticmethod
    def _allocate_pro_rata(fee_pool: Decimal, rows: Sequence[AllocationRow]) -> list[tuple[AllocationRow, Decimal]]:
        """Allocate in cents with a stable largest-remainder tie-breaker."""
        total = sum((row.eligible_amount for row in rows), Decimal("0"))
        if total <= 0:
            raise MatterReviewError("At least one eligible basis must be greater than zero.")
        pool_cents = int((fee_pool / _CENT).to_integral_exact())
        raw_cents = [(row, Decimal(pool_cents) * row.eligible_amount / total) for row in rows]
        allocated_cents = {row.identifier: int(value.to_integral_value(rounding=ROUND_DOWN)) for row, value in raw_cents}
        residual = pool_cents - sum(allocated_cents.values())
        for row, _value in sorted(
            raw_cents,
            key=lambda item: (-(item[1] - item[1].to_integral_value(rounding=ROUND_DOWN)), item[0].identifier),
        )[:residual]:
            allocated_cents[row.identifier] += 1
        return [(row, Decimal(allocated_cents[row.identifier]) * _CENT) for row in rows]

    def analysis_batch_preview(self, params: object) -> Mapping[str, object]:
        """Prepare local-only per-document analysis scopes and aggregate cost.

        This intentionally calls only ``AIAnalysisService.preview``.  It does
        not contact a provider, create analysis jobs, or transfer content.
        """
        document_ids = self._batch_document_ids(params, require_acknowledgement=False)
        documents = self._source_documents(document_ids)
        results: list[dict[str, object]] = []
        estimated_total = Decimal("0")
        codex_plan = False
        all_ready = True
        global_authorization = False
        configured = False
        for document in documents:
            document_id = str(document["id"])
            try:
                preview = self._analysis.preview({"documentId": document_id})
                cost = preview.get("cost")
                codex_plan = preview.get("provider") == "codex"
                estimated_cost = cost.get("estimatedCostUsd") if isinstance(cost, dict) else None
                within_document_limit = bool(cost.get("withinLimit")) if isinstance(cost, dict) else False
                if isinstance(estimated_cost, str):
                    estimated_total += Decimal(estimated_cost)
                elif not codex_plan:
                    all_ready = False
                ready = bool(preview.get("configured")) and within_document_limit and (codex_plan or isinstance(estimated_cost, str))
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
            "estimatedCostUsd": None if codex_plan else self._decimal_string(estimated_total, places=6),
            "costLimitUsd": None if codex_plan else self._decimal_string(limit, places=2),
            "withinCostLimit": codex_plan or estimated_total <= limit,
            "ready": all_ready and (codex_plan or estimated_total <= limit) and global_authorization and configured,
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
            raise MatterReviewError("Connect the selected AI provider in Settings.")
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
            estimated_cost_usd=preview["estimatedCostUsd"],
            cost_limit_usd=preview["costLimitUsd"],
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
                    json.dumps({"document_count": len(document_ids), "provider": "codex" if preview["estimatedCostUsd"] is None else "openai", "payload_logged": False}),
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
        """Plan a bounded, explicitly scoped matter question without a provider request."""
        request = self._matter_question_params(params, acknowledged=False)
        scope, plan, _follow_up_document_ids = self._prepare_question(request)
        return self._question_preview_payload(scope, plan)

    def _prepare_question(self, request: MatterQuestionRequest) -> tuple[DocumentScope, dict[str, object], frozenset[str]]:
        """Resolve scope, question-level document constraints, follow-up context, then plan.

        Every answer is planned from its own retrieval: an earlier answer is
        at most non-evidence context, and only when its source documents lie
        within the documents this question may use.
        """
        scope = self._resolve_scope(request)
        follow_up, follow_up_document_ids = self._follow_up_context(request.follow_up_question_id, scope)
        constraint = self._document_constraint(request.question, scope)
        notes: list[str] = []
        working = scope
        if constraint.requested:
            if constraint.problems:
                resolved = "; ".join(f"“{item.filename}”" for item in constraint.resolved)
                answer = LocalAnswer(
                    self._question_intent(request.question).kind,
                    "The documents named in this question could not be resolved unambiguously, so nothing was searched "
                    "or sent. " + " ".join(constraint.problems)
                    + (f" Resolved so far: {resolved}." if resolved else ""),
                    (), "local-document-resolution",
                )
                plan = self._local_plan(answer, 0)
                plan["documentConstraint"] = constraint
                return scope, plan, frozenset()
            resolved_ids = {item.document_id for item in constraint.resolved}
            # Question-level references only narrow the authorized scope.
            working = DocumentScope(
                scope.matter_id, scope.mode, tuple(document for document in scope.documents if document.document_id in resolved_ids),
            )
            if follow_up_document_ids and not follow_up_document_ids <= resolved_ids:
                follow_up, follow_up_document_ids = None, frozenset()
                notes.append(
                    "The earlier answer was not reused as context because it relied on documents other than those named in this question."
                )
        plan = dict(self._matter_question_plan(
            working, request.question, follow_up, follow_up_document_ids, constrained=constraint.requested,
        ))
        plan["documentConstraint"] = constraint
        plan["notes"] = [*notes, *plan.get("notes", [])]  # type: ignore[misc]
        return scope, plan, follow_up_document_ids

    def _document_constraint(self, question: str, scope: DocumentScope) -> DocumentConstraint:
        """Match file names/dates against every matter document, marking scope membership."""
        with self._database.connection() as connection:
            rows = connection.execute("SELECT id, original_filename, file_format FROM source_documents").fetchall()
        return resolve_document_references(question, [
            CandidateDocument(str(row["id"]), str(row["original_filename"]), str(row["file_format"] or ""), str(row["id"]) in scope.document_ids)
            for row in rows
        ])

    def _question_preview_payload(self, scope: DocumentScope, plan: Mapping[str, object]) -> Mapping[str, object]:
        self._analysis._refresh_configuration()
        configuration = self._analysis._configuration
        controls = self._analysis._cost_controls
        document_scope = self._document_scope_payload(scope, plan)
        if isinstance(plan.get("documentBilling"), DocumentBillingAnswer) or isinstance(plan.get("localAnswer"), LocalAnswer):
            return {
                "intent": plan.get("intent"),
                "provider": None,
                "model": None,
                "globalAuthorization": self._analysis._global_authorized(),
                "configured": configuration.state() == "configured_not_live_tested",
                "localOnly": True,
                "scope": plan["scope"],
                "documentScope": document_scope,
                "cost": {
                    "estimatedInputTokens": 0,
                    "maxOutputTokens": 0,
                    "estimatedCostUsd": "0.000000",
                    "limitUsd": self._analysis._cost_string(controls.max_estimated_cost_usd),
                    "withinLimit": True,
                    "maxRequestAttempts": 0,
                },
            }
        estimate = self._analysis._estimate_cost(plan["inputTokens"], plan["maxOutputTokens"])
        estimated_cost = self._analysis._cost_string(estimate)
        codex_plan = configuration.provider == "codex"
        within_limit = codex_plan or (estimate is not None and estimate <= controls.max_estimated_cost_usd)
        return {
            "intent": plan.get("intent"),
            "provider": configuration.provider,
            "model": configuration.model,
            "globalAuthorization": self._analysis._global_authorized(),
            "configured": configuration.state() == "configured_not_live_tested",
            "localOnly": False,
            "scope": plan["scope"],
            "documentScope": document_scope,
            "cost": {
                "estimatedInputTokens": plan["inputTokens"],
                "maxOutputTokens": 0 if codex_plan else plan["maxOutputTokens"],
                "estimatedCostUsd": None if codex_plan else estimated_cost,
                "limitUsd": None if codex_plan else self._analysis._cost_string(controls.max_estimated_cost_usd),
                "withinLimit": within_limit,
                "maxRequestAttempts": 1 if codex_plan else controls.max_request_attempts,
            },
        }

    def ask_question(self, params: object) -> Mapping[str, object]:
        """Start one consent-gated, in-memory matter question.

        Unlike document analysis, this never writes a proposed domain record;
        question text and answer remain in memory for the current backend run.
        The scope is resolved again here rather than trusted from a preview,
        and every document whose excerpt would be transmitted must be named
        in this request's explicit consent list.
        """
        request = self._matter_question_params(params, acknowledged=True)
        scope, plan, follow_up_document_ids = self._prepare_question(request)
        document_scope = self._document_scope_payload(scope, plan)
        searchable_ids = tuple(document.document_id for document in scope.searchable)
        local_answer = plan.get("localAnswer")
        if isinstance(local_answer, LocalAnswer):
            # Built locally from stored extractions; nothing is transmitted.
            citations = self._validate_direct_matter_citations(local_answer.citations)
            job = MatterQuestion(
                id=str(uuid.uuid4()), document_ids=searchable_ids, status="completed", scope=plan["scope"],
                answer=local_answer.answer, citations=tuple(citations), model=local_answer.model,
                matter_id=scope.matter_id, question=request.question, document_scope=document_scope,
                context_document_ids=frozenset(citation["documentId"] for citation in citations) | follow_up_document_ids,
                intent=local_answer.intent, inventory=local_answer.inventory, holdings=local_answer.holdings,
            )
            with self._question_lock:
                self._questions[job.id] = job
            return self.question_status({"questionId": job.id})
        document_billing = plan.get("documentBilling")
        if isinstance(document_billing, DocumentBillingAnswer):
            citations = self._validate_direct_matter_citations(document_billing.citations)
            if not citations:
                raise MatterReviewError("The approved billing instruction could not be revalidated locally.")
            job = MatterQuestion(
                id=str(uuid.uuid4()), document_ids=searchable_ids, status="completed", scope=plan["scope"],
                answer=document_billing.answer, citations=tuple(citations), model="local-document-calculation",
                matter_id=scope.matter_id, question=request.question, document_scope=document_scope,
                context_document_ids=frozenset(citation["documentId"] for citation in citations) | follow_up_document_ids,
                intent=INTENT_CALCULATION, billing=document_billing.payload,
            )
            with self._question_lock:
                self._questions[job.id] = job
            return self.question_status({"questionId": job.id})
        preview = self._question_preview_payload(scope, plan)
        if not preview["globalAuthorization"]:
            raise MatterReviewError("Enable cloud-processing authorization in Settings before asking a consolidated question.")
        if not preview["configured"]:
            raise MatterReviewError("Connect the selected AI provider in Settings.")
        cost = preview["cost"]
        if not isinstance(cost, dict) or not cost["withinLimit"]:
            raise MatterReviewError("This question exceeds the current development safety limit.")
        transmitted = frozenset(str(value) for value in plan["transmittedDocumentIds"])  # type: ignore[union-attr]
        if not request.consented_document_ids <= scope.document_ids:
            raise MatterReviewError("Consent can only cover documents in the current question scope.")
        if not transmitted <= request.consented_document_ids:
            raise MatterReviewError(
                "The documents whose excerpts would be sent changed after you reviewed this question. "
                "Review the documents listed for this question and confirm again."
            )
        with self._question_lock:
            if any(job.status in {"queued", "running"} for job in self._questions.values()):
                raise MatterReviewError("Another consolidated question is already being answered.")
            job = MatterQuestion(
                id=str(uuid.uuid4()), document_ids=searchable_ids, status="queued", scope=plan["scope"],
                matter_id=scope.matter_id, question=request.question, document_scope=document_scope,
                context_document_ids=transmitted, intent=str(plan.get("intent") or "general"),
                holdings=plan.get("holdingsEvidence") if isinstance(plan.get("holdingsEvidence"), Mapping) else None,
            )
            self._questions[job.id] = job
        try:
            self._record_question_consent(job.id, scope, transmitted, str(preview["provider"]))
        except BaseException:
            # Never leave an unsent question queued: it would block later questions.
            with self._question_lock:
                self._questions.pop(job.id, None)
            raise
        worker = threading.Thread(
            target=self._run_matter_question,
            args=(job.id, request.question, plan), daemon=True, name=f"group-claims-matter-question-{job.id[:8]}"
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
                "matterId": job.matter_id,
                "status": job.status,
                "answer": job.answer,
                "unknown": job.unknown,
                "citations": list(job.citations),
                "errorCode": job.error_code,
                "model": job.model,
                "intent": job.intent,
                "inventory": dict(job.inventory) if job.inventory is not None else None,
                "holdingsComparison": dict(job.holdings) if job.holdings is not None else None,
                "billingAllocation": dict(job.billing) if job.billing is not None else None,
                "scope": dict(job.scope),
                "documentScope": dict(job.document_scope or {}),
                "usage": {
                    "inputTokens": job.input_tokens,
                    "outputTokens": job.output_tokens,
                    "totalTokens": job.total_tokens,
                    "actualCostEstimateUsd": job.actual_cost_estimate_usd,
                },
            }

    def _resolve_scope(self, request: MatterQuestionRequest) -> DocumentScope:
        """Resolve the permitted documents from SQLite before any retrieval.

        Renderer-supplied identifiers are only a request: each selected ID
        must belong to the named matter, and all-document scope is derived
        from the matter's own document records rather than from the client.
        """
        if request.matter_id != WORKSPACE_MATTER_ID:
            raise MatterReviewError("The requested matter is unavailable. Reopen the matter and try again.")
        self._analysis._refresh_configuration()
        provider = self._analysis._configuration.provider
        latest_extraction = (
            "(SELECT e.id FROM document_extractions e WHERE e.source_document_id = d.id AND e.status = 'parsed' "
            "ORDER BY e.started_at DESC, e.id DESC LIMIT 1)"
        )
        with self._database.transaction() as connection:
            if request.mode == "selected":
                placeholders = ",".join("?" for _ in request.selected_document_ids)
                rows = connection.execute(
                    f"SELECT d.id, d.original_filename, d.processing_status, {latest_extraction} AS extraction_id "
                    f"FROM source_documents d WHERE d.id IN ({placeholders})",
                    list(request.selected_document_ids),
                ).fetchall()
                if len(rows) != len(request.selected_document_ids):
                    raise MatterReviewError("One or more selected documents do not belong to this matter.")
                by_id = {str(row["id"]): row for row in rows}
                rows = [by_id[document_id] for document_id in request.selected_document_ids]
            else:
                rows = connection.execute(
                    f"SELECT d.id, d.original_filename, d.processing_status, {latest_extraction} AS extraction_id "
                    f"FROM source_documents d ORDER BY d.imported_at DESC, d.id DESC"
                ).fetchall()
            parsed = {
                str(row["extraction_id"]): str(row["id"])
                for row in rows
                if row["processing_status"] == "parsed" and isinstance(row["extraction_id"], str)
            }
            index_states = DocumentSearchIndex.ensure_indexed(connection, parsed)
            authorized: set[str] = set()
            if rows and provider:
                document_ids = [str(row["id"]) for row in rows]
                placeholders = ",".join("?" for _ in document_ids)
                # Earlier consent is honoured only for the same provider and
                # only since cloud processing was most recently enabled.
                authorized = {
                    str(row["target_id"]) for row in connection.execute(
                        f"SELECT DISTINCT target_id FROM audit_events WHERE target_type = 'source_document' "
                        f"AND event_type IN ('ai_document_analysis_authorized', ?) AND target_id IN ({placeholders}) "
                        f"AND json_extract(details_json, '$.provider') = ? AND occurred_at >= COALESCE(("
                        f"SELECT occurred_at FROM ai_authorizations WHERE authorized = 1 "
                        f"ORDER BY occurred_at DESC LIMIT 1), '9999')",
                        [_DOCUMENT_CONSENT_EVENT, *document_ids, provider],
                    ).fetchall()
                }
        documents: list[ScopedDocument] = []
        for row in rows:
            document_id = str(row["id"])
            extraction_id = row["extraction_id"] if isinstance(row["extraction_id"], str) else None
            state = index_states.get(extraction_id) if extraction_id is not None else None
            if row["processing_status"] != "parsed":
                reason: str | None = _UNAVAILABLE_REASONS.get(str(row["processing_status"]), "extraction_unavailable")
            elif state is None:
                reason = "extraction_unavailable"
            elif state.item_count == 0:
                reason = "no_searchable_text"
            else:
                reason = None
            documents.append(ScopedDocument(
                document_id=document_id,
                filename=str(row["original_filename"]),
                extraction_id=extraction_id if reason is None else None,
                unavailable_reason=reason,
                incomplete=bool(state and state.unavailable_page_count > 0),
                previously_authorized=document_id in authorized,
            ))
        return DocumentScope(request.matter_id, request.mode, tuple(documents))

    def _follow_up_context(
        self, question_id: str | None, scope: DocumentScope,
    ) -> tuple[Mapping[str, str] | None, frozenset[str]]:
        """Reuse an earlier turn only when its source documents remain in scope.

        The renderer names the earlier turn; it cannot supply the prior
        question or answer text.  A follow-up after narrowing the scope would
        otherwise transmit an answer derived from now-excluded documents.
        """
        if question_id is None:
            return None, frozenset()
        with self._question_lock:
            job = self._questions.get(question_id)
            if job is None or job.status != "completed":
                raise MatterReviewError("The earlier answer is unavailable. Ask this as a new question.")
            parent_question, parent_answer = job.question, job.answer
            parent_unknown, parent_documents, parent_matter = job.unknown, job.context_document_ids, job.matter_id
            # Names an earlier billing answer flagged as conflicting, so "explain
            # that discrepancy" can be resolved.  Only names: the figures are
            # read again from the documents.
            parent_subjects = [
                str(check.get("participant") or "") for check in (job.billing or {}).get("crossChecks", [])  # type: ignore[union-attr]
                if isinstance(check, Mapping) and check.get("status") == "conflict"
            ]
        if parent_matter != scope.matter_id:
            raise MatterReviewError("The earlier answer belongs to another matter. Ask this as a new question.")
        if not parent_documents <= scope.document_ids:
            raise MatterReviewError(
                "This follow-up would reuse an answer based on documents outside the current scope. "
                "Ask it as a new question in this scope instead."
            )
        answer = parent_answer if parent_answer and not parent_unknown else _UNKNOWN_FOLLOW_UP_ANSWER
        return {
            "question": parent_question[:600], "answer": answer[:1200],
            "subjects": "\n".join(dict.fromkeys(name for name in parent_subjects if name)),
        }, parent_documents

    def _record_question_consent(
        self, question_id: str, scope: DocumentScope, transmitted: frozenset[str], provider: str,
    ) -> None:
        """Audit the explicit document-level consent without storing content or the question."""
        # Same ISO format as ``ai_authorizations`` so the "since cloud
        # processing was last enabled" comparison is a valid string order.
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (id, event_type, target_type, target_id, actor_type, occurred_at, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()), "ai_matter_question_authorized", "matter", scope.matter_id, "user", now,
                    json.dumps({
                        "provider": provider, "scope_mode": scope.mode, "document_count": len(transmitted),
                        "question_reference": question_id, "payload_logged": False,
                    }),
                ),
            )
            connection.executemany(
                "INSERT INTO audit_events (id, event_type, target_type, target_id, actor_type, occurred_at, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(uuid.uuid4()), _DOCUMENT_CONSENT_EVENT, "source_document", document_id, "user", now,
                        json.dumps({"provider": provider, "scope_mode": scope.mode, "payload_logged": False}),
                    )
                    for document_id in sorted(transmitted)
                ],
            )

    @staticmethod
    def _document_scope_payload(scope: DocumentScope, plan: Mapping[str, object]) -> Mapping[str, object]:
        constraint = plan.get("documentConstraint")
        transmitted = set(str(value) for value in plan.get("transmittedDocumentIds", ()))  # type: ignore[union-attr]
        evidence = set(str(value) for value in plan.get("evidenceDocumentIds", ()))  # type: ignore[union-attr]
        return {
            "matterId": scope.matter_id,
            "mode": scope.mode,
            "documentsInScope": len(scope.documents),
            "searchableDocuments": len(scope.searchable),
            "unavailableDocuments": [
                {"documentId": document.document_id, "filename": document.filename, "reason": document.unavailable_reason}
                for document in scope.documents if document.unavailable_reason is not None
            ],
            "incompleteDocuments": [
                {"documentId": document.document_id, "filename": document.filename}
                for document in scope.searchable if document.incomplete
            ],
            "documentsWithEvidence": len(evidence),
            "transmittedDocuments": [
                {
                    "documentId": document.document_id,
                    "filename": document.filename,
                    "previouslyAuthorized": document.previously_authorized,
                }
                for document in scope.documents if document.document_id in transmitted
            ],
            "searchTruncated": bool(plan.get("searchTruncated", False)),
            # Documents a question named explicitly (by file name or date).
            "resolvedDocuments": [
                {"documentId": item.document_id, "filename": item.filename, "matchedBy": item.matched_by}
                for item in (constraint.resolved if isinstance(constraint, DocumentConstraint) else ())
            ],
            "notes": [str(note) for note in plan.get("notes", ())],  # type: ignore[union-attr]
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
            if configuration.provider not in {"openai", "codex"} or not configuration.credential_configured:
                raise AIAnalysisError("AI_PROVIDER_NOT_CONFIGURED", "Connect the selected AI provider in Settings.")
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
            # Dated holding cells behind a legal-status answer are cited too,
            # re-resolved locally, whichever excerpt the model chose.
            cited = {(citation["documentId"], citation["sourceReference"]) for citation in citations}
            for citation in self._validate_direct_matter_citations(plan.get("holdingsCitations", ())):  # type: ignore[arg-type]
                if (citation["documentId"], citation["sourceReference"]) not in cited:
                    citations.append(citation)
                    cited.add((citation["documentId"], citation["sourceReference"]))
            controls = self._analysis._cost_controls
            actual_cost = self._analysis._estimate_cost(result.input_tokens, result.output_tokens) if result.input_tokens is not None and result.output_tokens is not None else None
            with self._question_lock:
                job = self._questions.get(question_id)
                if job is None:
                    return
                job.status = "completed"
                job.answer = self._humanize_citation_keys(str(result.answer or ""), plan["citationTargets"])
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
        scope: DocumentScope,
        question: str,
        follow_up: Mapping[str, str] | None = None,
        follow_up_document_ids: frozenset[str] = frozenset(),
        *,
        constrained: bool = False,
    ) -> Mapping[str, object]:
        """Build a bounded prompt context from the resolved scope only.

        Every query below is restricted to the current extraction of a
        document already validated by ``_resolve_scope``; there is no
        unscoped document query on this path.
        """
        documents = scope.searchable
        if not documents:
            raise MatterReviewError(
                "No document in this scope has searchable extracted content. "
                "Parse or reprocess the documents listed as unavailable, or change the scope."
            )
        document_ids = [document.document_id for document in documents]
        names = {document.document_id: document.filename for document in documents}
        current_extractions = {str(document.extraction_id): document.document_id for document in documents}
        extraction_ids = list(current_extractions)
        intent = self._question_intent(question)
        if intent.kind == INTENT_HOLDINGS_RECONSTRUCTION:
            reconstruction = self._reconstruction_plan(documents, question, intent, follow_up)
            if reconstruction is not None:
                return reconstruction
            # No subject with dated records in scope: answer it as before.
            intent = self._question_intent(question, reconstruction=False)
        if intent.kind in {INTENT_INVENTORY, INTENT_RELATIONSHIP}:
            return self._inventory_plan(documents, intent)
        if intent.kind == INTENT_HOLDINGS_COMPARISON:
            return self._holdings_plan(documents, intent, question, constrained=constrained)
        legal_holdings = (
            self._legal_holdings_evidence(documents, question)
            if intent.kind == INTENT_LEGAL_STATUS and _HOLDINGS_MOVEMENT_CUE.search(question.casefold()) else None
        )
        if legal_holdings is not None and legal_holdings.local_answer is not None:
            return self._local_plan(legal_holdings.local_answer, len(documents))
        with self._database.connection() as connection:
            present_names, missing_names = self._named_entities(connection, extraction_ids, question)
        required_terms = sorted({*(identifier.casefold() for identifier in intent.identifiers), *(name.casefold() for name in present_names)})
        notes = [f"Not found in the documents used: “{name}”." for name in missing_names]
        if missing_names and not required_terms:
            return self._local_plan(LocalAnswer(
                intent.kind,
                "None of the names in this question appear in the searchable documents used ("
                + ", ".join(f"“{name}”" for name in missing_names)
                + "). Nothing was sent. Check the spelling, name the relevant document, or change the scope.",
                (), "local-relevance-check",
            ), len(documents), notes=notes)
        billing_instruction = self._document_billing_answer(documents, question)
        # The deterministic calculator owns only an explicit allocation
        # request. Every other billing conversation goes through the
        # question-aware provider with this instruction as cited evidence.
        document_billing = (
            billing_instruction
            if billing_instruction is not None and self._document_billing_intent(question) == "allocation"
            else None
        )
        placeholders = ",".join("?" for _ in document_ids)
        extraction_placeholders = ",".join("?" for _ in extraction_ids)
        search_truncated = False
        # Earlier AI review output is used only when it was produced from the
        # document's current extraction, so evidence versions stay consistent.
        latest_current_job = (
            "j.id = (SELECT newer.id FROM ai_processing_jobs newer WHERE newer.source_document_id = j.source_document_id "
            "AND newer.extraction_id = j.extraction_id AND newer.status = 'completed' "
            "ORDER BY newer.created_at DESC, newer.id DESC LIMIT 1)"
        )
        with self._database.connection() as connection:
            summary_rows = connection.execute(
                f"SELECT j.source_document_id, j.extraction_id, j.summary_text, j.summary_unknown, j.summary_citations_json "
                f"FROM ai_processing_jobs j WHERE j.status = 'completed' AND {latest_current_job} "
                f"AND j.source_document_id IN ({placeholders}) AND j.extraction_id IN ({extraction_placeholders}) "
                f"ORDER BY j.source_document_id",
                [*document_ids, *extraction_ids],
            ).fetchall()
            rows = connection.execute(
                f"SELECT p.source_reference, p.evidence_snippet, p.proposal_type, p.field_name, p.value_json, p.is_unknown, "
                f"j.source_document_id, j.extraction_id FROM ai_proposals p JOIN ai_processing_jobs j ON j.id = p.job_id "
                f"WHERE p.validation_status = 'validated' AND j.status = 'completed' AND {latest_current_job} "
                f"AND j.source_document_id IN ({placeholders}) AND j.extraction_id IN ({extraction_placeholders}) "
                f"ORDER BY j.source_document_id, p.created_at, p.id LIMIT 41",
                [*document_ids, *extraction_ids],
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
            seen_excerpts: set[tuple[str, str]] = set()
            neighbour_context: dict[tuple[str, str], str] = {}

            def add_candidate(
                document_id: str, extraction_id: str, reference: str, description: str,
                *, prioritize_for_question: bool = False, source: Mapping[str, object] | None = None,
            ) -> None:
                if current_extractions.get(extraction_id) != document_id:
                    return
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
                # Repeated identical text in one document (for example a
                # header repeated on each worksheet) adds no new evidence.
                # Identical text in two documents is kept: that can be
                # corroboration or a conflict the reviewer must see.
                excerpt_identity = (document_id, excerpt.casefold())
                if excerpt_identity in seen_excerpts:
                    return
                seen_sources.add(identity)
                seen_excerpts.add(excerpt_identity)
                if source is not None and len(excerpt) < SHORT_EXCERPT_CHARACTERS:
                    context = self._neighbour_context(connection, source)
                    if context:
                        neighbour_context[identity] = context
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

            # Search every searchable document in scope, whether or not it has
            # an earlier AI review, using the local full-text index.  Only a
            # bounded, per-document-interleaved set of matching sources is
            # considered; each is re-resolved against the extraction tables.
            # Questions about explicit legal events *or* factual changes in
            # subfund/account/position snapshots get related excerpts.  The
            # latter must not be misclassified as a litigation-status question
            # merely because it contains the word "subfund".
            #
            # A follow-up may say "those" or "that holding".  The prior user
            # question improves source selection; the prior answer is never
            # treated as evidence.
            # An explicit follow-up ("what about those?") borrows the earlier
            # question's wording; one that names its own documents, entities
            # or identifiers is retrieved from its own terms only.
            explicit = constrained or bool(required_terms)
            retrieval_question = question if follow_up is None or explicit else f"{follow_up['question']}\n{question}"
            change_question = (
                self._is_change_question(retrieval_question)
                and not self._is_legal_status_question(retrieval_question)
            )
            terms = set(self._question_terms(retrieval_question))
            # A specific lookup searches for its literal identifiers (ISIN,
            # account number, quoted name) as well as its other terms.
            terms |= {identifier.casefold() for identifier in intent.identifiers}
            terms |= set(required_terms)
            if change_question:
                terms |= _SNAPSHOT_HEADING_TERMS
            search = DocumentSearchIndex.search(
                connection, extraction_ids, terms,
                per_document_limit=MAX_SEARCH_HITS_PER_DOCUMENT, total_limit=MAX_SEARCH_HITS,
            )
            search_truncated = search.truncated
            raw_sources = self._ordered_sources(
                connection, search.hits, retrieval_question, expand_snapshots=change_question,
            )
            snapshot_rows: set[tuple[str, str, int]] = set()
            if change_question:
                # Spreadsheet values are stored one cell at a time.  If a
                # heading or an event-bearing cell is relevant, include that
                # row and the next two rows so the model sees the associated
                # account, quantity, date, or identifier.  It still receives
                # and cites individual stored cells only.
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

            # Relevance gate, applied before the excerpt cap: when the question
            # names entities or identifiers, keep only text that mentions one,
            # cells in a row that does, and the heading rows above such rows.
            required_rows: set[tuple[str, object, object]] = set()
            required_sheets: dict[tuple[str, object], int] = {}
            for source in raw_sources:
                if required_terms and isinstance(source["row_number"], int) and any(
                    term in str(source["text"] or "").casefold() for term in required_terms
                ):
                    sheet_key = (str(source["extraction_id"]), source["sheet_name"])
                    required_rows.add((*sheet_key, source["row_number"]))
                    required_sheets[sheet_key] = max(required_sheets.get(sheet_key, 0), int(source["row_number"]))

            def passes_gate(source: Mapping[str, object]) -> bool:
                if not required_terms:
                    return True
                text = str(source["text"] or "").casefold()
                if any(term in text for term in required_terms):
                    return True
                sheet_key = (str(source["extraction_id"]), source["sheet_name"])
                row_number = source["row_number"]
                if (*sheet_key, row_number) in required_rows:
                    return True
                return (
                    isinstance(row_number, int) and row_number < required_sheets.get(sheet_key, 0)
                    and self._is_snapshot_heading(text)
                )

            for source in raw_sources:
                if len(reconciliation_candidates) >= MAX_RECONCILIATION_EXCERPTS:
                    break
                if not passes_gate(source):
                    continue
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
                    current_extractions[extraction_id], extraction_id, str(source["source_reference"]),
                    (
                        "QUESTION-RELEVANT CHANGE/SNAPSHOT EXCERPT"
                        if in_snapshot_context else "QUESTION-RELEVANT SOURCE EXCERPT"
                    ),
                    prioritize_for_question=True,
                    source=None if in_snapshot_context else source,
                )

            if required_terms:
                # Earlier AI-review summaries and findings pass the same gate:
                # their cell row or text block must mention a named entity or
                # identifier. (Source excerpts were gated above.)
                candidates = [
                    candidate for candidate in candidates
                    if self._mentions_required_term(connection, candidate, required_terms, neighbour_context)
                ]
            candidates = reconciliation_candidates + candidates
            if required_terms:
                if not candidates and billing_instruction is None and legal_holdings is None:
                    return self._local_plan(LocalAnswer(
                        intent.kind,
                        "The searchable documents used mention "
                        + ", ".join(f"“{term}”" for term in [*present_names, *intent.identifiers])
                        + " only in passages that could not be retrieved as citable evidence for this question. "
                        "Nothing was sent. Ask about a specific document or a narrower point.",
                        (), "local-relevance-check",
                    ), len(documents), notes=notes)
            if billing_instruction is not None:
                # The parser has already validated the exact instruction
                # inputs. Provide them to the AI as evidence rather than
                # bypassing the question-answering path with a canned answer.
                billing_sources = {
                    (document_id, reference)
                    for document_id, _extraction_id, reference, _filename in billing_instruction.citations
                }
                candidates = [
                    candidate for candidate in candidates
                    if (candidate[0], candidate[2]) not in billing_sources
                ]
                for document_id, extraction_id, reference, filename in reversed(billing_instruction.citations):
                    candidates.insert(
                        0,
                        (
                            document_id,
                            extraction_id,
                            reference,
                            filename,
                            "DOCUMENT-BACKED BILLING INSTRUCTION; EVIDENCE: "
                            + billing_instruction.instruction_context,
                        ),
                    )

            if legal_holdings is not None:
                # The dated holding records come first; other excerpts (for
                # example an assignment notice) follow within the budget.
                pinned = legal_holdings.candidate
                candidates = [pinned, *(candidate for candidate in candidates if (candidate[0], candidate[2]) != (pinned[0], pinned[2]))]

        total = len(candidates)
        if not candidates and document_billing is None:
            raise MatterReviewError(
                "No relevant extracted evidence was found in the searchable documents in this scope. "
                "Rephrase the question with terms used in the documents, or change the scope."
            )

        if document_billing is not None:
            self._analysis._refresh_configuration()
            controls = self._analysis._cost_controls
            return {
                "context": "",
                "citationKeys": (),
                "citationTargets": {},
                "documentBilling": document_billing,
                "localFallback": None,
                "comparisonCoverageComplete": True,
                "intent": INTENT_CALCULATION,
                "inputTokens": 0,
                "maxOutputTokens": 0,
                # A local calculation transmits nothing to a provider.
                "transmittedDocumentIds": (),
                "evidenceDocumentIds": tuple(sorted({citation[0] for citation in document_billing.citations})),
                "searchTruncated": search_truncated,
                "scope": {
                    "sourceItems": len(document_billing.citations),
                    "totalSourceItems": len(document_billing.citations),
                    "excludedSourceItems": 0,
                    "truncated": False,
                    "documentsSelected": len(document_ids),
                    "documentsWithEvidence": len({citation[0] for citation in document_billing.citations}),
                    "comparisonCoverageComplete": True,
                },
            }

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
        # Comparisons also need breadth: one excerpt per document first.
        if self._is_overview_question(question) or intent.kind == INTENT_COMPARISON:
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

        scope_label = (
            f"all {len(document_ids)} searchable documents in this matter"
            if scope.mode == "all" else f"only the {len(document_ids)} searchable documents the user selected"
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
                if description.startswith(("DOCUMENT-BACKED BILLING INSTRUCTION;", "DOCUMENT-BACKED HOLDINGS RECORD;")):
                    # This is a compact, deterministic transcription of a
                    # single cited instruction. Keep all of its inputs so a
                    # conversational answer is not based on a clipped fee
                    # pool or participant list.
                    compact_description = self._bounded_text(description, 1_600) or "Billing instruction"
                elif marker_index >= 0:
                    # Keep the literal source at the end even when an earlier
                    # AI summary is long; the fallback and the provider both
                    # need actual locally resolved evidence, not just a
                    # paraphrase of that earlier summary.
                    prefix = self._bounded_text(description[:marker_index], 250) or "SOURCE CONTEXT"
                    evidence = (self._bounded_text(description[marker_index + len(evidence_marker):], 240) or "").strip()
                    compact_description = f"{prefix}{evidence_marker} {evidence}"
                else:
                    compact_description = self._bounded_text(description, 500) or "Evidence excerpt"
                line = f"[{key}] DOCUMENT: {compact_filename}; SOURCE: {reference}; {compact_description}"
                nearby = neighbour_context.get((source_document_id, reference))
                if nearby:
                    # Neighbouring cells/blocks help interpret a short value
                    # (for example a bare quantity).  They are labelled so
                    # the key still identifies the one cited source.
                    line += f"\n  NEARBY CONTEXT FOR [{key}] (same source location; not separately citable): {nearby}"
                lines.append(line)
            evidence_document_ids = {target[0] for target in targets.values()}
            coverage_context = (
                f"DOCUMENT SCOPE: {scope_label}. Use only the excerpts below. If the question needs information "
                "from a document outside this scope, say that the current scope does not establish it.\n"
                f"QUESTION COVERAGE: Evidence excerpts represent {len(evidence_document_ids)} of {len(document_ids)} searchable documents in scope. "
                "Do not describe the result as a complete cross-document comparison unless all of them are represented.\n"
                "CONFLICTS: Excerpts from different documents are separate records. If they disagree, say so and cite "
                "one excerpt from each document instead of choosing between them."
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
            "intent": intent.kind,
            "notes": notes,
            # Locally calculated holding records shown and cited with the answer.
            "holdingsEvidence": legal_holdings.payload if legal_holdings is not None else None,
            "holdingsCitations": legal_holdings.citations if legal_holdings is not None else (),
            "evidenceDocumentIds": tuple(sorted(evidence_document_ids)),
            # Every document whose content can reach the provider in this
            # request: cited excerpts plus any earlier answer reused as
            # follow-up context.  Consent must cover all of them.
            "transmittedDocumentIds": tuple(sorted(evidence_document_ids | follow_up_document_ids)),
            "searchTruncated": search_truncated,
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

    @classmethod
    def _question_intent(cls, question: str, *, reconstruction: bool = True) -> QuestionIntent:
        return classify_question(
            question,
            calculation=cls._is_document_billing_question(question),
            legal_status=cls._is_legal_status_question(question),
            change=cls._is_change_question(question),
            reconstruction=reconstruction and is_holdings_reconstruction(question),
        )

    def _reconstruction_plan(
        self, documents: Sequence[ScopedDocument], question: str, intent: QuestionIntent,
        follow_up: Mapping[str, str] | None,
    ) -> Mapping[str, object] | None:
        """Reconstruct each named subject's holding from the documents in scope, locally.

        The subject comes from the question; a follow-up that only says "its
        holdings" or "that discrepancy" borrows the names of the earlier
        question or the earlier answer's flagged conflict.  Either way every
        figure, date and citation is read again from the documents here.
        """
        scope_documents = [
            BillingScopeDocument(
                document.document_id, document.filename, str(document.extraction_id),
                document.filename.rsplit(".", 1)[-1].casefold() in _SPREADSHEET_EXTENSIONS,
            )
            for document in documents if document.extraction_id is not None
        ]
        extraction_ids = [document.extraction_id for document in scope_documents]
        notes: list[str] = []
        with self._database.connection() as connection:
            names, _missing = self._named_entities(connection, extraction_ids, question)
            if not names and follow_up is not None:
                names, _missing = self._named_entities(connection, extraction_ids, follow_up.get("question", ""))
                names = names or [
                    name for name in follow_up.get("subjects", "").split("\n")
                    if name and DocumentSearchIndex.search(connection, extraction_ids, (name.casefold(),), per_document_limit=1, total_limit=1).hits
                ]
                if names:
                    notes.append(
                        "This follow-up was read as being about " + ", ".join(f"“{name}”" for name in names)
                        + " from the earlier question and answer. The figures below were read again from the documents."
                    )
            evidence = [
                found for found in (
                    gather_holdings_evidence(connection, scope_documents, HoldingsSubject(name))
                    for name in names[:MAX_RECONSTRUCTION_SUBJECTS]
                ) if found.found
            ]
        if not evidence:
            return None
        if len(names) > MAX_RECONSTRUCTION_SUBJECTS:
            notes.append(f"Only the first {MAX_RECONSTRUCTION_SUBJECTS} names in the question were reconstructed.")
        billing = (
            self._document_billing_answer(documents, question, intent="allocation")
            if self._is_document_billing_question(question) else None
        )
        payload = billing.payload if billing is not None else None
        dated = [date for date in question_dates(question) if date.year is not None]
        comparison = (payload or {}).get("crossCheck") or {}
        on_date = (
            f"{dated[0].year:04d}-{dated[0].month:02d}-{dated[0].day:02d}" if dated
            else str(comparison.get("comparisonDate") or "") or None  # type: ignore[union-attr]
        )
        isin = next((identifier for identifier in intent.identifiers if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", identifier)), None)
        sections: list[str] = []
        citations: list[tuple[str, str, str, str]] = []
        billing_sources = {(citation[0], citation[2]): citation for citation in (billing.citations if billing else ())}
        for found in evidence:
            result = reconstruct_holdings(found, on_date, isin)
            billing_lines, billing_cited = (
                reconstruction_billing_effect(found.subject, result, payload) if self._is_document_billing_question(question)
                else (None, [])
            )
            text, sources = reconstruction_text(found, result, billing_lines)
            sections.append(f"{found.subject.label}\n\n{text}" if len(evidence) > 1 else text)
            citations += [*sources, *(billing_sources[key] for key in billing_cited if key in billing_sources)]
        citations = list(dict.fromkeys(citations))
        return self._local_plan(LocalAnswer(
            INTENT_HOLDINGS_RECONSTRUCTION, "\n\n".join(sections), tuple(citations), "local-holdings-reconstruction",
            frozenset(citation[0] for citation in citations), any(found.truncated for found in evidence),
        ), len(documents), notes=notes)

    def _inventory_plan(self, documents: Sequence[ScopedDocument], intent: QuestionIntent) -> Mapping[str, object]:
        """Answer a listing or relationship question from every eligible document, locally.

        No top-K excerpt selection and no provider request: the inventory
        builder examines each in-scope document's indexed extraction within
        bounded local limits and reports any partial coverage.
        """
        relationships = intent.kind == INTENT_RELATIONSHIP
        roles = intent.roles | (
            frozenset({ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY, ROLE_SUBFUND, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND})
            if relationships else frozenset()
        )
        with self._database.connection() as connection:
            inventory = EntityInventoryBuilder(connection, [
                InventoryDocument(document.document_id, document.filename, str(document.extraction_id))
                for document in documents
            ]).build(roles, relationships=relationships)
        return self._local_plan(LocalAnswer(
            intent.kind, inventory.answer, inventory.citations,
            "local-relationship-mapping" if relationships else "local-entity-inventory",
            inventory.evidence_document_ids, inventory.partial, inventory=inventory.payload,
        ), len(documents))

    def _holdings_plan(
        self, documents: Sequence[ScopedDocument], intent: QuestionIntent, question: str, *, constrained: bool,
    ) -> Mapping[str, object]:
        """Compare dated holdings in exactly two spreadsheets, locally and exactly."""
        spreadsheets = [
            document for document in documents
            if document.filename.rsplit(".", 1)[-1].casefold() in _SPREADSHEET_EXTENSIONS
        ]
        if len(spreadsheets) != 2:
            listed = ", ".join(f"“{document.filename}”" for document in spreadsheets[:10]) or "none"
            return self._local_plan(LocalAnswer(
                intent.kind,
                f"A dated holdings comparison needs exactly two holdings spreadsheets, but {len(spreadsheets)} "
                f"{'were named' if constrained else 'are in the current scope'} ({listed}). Name the two files, for example "
                "by the dates in their file names, or select exactly two spreadsheets. Nothing was compared or sent.",
                (), "local-holdings-comparison",
            ), len(documents))
        with self._database.connection() as connection:
            result = HoldingsComparison(connection, [
                HoldingsDocument(document.document_id, document.filename, str(document.extraction_id))
                for document in spreadsheets
            ]).compare(question, legal_question=intent.legal_question)
        return self._local_plan(LocalAnswer(
            intent.kind, result.answer, result.citations, "local-holdings-comparison",
            result.evidence_document_ids, not result.complete, holdings=result.payload,
        ), len(documents))

    def _legal_holdings_evidence(self, documents: Sequence[ScopedDocument], question: str) -> LegalHoldingsEvidence | None:
        """Retrieve the factual holding records a legal-rights question refers to.

        The two dated spreadsheets are compared locally (``Decimal``).  The
        observed quantity changes become the primary evidence and their
        document caveat supporting context; neither is a legal conclusion.
        """
        spreadsheets = [
            document for document in documents
            if document.filename.rsplit(".", 1)[-1].casefold() in _SPREADSHEET_EXTENSIONS
        ]
        if len(spreadsheets) != 2:
            return None
        extraction_of = {document.document_id: str(document.extraction_id) for document in spreadsheets}
        with self._database.connection() as connection:
            result = HoldingsComparison(connection, [
                HoldingsDocument(document.document_id, document.filename, extraction_of[document.document_id])
                for document in spreadsheets
            ]).compare(question, legal_question=True)
            rows = [row for row in result.payload["rows"] if row["status"] == "compared"]  # type: ignore[index, union-attr]
            if not rows:
                return None
            caveat_references = {
                (str(source["documentId"]), str(source["reference"]))
                for caveat in result.payload["caveats"] for source in caveat["sources"]  # type: ignore[index, union-attr]
            }
            legal_hits = DocumentSearchIndex.search(
                connection, list(extraction_of.values()), _LEGAL_EVENT_TERMS, per_document_limit=10, total_limit=20,
            ).hits
        other_legal_wording = any((hit.document_id, hit.source_reference) not in caveat_references for hit in legal_hits)
        changed = [row for row in rows if Decimal(str(row["difference"])) != 0]
        relevant = changed or rows
        documents_payload = result.payload["documents"]  # type: ignore[index]
        dates = [str(document["date"] or document["filename"]) for document in documents_payload]  # type: ignore[index]

        citations: list[tuple[str, str, str, str]] = []

        def cite(side: Mapping[str, object] | None, references: Sequence[object]) -> None:
            if not side:
                return
            for reference in references:
                citation = (str(side["documentId"]), extraction_of[str(side["documentId"])], str(reference), str(side["filename"]))
                if reference and citation not in citations:
                    citations.append(citation)

        lines: list[str] = []
        for row in relevant:
            earlier, later = row["earlier"], row["later"]
            for side in (earlier, later):
                cite(side, [*side["entityReferences"], side["accountReference"], side["quantityReference"]])  # type: ignore[index]
            label = " / ".join(
                [str(entity["name"]) for entity in row["entities"] if entity["kind"] != "investor"]  # type: ignore[union-attr]
                + ([str(row["account"])] if row["account"] else [])
            )
            lines.append(
                f"{label}: {grouped_quantity(Decimal(str(earlier['quantity'])))} → "  # type: ignore[index]
                f"{grouped_quantity(Decimal(str(later['quantity'])))} "  # type: ignore[index]
                f"(change {grouped_quantity(Decimal(str(row['difference'])), signed=True)})"
            )
        caveats = result.payload["caveats"]  # type: ignore[index]
        for caveat in caveats:  # type: ignore[union-attr]
            for source in caveat["sources"]:
                citations.append((str(source["documentId"]), extraction_of[str(source["documentId"])], str(source["reference"]), str(source["filename"])))
        payload = dict(result.payload)
        quantity_heading = next((
            str(table["quantityHeading"]) for document in documents_payload for table in document["tables"]  # type: ignore[index, union-attr]
        ), "quantity")
        later_side = relevant[0]["later"]
        candidate = (
            str(later_side["documentId"]), extraction_of[str(later_side["documentId"])], str(later_side["quantityReference"]),  # type: ignore[index]
            str(later_side["filename"]),  # type: ignore[index]
            f"DOCUMENT-BACKED HOLDINGS RECORD; EVIDENCE: Recorded {quantity_heading.casefold()}, {dates[0]} → {dates[1]} "
            f"(compared locally from both dated spreadsheets): " + "; ".join(lines)
            + ". A recorded holding change is not itself evidence of a legal claim assignment.",
        )
        local_answer = None
        if {document.document_id for document in documents} == set(extraction_of) and not other_legal_wording:
            stated = ""
            if caveats:
                first = caveats[0]  # type: ignore[index]
                where = ", ".join(f"{source['filename']} {source['reference']}" for source in first["sources"])  # type: ignore[index]
                stated = f" The spreadsheets themselves state ({where}): “{first['text']}”"  # type: ignore[index]
            issues = " ".join(str(issue) for issue in result.payload["issues"])  # type: ignore[union-attr]
            answer = (
                f"The holdings spreadsheets dated {dates[0]} and {dates[1]} record: " + "; ".join(lines) + ". "
                "These are recorded holding quantities. Neither spreadsheet records an assignment or transfer of legal "
                f"claim rights.{stated} A change in recorded holdings does not by itself prove a securities transfer between "
                "these accounts or an assignment of legal claim rights; that would need explicit evidence, such as an "
                "assignment agreement or notice, and no such document is among the documents used."
                + (f" {issues}" if issues else "")
            )
            local_answer = LocalAnswer(
                INTENT_LEGAL_STATUS, answer, tuple(citations), "local-legal-status-holdings",
                frozenset(citation[0] for citation in citations), False, holdings=payload,
            )
        return LegalHoldingsEvidence(payload, tuple(citations), candidate, local_answer)

    @staticmethod
    def _local_plan(answer: LocalAnswer, documents_selected: int, *, notes: Sequence[str] = ()) -> dict[str, object]:
        return {
            "intent": answer.intent,
            "localAnswer": answer,
            "context": "",
            "citationKeys": (),
            "citationTargets": {},
            "localFallback": None,
            "comparisonCoverageComplete": not answer.partial,
            "inputTokens": 0,
            "maxOutputTokens": 0,
            "transmittedDocumentIds": (),
            "evidenceDocumentIds": tuple(sorted(answer.evidence_document_ids)),
            "searchTruncated": answer.partial,
            "notes": list(notes),
            "scope": {
                "sourceItems": len(answer.citations),
                "totalSourceItems": len(answer.citations),
                "excludedSourceItems": 0,
                "truncated": answer.partial,
                "documentsSelected": documents_selected,
                "documentsWithEvidence": len(answer.evidence_document_ids),
                "comparisonCoverageComplete": not answer.partial,
            },
        }

    @staticmethod
    def _named_entities(
        connection: sqlite3.Connection, extraction_ids: Sequence[str], question: str,
    ) -> tuple[list[str], list[str]]:
        """Split capitalised name phrases into those present in scope and those absent."""
        present: list[str] = []
        missing: list[str] = []
        for variants in question_entity_names(question):
            found = next((
                variant for variant in variants
                if DocumentSearchIndex.search(connection, extraction_ids, (variant.casefold(),), per_document_limit=1, total_limit=1).hits
            ), None)
            if found is None:
                missing.append(variants[0])
            elif found.casefold() not in {name.casefold() for name in present}:
                present.append(found)
        return present, missing

    @staticmethod
    def _mentions_required_term(
        connection: sqlite3.Connection, candidate: tuple[str, str, str, str, str], terms: Sequence[str],
        neighbour_context: Mapping[tuple[str, str], str],
    ) -> bool:
        document_id, extraction_id, reference, _filename, description = candidate
        # Only the cited source counts: an earlier summary may name the
        # entity while citing an unrelated cell (for example a sheet title).
        evidence = description.split("; EVIDENCE:", 1)[-1]
        text = f"{evidence} {neighbour_context.get((document_id, reference), '')}".casefold()
        if any(term in text for term in terms):
            return True
        # A spreadsheet cell qualifies when its row names the entity/identifier.
        cell = connection.execute(
            "SELECT content_kind, worksheet_index, sheet_name, row_number FROM extracted_content "
            "WHERE extraction_id = ? AND source_reference = ? LIMIT 1",
            (extraction_id, reference),
        ).fetchone()
        if cell is None or cell["row_number"] is None:
            return False
        row_text = " ".join(str(row["value_text"] or "") for row in connection.execute(
            "SELECT value_text FROM extracted_content WHERE extraction_id = ? AND content_kind = ? AND worksheet_index IS ? "
            "AND sheet_name IS ? AND row_number = ? AND is_empty = 0",
            (extraction_id, cell["content_kind"], cell["worksheet_index"], cell["sheet_name"], cell["row_number"]),
        ).fetchall()).casefold()
        return any(term in row_text for term in terms)

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
            # Only the first line holds the cited excerpt; an indented nearby
            # context line is interpretation aid, never quotable evidence.
            match = re.match(r"^\[([^\]]+)\].*?EVIDENCE:\s*(.*)$", line.split("\n", 1)[0])
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
            "compare", "comparison", "each isin", "for each isin", "each available date",
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

    def _validate_direct_matter_citations(
        self, citations: Sequence[tuple[str, str, str, str]],
    ) -> list[Mapping[str, str]]:
        """Re-resolve locally produced calculation citations before display."""
        validated: list[Mapping[str, str]] = []
        with self._database.connection() as connection:
            for document_id, extraction_id, reference, filename in citations:
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
    def _humanize_citation_keys(answer: str, targets: object) -> str:
        """Keep provider-internal E-keys out of lawyer-facing answer text."""
        if not isinstance(targets, dict):
            return answer
        rendered = answer
        for key, target in targets.items():
            if not isinstance(key, str) or not isinstance(target, tuple) or len(target) != 4:
                continue
            _document_id, _extraction_id, reference, filename = target
            if not isinstance(reference, str) or not isinstance(filename, str):
                continue
            label = f"{filename} ({reference})"
            rendered = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", label, rendered,
            )
        return rendered

    @staticmethod
    def _is_document_billing_question(question: str) -> bool:
        query = primary_request_text(question)
        return any(term in query for term in (
            "pro rata", "pro-rata", "allocation", "allocate", "billing", "invoice", "fee pool",
        ))

    @staticmethod
    def _question_terms(question: str) -> frozenset[str]:
        """Return the lower-case substrings that make a source relevant.

        The same terms drive the local index query and the final in-memory
        check, so indexing never changes which sources count as relevant.
        """
        query = question.casefold()
        if MatterReviewService._is_document_billing_question(question):
            return frozenset({
                "billing", "pro-rata", "pro rata", "fee pool", "allocation", "eligible", "nominal",
                "invoice", "currency", "withdraw", "excluded",
            })
        if MatterReviewService._is_legal_status_question(question):
            return frozenset({
                "litig", "claim", "right", "assign", "transfer", "withdraw", "settle", "mandate", "participat",
                "entitle", "abtret", "übertrag", "uebertrag", "klage", "anspruch", "verfahren", "vergleich",
                "austritt", "beteilig", "beend", "kündig", "kuendig",
            })
        if MatterReviewService._is_change_question(question):
            return frozenset({
                "change", "changed", "transfer", "trade", "transaction", "movement", "purchase", "sale",
                "sell", "redeem", "redemption", "liquidat", "merge", "split", "delete", "remov", "close",
                "subfund", "sub-fund", "fund", "account", "holding", "position", "quantity", "share",
                "unit", "isin", "date", "übertrag", "uebertrag", "änder", "aender", "bestand", "konto",
                "verkauf", "kauf", "tilgung", "liquid", "teilfonds", "teil-fonds", "fonds", "depot",
                "anteil", "anteile", "wertpapier", "zuteil", "transaktion", "rückzahlung", "rueckzahlung",
                "abschreib", "herabsetz", "umtausch", "kündigung", "kuendigung",
            })
        terms = {
            # A trailing plural "s" would stop "investors" matching "Investor".
            token[:-1] if len(token) > 5 and token.endswith("s") and not token.endswith("ss") else token
            for token in re.findall(r"[a-z0-9]{5,}", query) if token not in _GENERIC_QUESTION_WORDS
        }
        return frozenset(terms | {identifier.casefold() for identifier in lookup_identifiers(question)})

    @staticmethod
    def _is_question_relevant_source(question: str, source_text: str) -> bool:
        """Select only bounded source material likely to bear on the question.

        This is retrieval, not a legal conclusion.  The provider must still
        cite the exact local excerpt and the response remains provisional.
        """
        terms = MatterReviewService._question_terms(question)
        text = source_text.casefold()
        return bool(terms) and any(term in text for term in terms)

    def _ordered_sources(
        self,
        connection: sqlite3.Connection,
        hits: Sequence[SearchHit],
        question: str,
        *,
        expand_snapshots: bool,
    ) -> list[dict[str, object]]:
        """Order index hits for bounded selection, adding snapshot rows if needed.

        Within a document, sources keep their reading order (sheet, row,
        column or page, block).  Documents are then interleaved, in order of
        their best match, so a multi-document question is not answered from
        whichever file happens to sort first.
        """
        sources: dict[tuple[str, str], dict[str, object]] = {}
        document_order: list[str] = []
        for hit in hits:
            if hit.document_id not in document_order:
                document_order.append(hit.document_id)
            sources[(hit.extraction_id, hit.source_reference)] = {
                "document_id": hit.document_id, "extraction_id": hit.extraction_id,
                "content_kind": hit.source_kind, "source_reference": hit.source_reference, "text": hit.text,
                "sheet_name": hit.sheet_name, "worksheet_index": hit.worksheet_index,
                "row_number": hit.row_number, "column_number": hit.column_number,
                "page_number": hit.page_number, "block_index": hit.block_index,
            }
        if expand_snapshots:
            expansions: list[tuple[str, str, str, str, int | None, int]] = []
            seen_rows: set[tuple[str, str, int]] = set()
            for hit in hits:
                if (
                    hit.sheet_name is None or hit.row_number is None
                    or not (self._is_snapshot_heading(hit.text) or self._is_question_relevant_source(question, hit.text))
                ):
                    continue
                row_identity = (hit.extraction_id, hit.sheet_name, hit.row_number)
                if row_identity in seen_rows:
                    continue
                seen_rows.add(row_identity)
                expansions.append((
                    hit.document_id, hit.extraction_id, hit.source_kind, hit.sheet_name, hit.worksheet_index, hit.row_number,
                ))
                if len(expansions) >= MAX_SNAPSHOT_ROW_EXPANSIONS:
                    break
            for document_id, extraction_id, content_kind, sheet_name, worksheet_index, row_number in expansions:
                for row in connection.execute(
                    "SELECT source_reference, content_kind, sheet_name, worksheet_index, row_number, column_number, "
                    "COALESCE(value_text, '') || ' ' || COALESCE(formula_text, '') AS text FROM extracted_content "
                    "WHERE extraction_id = ? AND content_kind = ? AND worksheet_index IS ? AND sheet_name = ? "
                    "AND row_number BETWEEN ? AND ? AND is_empty = 0 ORDER BY row_number, column_number",
                    (extraction_id, content_kind, worksheet_index, sheet_name, row_number, row_number + 2),
                ).fetchall():
                    sources.setdefault((extraction_id, str(row["source_reference"])), {
                        "document_id": document_id, "extraction_id": extraction_id,
                        "content_kind": row["content_kind"], "source_reference": str(row["source_reference"]),
                        "text": str(row["text"] or ""), "sheet_name": row["sheet_name"],
                        "worksheet_index": row["worksheet_index"], "row_number": row["row_number"],
                        "column_number": row["column_number"], "page_number": None, "block_index": None,
                    })

        def reading_order(source: Mapping[str, object]) -> tuple[object, ...]:
            def number(key: str) -> int:
                value = source.get(key)
                return value if isinstance(value, int) else 0
            return (
                number("worksheet_index"), str(source.get("sheet_name") or ""), number("row_number"),
                number("column_number"), number("page_number"), number("block_index"), str(source["source_reference"]),
            )

        by_document: dict[str, list[dict[str, object]]] = {}
        for source in sources.values():
            by_document.setdefault(str(source["document_id"]), []).append(source)
        queues = [sorted(by_document[document_id], key=reading_order) for document_id in document_order]
        ordered: list[dict[str, object]] = []
        while any(queues):
            for queue in queues:
                if queue:
                    ordered.append(queue.pop(0))
        return ordered

    def _neighbour_context(self, connection: sqlite3.Connection, source: Mapping[str, object]) -> str | None:
        """Describe adjacent stored text so a short cell or block can be read correctly."""
        extraction_id = source.get("extraction_id")
        reference = source.get("source_reference")
        parts: list[str] = []
        if source.get("content_kind") == "pdf_text_block":
            page_number, block_index = source.get("page_number"), source.get("block_index")
            if not isinstance(page_number, int) or not isinstance(block_index, int):
                return None
            for row in connection.execute(
                "SELECT b.block_index, b.text FROM pdf_page_extractions p JOIN pdf_text_blocks b "
                "ON b.pdf_page_extraction_id = p.id WHERE p.extraction_id = ? AND p.page_number = ? "
                "AND b.block_index IN (?, ?) AND b.text <> '' ORDER BY b.block_index",
                (extraction_id, page_number, block_index - 1, block_index + 1),
            ).fetchall():
                label = "PREVIOUS BLOCK" if int(row["block_index"]) < block_index else "NEXT BLOCK"
                parts.append(f"{label}: {' '.join(str(row['text']).split())[:80]}")
        else:
            row_number, sheet_name = source.get("row_number"), source.get("sheet_name")
            if not isinstance(row_number, int) or not isinstance(sheet_name, str):
                return None
            grid = (extraction_id, source.get("content_kind"), source.get("worksheet_index"), sheet_name)
            grid_filter = "extraction_id = ? AND content_kind = ? AND worksheet_index IS ? AND sheet_name = ? AND is_empty = 0"

            def row_text(number: int) -> str:
                cells = connection.execute(
                    f"SELECT source_reference, value_text FROM extracted_content WHERE {grid_filter} "
                    f"AND row_number = ? AND source_reference <> ? ORDER BY column_number LIMIT 8",
                    (*grid, number, reference),
                ).fetchall()
                return " | ".join(f"{cell['source_reference']}: {str(cell['value_text'] or '')[:40]}" for cell in cells)

            same_row = row_text(row_number)
            if same_row:
                parts.append(f"SAME ROW: {same_row}")
            first_row = connection.execute(
                f"SELECT MIN(row_number) FROM extracted_content WHERE {grid_filter}", grid,
            ).fetchone()[0]
            if isinstance(first_row, int) and first_row < row_number:
                first = row_text(first_row)
                if first:
                    parts.append(f"FIRST ROW OF SHEET: {first}")
        context = "; ".join(parts)
        return context[:MAX_NEIGHBOUR_CONTEXT_CHARACTERS] if context else None

    @staticmethod
    def _is_legal_status_question(question: str) -> bool:
        # Legal words inside a caveat request or negative instruction ("do
        # not treat this as an assignment") do not make a legal question.
        query = primary_request_text(question)
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
        return any(term in text for term in _SNAPSHOT_HEADING_TERMS)

    @staticmethod
    def _matter_question_params(params: object, acknowledged: bool) -> MatterQuestionRequest:
        """Validate an explicitly scoped question request.

        A request must always name its matter and scope mode.  Selected mode
        never falls back to all documents: an empty selection is rejected.
        """
        required = {"matterId", "scope", "question"}
        if acknowledged:
            required |= {"acknowledged", "consentedDocumentIds"}
        if not isinstance(params, dict) or not required.issubset(params) or not set(params).issubset(required | {"followUpQuestionId"}):
            raise MatterReviewError("The consolidated question request is invalid.")
        if acknowledged and params.get("acknowledged") is not True:
            raise MatterReviewError("Confirm external processing before asking a consolidated question.")
        matter_id = params.get("matterId")
        if not isinstance(matter_id, str) or not _MATTER_ID.fullmatch(matter_id):
            raise MatterReviewError("The requested matter is unavailable. Reopen the matter and try again.")
        question = params.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 600:
            raise MatterReviewError("Enter a question of no more than 600 characters.")
        scope = params.get("scope")
        mode = scope.get("mode") if isinstance(scope, dict) else None
        if not isinstance(scope, dict) or mode not in _SCOPE_MODES:
            raise MatterReviewError("Choose whether to ask about all documents or selected documents only.")
        selected: tuple[str, ...] = ()
        if mode == "all":
            if set(scope) != {"mode"}:
                raise MatterReviewError("An all-document question cannot also name selected documents.")
        else:
            if set(scope) != {"mode", "documentIds"} or not isinstance(scope.get("documentIds"), list):
                raise MatterReviewError("Select documents for a selected-documents question.")
            document_ids = scope["documentIds"]
            if not document_ids:
                raise MatterReviewError("Select at least one document, or choose all documents in this matter.")
            if len(document_ids) > MAX_DOCUMENTS:
                raise MatterReviewError(f"Select up to {MAX_DOCUMENTS} documents for one question.")
            if not all(isinstance(value, str) and _UUID.fullmatch(value) for value in document_ids) or len(set(document_ids)) != len(document_ids):
                raise MatterReviewError("Each selected document must have a unique valid reference.")
            selected = tuple(document_ids)
        follow_up_question_id = params.get("followUpQuestionId")
        if follow_up_question_id is not None and (not isinstance(follow_up_question_id, str) or not _UUID.fullmatch(follow_up_question_id)):
            raise MatterReviewError("The follow-up reference is invalid.")
        consented: frozenset[str] = frozenset()
        if acknowledged:
            values = params.get("consentedDocumentIds")
            if not isinstance(values, list) or len(values) > 10_000 or not all(isinstance(value, str) and _UUID.fullmatch(value) for value in values):
                raise MatterReviewError("The document consent list is invalid.")
            consented = frozenset(values)
        return MatterQuestionRequest(
            matter_id=matter_id, mode=str(mode), selected_document_ids=selected, question=question.strip(),
            follow_up_question_id=follow_up_question_id, consented_document_ids=consented,
        )

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
