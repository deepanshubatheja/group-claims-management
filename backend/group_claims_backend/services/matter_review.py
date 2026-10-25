"""Read-only consolidated review and deterministic billing preview services.

This module deliberately operates on provisional, evidence-linked material.
It does not create or update holdings, claims, billing agreements, or invoices.
Billing remains an in-memory calculation using either user-entered assumptions
or one explicitly approved instruction stored in the selected documents.
"""

from __future__ import annotations

import json
import logging
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

from .ai import AIServiceError, QuestionResponseContract
from .ai_analysis import AIAnalysisError, AIAnalysisService
from .billing import BillingError, BillingService
from .document_search import DocumentSearchIndex, SearchHit
from .document_references import CandidateDocument, DocumentConstraint, question_dates, resolve_document_references
from .entity_inventory import EntityInventoryBuilder, InventoryDocument
from .holdings_comparison import HoldingsComparison, HoldingsDocument, HoldingsResult, grouped_quantity
from .billing_instruction import BillingInstruction, allocate, participant_marker, parse_billing_instruction
from .structured_evidence import ANSWER_BOILERPLATE, Line, StructuredEvidence, question_terms, render as render_structured
from .question_intent import (
    INTENT_CALCULATION, INTENT_COMPARISON, INTENT_HOLDINGS_COMPARISON, INTENT_INVENTORY, INTENT_LEGAL_STATUS, INTENT_RELATIONSHIP,
    ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY, ROLE_SUBFUND, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND,
    QuestionIntent, classify_question, is_contextual_phrase, lookup_identifiers, primary_request_text, question_entity_names,
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
_DATE_DOCUMENT_SELECTOR = re.compile(
    r"\b(?:spreadsheet|workbook|statement|report|file|document)s?\s+dated\b", re.IGNORECASE,
)
_UNKNOWN_FOLLOW_UP_ANSWER = "The selected evidence does not establish an answer to this question."
# Question filler words that otherwise match unrelated text in every document.
_GENERIC_QUESTION_WORDS = frozenset({
    "which", "where", "there", "their", "these", "those", "about", "across", "mention", "mentioned", "mentions",
    "upload", "uploaded", "document", "documents", "files", "record", "records", "recorded", "please", "could",
    "would", "should", "every", "other", "named", "listed", "contain", "contains", "include", "includes",
    "provide", "information", "details", "anything",
})
_SPREADSHEET_EXTENSIONS = frozenset({"xlsx", "xls", "csv"})
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
MAX_QUESTION_CITATION_SOURCES = 16
# User question text is measured as Unicode code points after outer
# whitespace is removed. It is independent of the bounded provider prompt,
# which may still reject a detailed question when no cited evidence can fit.
MAX_MATTER_QUESTION_CHARACTERS = 5_000
# Historical reconstructions have a small, separate bounded allowance.  Four
# documents plus six direct holdings cells cannot reliably fit in the generic
# short-answer budget without dropping a fact-level source.
# Six direct holdings rows, three event records, and five independently
# citable billing facts fit below this measured bounded allowance. Keeping it
# explicit prevents an all-document historical request from silently dropping
# a required billing basis or event source.
MAX_HISTORICAL_QUESTION_CHARACTERS = 6_000
MAX_RECONCILIATION_EXCERPTS = 20
# Request characters kept for cited evidence before an earlier answer is
# added as follow-up context; the earlier answer is shortened to fit.
FOLLOW_UP_EVIDENCE_RESERVE_CHARACTERS = 600
_CONFLICT_CUE = re.compile(r"\b(?:conflict\w*|contradict\w*|discrepan\w*|disput\w*|inconsisten\w*|competing|mismatch\w*|disagree\w*)\b")
_POSITION_CUE = re.compile(r"\b(?:holdings?|positions?|balances?|records?|values?|figures?|amounts?|nominal|statements?)\b")
_INVOICE_RECIPIENT_CUE = re.compile(
    r"\binvoice\s+recipients?\b|\binvoices?\b[^.?]*\b(?:recipients?|receive|receiving|addressed|send|sent|issued\s+to|bill(?:ed)?\s+to|responsib\w*)\b"
    r"|\bwho\s+(?:should|will|must|is\s+to)\s+(?:be\s+)?(?:invoiced|billed)\b"
    r"|\b(?:receive|get|be\s+sent|be\s+addressed|pay)\b[^.?]*\binvoices?\b"
)
_HYPOTHETICAL_CUE = re.compile(r"\bhypothetic\w*|\bwhat\s+if\b|\bsuppos\w*|\bassum(?:e|ing|ption)\b|\bscenario\b|\bif\b[^.?]*\b(?:were|had|became)\b")
_CLAIM_CUE = re.compile(r"\bassign(?:s|ed|ee|ees|or|ors|ment|ments)?\b|\bclaim[-\s]rights?\b|\blegal\s+claims?\b|\bclaim\s+ownership\b|\bclaimants?\b")
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_BASIS_OVERRIDE = re.compile(
    rf"(?P<label>(?:[A-Z][\w\-]*\s+){{0,6}}[A-Z0-9][\w\-]*)['’]s\s+(?:eligible\s+)?(?:nominal\s+)?(?:basis|nominal|amount|holding)\s+"
    rf"(?:were|was|is|becomes|became|of|at|changed\s+to|=)\s*(?:[A-Z]{{3}}\s*)?(?P<amount>{_AMOUNT})"
    rf"|\b(?:basis|eligible\s+nominal)\s+(?:for|of)\s+(?P<label2>(?:[A-Z][\w\-]*\s+){{0,6}}[A-Z0-9][\w\-]*)\s+(?:were|was|is|of|at|to)\s*(?:[A-Z]{{3}}\s*)?(?P<amount2>{_AMOUNT})"
)
_POOL_OVERRIDE = re.compile(rf"\bfee\s+pool\s+(?:were|was|is|of|became|changed\s+to)\s*(?:[A-Z]{{3}}\s*)?(?P<amount>{_AMOUNT})", re.IGNORECASE)
_INVOICE_STATEMENT = re.compile(
    r"\binvoice\s+recipient\b|\bnot\s+an\s+invoice\b|\bbill(?:ed)?\s+to\b|\binvoices?\s+(?:should|shall|must|will)\s+be\s+(?:addressed|sent|issued)\s+to\b",
    re.IGNORECASE,
)
_TRUST_CUE = re.compile(r"\b(?:trust\w*|rely|reliable|believe|correct|accurate|better[- ]supported|which\s+(?:one|value|figure|position|balance|record|amount))\b")
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
LOGGER = logging.getLogger(__name__)


def _escape(value: object) -> str:
    """Document text safe to embed in a structured ``Line`` template."""
    return str(value).replace("{", "{{").replace("}", "}}")


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
    # This is a local Decimal calculation, not source-document prose. It is
    # supplied only beside the individual source blocks required to verify it.
    derived_calculation_context: str = ""
    # (document id, source reference, plain-language fact label). These names
    # are trusted local parser output and never document-provided instructions.
    source_facts: tuple[tuple[str, str, str], ...] = ()


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
class AnalysisRequirements:
    """Independent factual tasks requested by one Matter Review question.

    Routing still selects a convenient primary presentation strategy, but it
    must not discard another requested analysis merely because a question
    mentions claim rights, a transfer, or billing as well as holdings.
    """

    historical_holdings: bool = False
    historical_chronology: bool = False
    holdings_comparison: bool = False
    multiple_as_of_dates: bool = False
    securities_transfer: bool = False
    claim_assignment: bool = False
    contradictions: bool = False
    pro_rata_billing: bool = False
    billing_impact: bool = False
    invoice_status: bool = False

    @property
    def requires_cross_document_coverage(self) -> bool:
        """Whether a compound request must retain the resolved whole scope."""
        return self.historical_chronology or (
            self.historical_holdings and (
                self.multiple_as_of_dates
                or self.securities_transfer
                or self.claim_assignment
                or self.contradictions
                or self.pro_rata_billing
                or self.billing_impact
                or self.invoice_status
            )
        ) or (
            # A direct comparison of contradictory holdings with an approved
            # billing instruction is also compound evidence work.  It must
            # not be intercepted by the single-instruction billing shortcut.
            self.holdings_comparison and self.contradictions and self.pro_rata_billing
        )

    def as_dict(self) -> Mapping[str, bool]:
        return {
            "historicalHoldings": self.historical_holdings,
            "historicalChronology": self.historical_chronology,
            "holdingsComparison": self.holdings_comparison,
            "multipleAsOfDates": self.multiple_as_of_dates,
            "securitiesTransfer": self.securities_transfer,
            "claimAssignment": self.claim_assignment,
            "contradictions": self.contradictions,
            "proRataBilling": self.pro_rata_billing,
            "billingImpact": self.billing_impact,
            "invoiceStatus": self.invoice_status,
        }

    def prompt_text(self) -> str:
        requested = [
            label for enabled, label in (
                (self.historical_holdings, "historical holdings"),
                (self.historical_chronology, "chronological timeline"),
                (self.holdings_comparison, "dated holdings comparison"),
                (self.multiple_as_of_dates, "requested as-of dates"),
                (self.securities_transfer, "securities transfers"),
                (self.claim_assignment, "claim-right assignments"),
                (self.contradictions, "unresolved contradictions"),
                (self.pro_rata_billing, "approved pro-rata billing"),
                (self.billing_impact, "billing-basis impact"),
                (self.invoice_status, "issued-invoice status"),
            ) if enabled
        ]
        return "; ".join(requested) or "answer only the supported factual request"


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
class HistoricalHoldingsEvidence:
    """Compact deterministic transcriptions of selected dated holdings sheets."""

    candidates: tuple[tuple[str, str, str, str, str], ...]
    citations: tuple[tuple[str, str, str, str], ...]


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
    answer: str | None = None
    unknown: bool = False
    citations: tuple[Mapping[str, str], ...] = ()
    error_code: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    actual_cost_estimate_usd: str | None = None
    # Short-lived operational metadata for test/support inspection. It must
    # never contain a credential, question text, prompt, or source content.
    execution: Mapping[str, object] | None = None


class MatterReviewService:
    """Retrieves bounded provisional findings and calculates a non-persistent preview."""

    def __init__(self, storage: StorageService, analysis: AIAnalysisService | None = None) -> None:
        if storage.database is None:
            raise MatterReviewError("Local storage is unavailable.")
        self._storage = storage
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
        self, documents: Sequence[ScopedDocument], question: str,
    ) -> DocumentBillingAnswer | None:
        """Calculate only from an expressly approved local billing instruction.

        This is intentionally narrower than the configurable preview: it does
        not infer a fee pool, participants, eligibility, or a legal effect
        from holdings, assignments, or an AI summary.  It is useful when the
        selected material itself supplies all of those inputs, such as the
        synthetic matter supplied with ClaimFlow.
        """
        if not self._is_document_billing_question(question):
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
        for page_sources in pages.values():
            source_text = "\n".join(str(source["text"]) for source in page_sources)
            parsed = self._parse_document_billing_instruction(source_text)
            if parsed is None:
                continue
            currency, fee_pool, rows, period = parsed
            instructions.append((page_sources, currency, fee_pool, rows, period))
        # Multiple valid instructions could represent conflicting billing
        # agreements.  Refuse to choose between them automatically.
        if len(instructions) != 1:
            return None
        page_sources, currency, fee_pool, rows, period = instructions[0]
        source_text = "\n".join(str(source["text"]) for source in page_sources)
        allocations = self._allocate_pro_rata(fee_pool, rows)
        total_eligible = sum((row.eligible_amount for row in rows), Decimal("0"))
        allocations_text = "; ".join(
            f"{row.label}: {currency} {fee:,.2f}"
            for row, fee in allocations
        )
        excluded_match = re.search(
            r"(?:Excluded\s+participant\s*\n\s*([^\n(]+?)\s*\(([^)]+)\)\s*:\s*([^\n]+)"
            r"|([^\n(]+?)\s*\(([^)]+)\)\s+is\s+excluded\s+because\s+([^\n]+))",
            source_text, flags=re.IGNORECASE,
        )
        exclusion = ""
        if excluded_match is not None:
            name = (excluded_match.group(1) or excluded_match.group(4) or "").strip()
            identifier = (excluded_match.group(2) or excluded_match.group(5) or "").strip()
            reason = (excluded_match.group(3) or excluded_match.group(6) or "").strip()
            exclusion = (
                f" {name} ({identifier}) is excluded by the instruction because {reason}."
            )

        def source_fact_label(text: str) -> str:
            folded = text.casefold()
            if "fee pool" in folded:
                return "approved fee pool"
            if "allocation rule" in folded:
                return "approved allocation rule"
            if "eligible participant" in folded:
                return "approved eligible participant nominal basis"
            if " is excluded because " in folded or "excluded participant" in folded:
                return "approved excluded participant and reason"
            return "approved billing instruction fact"

        citation_sources = [
            source for source in page_sources
            if re.match(
                r"^\s*(Fee\s+pool|Allocation\s+rule|Eligible\s+participant|Excluded\s+participant)\b",
                str(source["text"]), flags=re.IGNORECASE,
            ) or " is excluded because " in str(source["text"]).casefold()
        ]
        # A parser may preserve an instruction as one large page block. That
        # single block remains a valid precise citation when field-level
        # layout blocks are not available.
        if not citation_sources:
            citation_sources = page_sources
        intent = self._document_billing_intent(question)
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
            answer = (
                f"Using the approved document-backed pro-rata instruction for {period}, the provisional allocation "
                f"of {currency} {fee_pool:,.2f} is {allocations_text}. "
                "The instruction applies eligible nominal amount divided by total eligible nominal amount, with "
                f"currency rounding to reconcile to the fee pool.{exclusion} This is a local calculation preview, "
                "not an invoice or a determination of legal entitlement."
            )
        elif intent in {"invoiced", "outstanding"}:
            answer = (
                f"The selected instruction supports a calculated allocation for {period} of {currency} {fee_pool:,.2f}: "
                f"{allocations_text}. It does not establish a verified issued invoice, payment, credit note, or "
                "outstanding balance. Those require separately confirmed local invoice and payment records."
            )
        elif intent == "ambiguous":
            answer = (
                f"Calculated allocation for {period}: {currency} {fee_pool:,.2f}, split as {allocations_text}. "
                "Issued invoices and outstanding balances are separate verified records; this instruction alone "
                "does not establish that any amount was invoiced."
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
            ),
            instruction_context=instruction_context,
            derived_calculation_context=(
                "DERIVED LOCAL PRO-RATA CALCULATION (not a source-document statement and not an invoice): "
                f"eligible denominator {self._document_decimal(total_eligible)}; "
                + "; ".join(
                    f"{row.label} calculated allocation {currency} {fee:,.2f}"
                    for row, fee in allocations
                )
                + f"; allocations reconcile exactly to {currency} {fee_pool:,.2f}. "
                "This calculation does not resolve disputed holdings or determine a legal invoice recipient."
            ),
            source_facts=tuple(
                (str(source["document_id"]), str(source["source_reference"]), source_fact_label(str(source["text"])))
                for source in citation_sources
            ),
        )

    def _persisted_billing_answer(
        self, documents: Sequence[ScopedDocument], question: str,
    ) -> DocumentBillingAnswer | None:
        """Present persisted allocations and verified invoice records without AI inference."""
        if not self._is_document_billing_question(question):
            return None
        document_by_id = {document.document_id: document for document in documents}
        if not document_by_id:
            return None
        placeholders = ",".join("?" for _ in document_by_id)
        with self._database.connection() as connection:
            agreements = connection.execute(
                f"SELECT id, source_document_id, source_reference FROM billing_agreements "
                f"WHERE approval_status = 'approved' AND source_document_id IN ({placeholders}) ORDER BY approved_at DESC, id DESC",
                tuple(document_by_id),
            ).fetchall()
        # Multiple approved instructions need explicit lawyer selection; do
        # not choose one by document order for a chat answer.
        if len(agreements) != 1:
            return None
        agreement = agreements[0]
        billing = BillingService(self._storage)
        try:
            summary = billing.invoicing({"billingAgreementId": str(agreement["id"])})
        except BillingError:
            return None
        intent = self._document_billing_intent(question)
        include_calculated = intent in {"allocation", "ambiguous", "overview", "explanation", "verification"}
        include_invoicing = intent in {"invoiced", "outstanding", "ambiguous", "overview"}
        allowed_document_ids = set(document_by_id)
        citations: list[tuple[str, str, str, str]] = []
        source_document_id = str(agreement["source_document_id"])
        source_document = document_by_id.get(source_document_id)
        if source_document is None or source_document.extraction_id is None:
            return None
        reference = str(agreement["source_reference"]).split(";", 1)[0].strip()
        citations.append((source_document_id, str(source_document.extraction_id), reference, source_document.filename))
        period = summary["billingPeriod"]
        period_text = (
            f"{period.get('start') or 'unknown start'} to {period.get('end') or 'unknown end'}"
            if isinstance(period, dict) else "the approved billing period"
        )
        calculation = billing.preview({"billingAgreementId": str(agreement["id"])})
        lines: list[str] = [
            f"Billing period: {period_text}. Total fee pool: "
            f"{calculation['currency']} {calculation['feePool']}."
        ]
        for allocation in summary["allocations"]:
            if not isinstance(allocation, dict):
                continue
            label = str(allocation.get("participantLabel", "Unknown participant"))
            facts: list[str] = []
            if include_calculated:
                if allocation.get("eligibilityStatus") == "excluded":
                    facts.append(f"Calculated allocation: excluded ({allocation.get('eligibilityReason') or 'reason not recorded'}).")
                else:
                    facts.append(
                        f"Calculated allocation: eligible nominal {allocation.get('eligibleBasis')}; "
                        f"share {allocation.get('share')}; {calculation['currency']} {allocation.get('allocatedFee')}."
                    )
            if include_invoicing:
                invoice_evidence = allocation.get("invoiceEvidence")
                usable_evidence = [item for item in invoice_evidence if isinstance(item, dict) and item.get("documentId") in allowed_document_ids] if isinstance(invoice_evidence, list) else []
                invoice_status = allocation.get("issuedInvoiceStatus")
                if invoice_status == "verified_issued" and usable_evidence:
                    facts.append(f"Issued invoices: {calculation['currency']} {allocation.get('verifiedInvoicedAmount')}.")
                    if allocation.get("allocationInvoiceDifference") is not None:
                        facts.append(f"Difference from calculated allocation: {calculation['currency']} {allocation.get('allocationInvoiceDifference')}.")
                    if allocation.get("outstandingStatus") == "verified":
                        facts.append(f"Outstanding balance: {calculation['currency']} {allocation.get('outstandingBalance')}.")
                    else:
                        facts.append("Outstanding balance: not established from verified payment and adjustment evidence.")
                    for item in usable_evidence:
                        document_id, source_ref = item.get("documentId"), item.get("sourceReference")
                        document = document_by_id.get(str(document_id))
                        if document is not None and document.extraction_id is not None and isinstance(source_ref, str):
                            citations.append((str(document_id), str(document.extraction_id), source_ref, document.filename))
                elif invoice_status == "no_issued_invoice_recorded":
                    facts.append("Issued invoices: no verified issued invoice record is linked to this agreement.")
                elif invoice_status == "draft_only":
                    facts.append("Issued invoices: drafts are present but are not counted as issued invoices.")
                else:
                    facts.append("Issued invoices: not determinable for this participant from in-scope verified invoice evidence.")
            if facts:
                lines.append(f"{label} — " + " ".join(facts))
        top = summary.get("invoicing") if isinstance(summary, dict) else None
        if isinstance(top, dict) and top.get("unmappedIssuedInvoiceLineCount"):
            lines.append(f"Notice: {top['unmappedIssuedInvoiceLineCount']} issued invoice line(s) cannot be matched to a calculated participant.")
        deduplicated = tuple(dict.fromkeys(citations))
        return DocumentBillingAnswer(" ".join(lines), deduplicated, "Persisted approved billing calculation and separately verified invoice ledger.")

    def _multi_document_billing_answer(
        self, documents: Sequence[ScopedDocument], question: str,
    ) -> DocumentBillingAnswer | None:
        """Explain the billing relevance of every explicitly requested document.

        A phrase such as "for each document" is a scope instruction, not a
        request to invent a separate fee pool for every file. The approved
        billing instruction remains the sole deterministic allocation input;
        dated holdings, transfers, assignments, and competing statements are
        reported only when locally stored evidence supports that role.
        """
        if not self._is_scope_wide_billing_question(question):
            return None
        calculation = self._persisted_billing_answer(documents, question) or self._document_billing_answer(documents, question)
        if calculation is None:
            return None
        terms = (
            "fee pool", "allocation rule", "eligible participant", "holding", "position", "nominal",
            "transfer", "effective", "claim", "assignment", "contradict", "conflict", "dispute",
        )
        citations: list[tuple[str, str, str, str]] = list(calculation.citations)
        relevance: list[str] = []
        with self._database.connection() as connection:
            for document in documents:
                extraction_id = str(document.extraction_id)
                hits = DocumentSearchIndex.search(
                    connection, [extraction_id], terms, per_document_limit=6, total_limit=6,
                ).hits
                source_text = "\n".join(hit.text for hit in hits)
                selected_reference = next((hit.source_reference for hit in hits if hit.source_reference), None)
                if selected_reference is None:
                    fallback = connection.execute(
                        "SELECT source_reference FROM extracted_content WHERE extraction_id = ? AND is_empty = 0 "
                        "ORDER BY worksheet_index, sheet_name, row_number, column_number LIMIT 1",
                        (extraction_id,),
                    ).fetchone()
                    if fallback is not None:
                        selected_reference = str(fallback["source_reference"])
                    else:
                        fallback = connection.execute(
                            "SELECT 'page ' || p.page_number || ' block ' || b.block_index AS source_reference "
                            "FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                            "WHERE p.extraction_id = ? AND b.text <> '' ORDER BY p.page_number, b.block_index LIMIT 1",
                            (extraction_id,),
                        ).fetchone()
                        selected_reference = str(fallback["source_reference"]) if fallback is not None else None
                if selected_reference is None:
                    relevance.append(f"{document.filename}: no citable extracted source was available.")
                    continue
                evidence = self._analysis._source_evidence(connection, extraction_id, selected_reference)
                if evidence is None:
                    relevance.append(f"{document.filename}: the selected source could not be revalidated.")
                    continue
                reference, evidence_text = evidence
                citations.append((document.document_id, extraction_id, reference, document.filename))
                folded = f"{source_text}\n{evidence_text}".casefold()
                if "fee pool" in folded and "allocation rule" in folded:
                    role = "contains the approved fee pool, eligibility basis, and allocation rule; it is the independent calculation input"
                elif "claim" in folded and "assign" in folded:
                    role = "records a claim-right assignment; it is legally distinct from a securities movement and does not supply a separate fee pool"
                elif "transfer" in folded or "securities movement" in folded:
                    role = "records securities-movement evidence; it supports timing and holdings context but does not supply a separate fee pool"
                elif any(term in folded for term in ("contradict", "conflict", "dispute")):
                    role = "contains potentially competing holdings evidence; it must remain unresolved and does not replace the approved billing basis"
                elif any(term in folded for term in ("holding", "position", "nominal", "as of")):
                    role = "contains a dated holdings observation supporting the allocation context; it does not by itself create a billing rule"
                else:
                    role = "does not expose an independently parsed billing input in the selected evidence"
                relevance.append(f"{document.filename}: {role}.")
        cited_document_ids = {citation[0] for citation in citations}
        if len(cited_document_ids) != len(documents):
            return None
        return DocumentBillingAnswer(
            answer=(
                f"Across all {len(documents)} selected documents, there is one consolidated billing analysis. "
                f"{calculation.answer} Document relevance: " + " ".join(relevance)
            ),
            citations=tuple(dict.fromkeys(citations)),
            instruction_context=calculation.instruction_context,
        )

    @staticmethod
    def _document_billing_intent(question: str) -> str:
        """Choose a response form without treating a billing keyword as a calculation request."""
        query = question.casefold()
        if any(term in query for term in ("outstanding", "remain outstanding", "remaining balance")):
            return "outstanding"
        if any(term in query for term in ("invoiced", "issued invoice", "issued invoices")):
            return "invoiced"
        if any(term in query for term in ("being billed", "is it billed", "amount billed")):
            return "ambiguous"
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
    def _is_scope_wide_billing_question(question: str) -> bool:
        query = " ".join(primary_request_text(question).casefold().split())
        if not MatterReviewService._is_document_billing_question(question):
            return False
        return any(phrase in query for phrase in (
            "for each document", "for every document", "each of the document", "each document",
            "across all documents", "across the documents", "all selected documents", "all the documents",
        ))

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
        numbered_participants = re.findall(
            r"Eligible\s+participant\s+(\d+)\b", source_text, flags=re.IGNORECASE,
        )
        if money is None or period is None or not participants:
            return None
        # If the instruction declares numbered participants, every declared
        # row needs a parseable nominal basis. Otherwise a malformed second
        # participant could silently become an unsupported one-row allocation.
        if numbered_participants:
            expected_numbers = list(range(1, len(numbered_participants) + 1))
            parsed_numbers = [int(value) for value in numbered_participants]
            if parsed_numbers != expected_numbers or len(participants) != len(numbered_participants):
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
        requirements = self._analysis_requirements(request.question)
        # Dates such as "as of 14 February" are factual targets in a
        # historical reconstruction, not necessarily file-name references.
        # Treating them as document constraints previously rejected the six
        # source question before retrieval (there is no 14 February file).
        # Explicit file selection remains available through the scope picker.
        constraint = self._document_constraint(request.question, scope)
        # Dates such as "as of 14 February" remain factual targets in a
        # historical reconstruction, not file selectors.  Explicit IDs,
        # filenames, numbered prefixes, and titles still deliberately narrow
        # the already authorized scope.
        # A descriptive filename phrase can overlap ordinary analytical
        # language (for example, “claim-right assignment effective”), so it
        # must not shrink a compound all-document request on its own. Stable
        # IDs, exact filenames, numbered prefixes, and an explicit “document
        # dated …” selector remain deliberate narrowing instructions.
        has_stable_scope_selector = any(
            item.matched_by in {"the document ID", "the file name"}
            or "numbered file prefix" in item.matched_by
            for item in constraint.resolved
        )
        if (
            requirements.requires_cross_document_coverage
            and not has_stable_scope_selector
            and not _DATE_DOCUMENT_SELECTOR.search(request.question)
        ):
            constraint = DocumentConstraint(False)
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
                execution=self._local_execution_metadata(scope, plan, local_answer.model),
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
                intent=INTENT_CALCULATION,
                execution=self._local_execution_metadata(scope, plan, "local-document-calculation"),
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
                execution=self._provider_execution_metadata(scope, plan, preview),
            )
            self._questions[job.id] = job
        try:
            self._record_question_consent(job.id, scope, transmitted, str(preview["provider"]))
        except sqlite3.Error as error:
            # This is the only synchronous write in the question-submit path.
            # A second ClaimFlow process (for example an installed build left
            # open alongside ``npm run dev``) can temporarily hold the same
            # local SQLite store.  Do not turn that operational condition
            # into the protocol's generic internal-error response, and do
            # not leave its queued job behind to block the next question.
            with self._question_lock:
                self._questions.pop(job.id, None)
            if "locked" in str(error).casefold() or "busy" in str(error).casefold():
                raise MatterReviewError(
                    "ClaimFlow's local database is busy. Close any other ClaimFlow window or development session, "
                    "wait a moment, and try the question again."
                ) from error
            raise MatterReviewError(
                "ClaimFlow could not save this question's local authorization record. Restart ClaimFlow and try again."
            ) from error
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
                "scope": dict(job.scope),
                "documentScope": dict(job.document_scope or {}),
                "usage": {
                    "inputTokens": job.input_tokens,
                    "outputTokens": job.output_tokens,
                    "totalTokens": job.total_tokens,
                    "actualCostEstimateUsd": job.actual_cost_estimate_usd,
                },
                "execution": dict(job.execution or {}),
            }

    @staticmethod
    def _local_execution_metadata(scope: DocumentScope, plan: Mapping[str, object], model: str) -> Mapping[str, object]:
        included = tuple(sorted(str(value) for value in plan.get("evidenceDocumentIds", ())))
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "provider": None,
            "model": model,
            "providerCallAttempted": False,
            "providerOutcome": "local_only",
            "fallbackUsed": False,
            "fallbackReason": None,
            "modelUnknown": None,
            "responseStatus": "local",
            "validatedSectionKinds": (),
            "validatedSectionCounts": {},
            "validatedSectionStatusCounts": {},
            "validatedCitationCount": 0,
            "evidenceItemCounts": dict(plan.get("evidenceItemCounts", {})),
            "documentsSelected": tuple(document.document_id for document in scope.searchable),
            "documentsIncluded": included,
            "documentsExcluded": tuple(sorted(set(document.document_id for document in scope.searchable) - set(included))),
            "evidenceTruncated": bool(plan.get("scope", {}).get("truncated", False)) if isinstance(plan.get("scope"), Mapping) else False,
            "requirements": dict(plan.get("analysisRequirements", {})) if isinstance(plan.get("analysisRequirements"), Mapping) else {},
        }

    @staticmethod
    def _provider_execution_metadata(
        scope: DocumentScope, plan: Mapping[str, object], preview: Mapping[str, object],
    ) -> Mapping[str, object]:
        included = tuple(sorted(str(value) for value in plan.get("evidenceDocumentIds", ())))
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "provider": preview.get("provider"),
            "model": preview.get("model"),
            "providerCallAttempted": False,
            "providerOutcome": "queued",
            "fallbackUsed": False,
            "fallbackReason": None,
            "modelUnknown": None,
            "responseStatus": None,
            "validatedSectionKinds": (),
            "validatedSectionCounts": {},
            "validatedSectionStatusCounts": {},
            "validatedCitationCount": 0,
            "evidenceItemCounts": dict(plan.get("evidenceItemCounts", {})),
            "documentsSelected": tuple(document.document_id for document in scope.searchable),
            "documentsIncluded": included,
            "documentsExcluded": tuple(sorted(set(document.document_id for document in scope.searchable) - set(included))),
            "evidenceTruncated": bool(plan.get("scope", {}).get("truncated", False)) if isinstance(plan.get("scope"), Mapping) else False,
            "requirements": dict(plan.get("analysisRequirements", {})) if isinstance(plan.get("analysisRequirements"), Mapping) else {},
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
        if parent_matter != scope.matter_id:
            raise MatterReviewError("The earlier answer belongs to another matter. Ask this as a new question.")
        if not parent_documents <= scope.document_ids:
            raise MatterReviewError(
                "This follow-up would reuse an answer based on documents outside the current scope. "
                "Ask it as a new question in this scope instead."
            )
        answer = parent_answer if parent_answer and not parent_unknown else _UNKNOWN_FOLLOW_UP_ANSWER
        # The earlier answer is context, never evidence: its citation markers
        # ("[file, Sheet!C2]") are not reused and only consume the budget.
        for sentence in ANSWER_BOILERPLATE:
            answer = answer.replace(sentence, "")
        answer = " ".join(re.sub(r"\s*\[[^\[\]\n]{1,240}\]", "", answer).split())
        return {"question": parent_question[:600], "answer": answer[:1200]}, parent_documents

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
            job.execution = {**dict(job.execution or {}), "providerCallAttempted": True, "providerOutcome": "attempting"}
        try:
            self._analysis._refresh_configuration()
            if not self._analysis._global_authorized():
                raise AIAnalysisError("AI_AUTHORIZATION_WITHDRAWN", "Cloud-processing authorization was withdrawn.")
            configuration = self._analysis._configuration
            if configuration.provider not in {"openai", "codex"} or not configuration.credential_configured:
                raise AIAnalysisError("AI_PROVIDER_NOT_CONFIGURED", "Connect the selected AI provider in Settings.")
            response_contract = plan.get("responseContract")
            provider_kwargs = (
                {"response_contract": response_contract}
                if isinstance(response_contract, QuestionResponseContract) else {}
            )
            result = self._analysis._new_provider(int(plan["maxOutputTokens"])).answer_question(
                question, str(plan["context"]), tuple(plan["citationKeys"]), **provider_kwargs,
            )
            response_metadata = dict(result.historical_metadata or {})
            provider_unknown = result.unknown
            local_fallback = plan.get("localFallback")
            # A comparison across a selected matter must never be presented
            # as comprehensive when the bounded prompt could include evidence
            # from only a subset of those documents.  Prefer the precise,
            # cited local coverage statement even if a model attempts a broad
            # answer from the partial context.
            coverage_incomplete_comparison = plan.get("comparisonCoverageComplete") is False
            fallback_reason: str | None = None
            if coverage_incomplete_comparison:
                fallback_reason = "evidence_coverage_incomplete"
            elif result.unknown or not result.answer:
                fallback_reason = "provider_unsupported_answer"
            if fallback_reason is not None and isinstance(local_fallback, tuple):
                answer, keys = local_fallback
                result = type(result)(
                    answer, False,
                    tuple({"sourceReference": key, "evidenceSnippet": ""} for key in keys),
                    result.model, result.input_tokens, result.output_tokens, result.total_tokens,
                )
            citations = self._validate_matter_citations(
                result.citations,
                plan["citationTargets"],
                origin="local_fallback" if fallback_reason is not None else None,
            )
            if not citations:
                raise AIAnalysisError("AI_ANSWER_EVIDENCE_UNSUPPORTED", "The answer did not include supported local evidence.")
            # Dated holding cells behind a locally-produced legal-status
            # answer are cited too, re-resolved locally, whichever excerpt
            # the model chose.  Do not append cross-document coverage
            # references to a provider answer: a document being in context is
            # not proof for a particular conclusion.
            cited = {(citation["documentId"], citation["sourceReference"]) for citation in citations}
            additional_citations = [
                *plan.get("holdingsCitations", ()),  # type: ignore[arg-type]
            ]
            if not isinstance(plan.get("responseContract"), QuestionResponseContract):
                additional_citations.extend(plan.get("coverageCitations", ()))  # type: ignore[arg-type]
            for citation in self._validate_direct_matter_citations(additional_citations):
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
                job.execution = {
                    **dict(job.execution or {}),
                    "providerOutcome": "completed",
                    "fallbackUsed": fallback_reason is not None,
                    "fallbackReason": fallback_reason,
                    # The provider adapter has already schema-validated these
                    # aggregate facts. No model text, prompt, excerpt, or
                    # credential is retained in execution diagnostics.
                    "modelUnknown": response_metadata.get("modelUnknown", provider_unknown),
                    "responseStatus": (
                        "fallback"
                        if fallback_reason is not None
                        else response_metadata.get("responseStatus", "no_supported_findings" if result.unknown else "complete")
                    ),
                    "validatedSectionKinds": tuple(response_metadata.get("validatedSectionKinds", ())),
                    "validatedSectionCounts": dict(response_metadata.get("validatedSectionCounts", {})),
                    "validatedSectionStatusCounts": dict(response_metadata.get("validatedSectionStatusCounts", {})),
                    "validatedCitationCount": int(response_metadata.get("validatedCitationCount", len(result.citations))),
                }
        except AIServiceError as error:
            with self._question_lock:
                job = self._questions.get(question_id)
                if job is not None:
                    job.status = "failed"
                    job.error_code = error.code
                    job.execution = {
                        **dict(job.execution or {}),
                        "providerOutcome": "provider_failure",
                        "fallbackUsed": False,
                        "fallbackReason": None,
                    }
        except Exception:
            with self._question_lock:
                job = self._questions.get(question_id)
                if job is not None:
                    job.status = "failed"
                    job.error_code = "AI_PROVIDER_FAILURE"
                    job.execution = {
                        **dict(job.execution or {}),
                        "providerOutcome": "provider_failure",
                        "fallbackUsed": False,
                        "fallbackReason": None,
                    }

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
        requirements = self._analysis_requirements(question)
        structured: StructuredEvidence | None = None
        scope_notes: list[str] = []
        if requirements.requires_cross_document_coverage:
            structured, documents, scope_notes = self._scoped_structured_evidence(documents, question)
            document_ids = [document.document_id for document in documents]
            names = {document.document_id: document.filename for document in documents}
            current_extractions = {str(document.extraction_id): document.document_id for document in documents}
            extraction_ids = list(current_extractions)
        # Conflicting holdings ("identify the conflicts", "which position
        # should we trust?") are answered from the competing extracted
        # records, never from a two-spreadsheet comparison.
        conflict_mode = self._conflict_request(question, follow_up)
        if conflict_mode is not None and not requirements.requires_cross_document_coverage:
            conflict_plan = self._conflict_plan(documents, question, follow_up, assess=conflict_mode == "assess")
            if conflict_plan is not None:
                return conflict_plan
        # "Which names refer to the same fund?" is answered from extracted
        # account identifiers and documented alias statements, not keyword
        # ranking of raw text.  Without such records it falls through.
        if self._is_identity_question(question) and not requirements.requires_cross_document_coverage:
            identity_plan = self._identity_plan(documents, question)
            if identity_plan is not None:
                return identity_plan
        if not requirements.requires_cross_document_coverage:
            # Invoice recipients, approved or hypothetical billing, and claim
            # assignments are answered from the cited instruction and records.
            routed_query = " ".join(primary_request_text(question).casefold().split())
            routed = None
            if _INVOICE_RECIPIENT_CUE.search(routed_query):
                routed = self._invoice_recipient_plan(documents)
            elif self._is_document_billing_question(question) or (_HYPOTHETICAL_CUE.search(routed_query) and re.search(r"\b(?:basis|fee\s+pool|allocation)", routed_query)):
                routed = self._instruction_billing_plan(documents, question)
            elif _CLAIM_CUE.search(routed_query) and intent.kind != INTENT_HOLDINGS_COMPARISON and not _HOLDINGS_MOVEMENT_CUE.search(routed_query):
                # Holdings-movement questions keep the dated holdings comparison.
                routed = self._claim_assignment_plan(documents, question)
            if routed is not None:
                return routed
        # A historical reconstruction can also ask for named entities or
        # relationships.  Those words must not collapse it into a local
        # inventory response that ignores its dates and event requests.
        if intent.kind in {INTENT_INVENTORY, INTENT_RELATIONSHIP} and not requirements.historical_holdings:
            return self._inventory_plan(documents, intent)
        if intent.kind == INTENT_HOLDINGS_COMPARISON and not requirements.requires_cross_document_coverage:
            return self._holdings_plan(documents, intent, question, constrained=constrained)
        legal_holdings = (
            self._legal_holdings_evidence(documents, question)
            if (
                intent.kind == INTENT_LEGAL_STATUS
                and not requirements.requires_cross_document_coverage
                and _HOLDINGS_MOVEMENT_CUE.search(question.casefold())
            ) else None
        )
        historical_holdings: HistoricalHoldingsEvidence | None = None
        historical_evidence_note: str | None = None
        if requirements.requires_cross_document_coverage:
            try:
                historical_holdings = self._historical_holdings_evidence(documents, question)
            except (InvalidOperation, KeyError, TypeError, ValueError) as error:
                # This compact spreadsheet transcription is an optional local
                # retrieval aid. Older imported workbooks can contain an
                # incomplete row shape or a number format that it cannot
                # safely normalize. That must not abort the entire cited
                # question before the ordinary evidence retrieval/provider
                # path has a chance to answer it.
                #
                # Keep only the exception class in diagnostics: question and
                # document content must never enter operational logs.
                LOGGER.warning(
                    "Historical holdings transcription unavailable for a matter question (%s).",
                    type(error).__name__,
                )
                historical_evidence_note = (
                    "The local spreadsheet reconstruction aid could not normalize one or more holdings rows. "
                    "The answer uses the remaining cited source evidence and must identify any resulting coverage gap."
                )
        if legal_holdings is not None and legal_holdings.local_answer is not None:
            return self._local_plan(legal_holdings.local_answer, len(documents))
        with self._database.connection() as connection:
            present_names, missing_names = self._named_entities(connection, extraction_ids, question)
        required_terms = sorted({*(identifier.casefold() for identifier in intent.identifiers), *(name.casefold() for name in present_names)})
        notes = [
            *scope_notes,
            *([historical_evidence_note] if historical_evidence_note is not None else []),
            *(f"Not found in the documents used: “{name}”." for name in missing_names),
        ]
        if missing_names and not required_terms:
            return self._local_plan(LocalAnswer(
                intent.kind,
                "None of the names in this question appear in the searchable documents used ("
                + ", ".join(f"“{name}”" for name in missing_names)
                + "). Nothing was sent. Check the spelling, name the relevant document, or change the scope.",
                (), "local-relevance-check",
            ), len(documents), notes=notes)
        # A scoped chronology may include billing impact, but it must reach
        # evidence planning before any local billing-only shortcut can run.
        multi_document_billing = (
            None if requirements.requires_cross_document_coverage
            else self._multi_document_billing_answer(documents, question)
        )
        billing_instruction = self._document_billing_answer(documents, question)
        persisted_billing = self._persisted_billing_answer(documents, question)
        billing_fact_labels = {
            (document_id, reference): label
            for document_id, reference, label in (billing_instruction.source_facts if billing_instruction is not None else ())
        }
        # The deterministic calculator owns only an explicit allocation
        # request. Every other billing conversation goes through the
        # question-aware provider with this instruction as cited evidence.
        document_billing = (
            multi_document_billing
            if multi_document_billing is not None and not requirements.requires_cross_document_coverage
            else persisted_billing
            if persisted_billing is not None and not requirements.requires_cross_document_coverage
            else billing_instruction
            if (
                billing_instruction is not None
                and self._document_billing_intent(question) in {"allocation", "invoiced", "outstanding", "ambiguous"}
                and not requirements.requires_cross_document_coverage
            )
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
                excerpt = (
                    self._historical_evidence_excerpt(source_text)
                    if requirements.requires_cross_document_coverage
                    else self._analysis._evidence_excerpt(source_text)
                )
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

            historical_spreadsheet_ids = frozenset()
            if historical_holdings is not None:
                historical_spreadsheet_ids = frozenset(candidate[0] for candidate in historical_holdings.candidates)
                # These summaries are calculated from cited local cells, not
                # generated by a model. They prevent a one-cell-at-a-time
                # spreadsheet index from dropping Fund B/C under a bounded
                # multi-document question.
                candidates.extend(historical_holdings.candidates)
            if structured is not None:
                # Extracted holdings, transfers and alias statements, each cited
                # to one cell or block.  These are the reconstruction's evidence
                # floor regardless of how a document words its headings.
                # A structured fact replaces a generic transcription of the same
                # cell, which may lack the fund name the record carries.
                structured_facts = {
                    (citation[0], citation[2]): (citation, description)
                    for citation, description in structured.citations()
                    if current_extractions.get(citation[1]) == citation[0]
                }
                for position, candidate in enumerate(candidates):
                    fact = structured_facts.pop((candidate[0], candidate[2]), None)
                    if fact is not None:
                        candidates[position] = (*fact[0], fact[1])
                for (document_id, reference), (citation, description) in structured_facts.items():
                    seen_sources.add((document_id, reference))
                    candidates.append((*citation, description))

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
            retrieval_requirements = self._analysis_requirements(retrieval_question)
            # Legal claim-right wording can be one required part of a factual
            # reconstruction.  It must never suppress dated holdings and
            # transfer retrieval.
            change_question = (
                retrieval_requirements.historical_holdings
                or retrieval_requirements.securities_transfer
                or self._is_change_question(retrieval_question)
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
            # A named investor can be absent from an otherwise indispensable
            # custodian conflict notice.  For an explicit historical
            # reconstruction, the named-entity relevance gate would silently
            # omit precisely that contradicting record, so only use it for
            # narrower lookup questions.
            gate_required_terms: Sequence[str] = () if requirements.requires_cross_document_coverage else required_terms
            required_rows: set[tuple[str, object, object]] = set()
            required_sheets: dict[tuple[str, object], int] = {}
            for source in raw_sources:
                if gate_required_terms and isinstance(source["row_number"], int) and any(
                    term in str(source["text"] or "").casefold() for term in gate_required_terms
                ):
                    sheet_key = (str(source["extraction_id"]), source["sheet_name"])
                    required_rows.add((*sheet_key, source["row_number"]))
                    required_sheets[sheet_key] = max(required_sheets.get(sheet_key, 0), int(source["row_number"]))

            def passes_gate(source: Mapping[str, object]) -> bool:
                if not gate_required_terms:
                    return True
                text = str(source["text"] or "").casefold()
                if any(term in text for term in gate_required_terms):
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
                if str(source["document_id"]) in historical_spreadsheet_ids:
                    continue
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

            if required_terms and not requirements.requires_cross_document_coverage:
                # Earlier AI-review summaries and findings pass the same gate:
                # their cell row or text block must mention a named entity or
                # identifier. (Source excerpts were gated above.)
                candidates = [
                    candidate for candidate in candidates
                    if self._mentions_required_term(connection, candidate, required_terms, neighbour_context)
                ]
            if requirements.requires_cross_document_coverage:
                # Full-text ranking is intentionally selective, but an event
                # notice can use a morphological variant such as
                # "assignment" that does not match a short query token. For
                # a reconstruction, include one locally re-resolved source
                # from each otherwise-missing in-scope document and let the
                # bounded prompt/coverage check disclose any remaining gap.
                represented = {candidate[0] for candidate in [*reconciliation_candidates, *candidates]}
                for document in documents:
                    if document.document_id in represented:
                        continue
                    extraction_id = str(document.extraction_id)
                    block = connection.execute(
                        "SELECT 'page ' || p.page_number || ' block ' || b.block_index AS source_reference "
                        "FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                        "WHERE p.extraction_id = ? AND b.text <> '' ORDER BY p.page_number, b.block_index LIMIT 1",
                        (extraction_id,),
                    ).fetchone()
                    if block is not None:
                        reference = str(block["source_reference"])
                    else:
                        cell = connection.execute(
                            "SELECT source_reference FROM extracted_content WHERE extraction_id = ? AND is_empty = 0 "
                            "AND content_kind <> 'pdf_page' ORDER BY worksheet_index, sheet_name, row_number, column_number LIMIT 1",
                            (extraction_id,),
                        ).fetchone()
                        if cell is None:
                            continue
                        reference = str(cell["source_reference"])
                    add_candidate(
                        document.document_id, extraction_id, reference,
                        "COVERAGE REQUIRED SOURCE EXCERPT",
                        prioritize_for_question=True,
                    )
                # Retrieval ranking can select the assignment paragraph while
                # omitting its adjacent claim-reference/effective-date block.
                # Add a single directly citable event-bearing block per
                # document category; this is bounded local retrieval, not a
                # model-created summary or a whole-document transfer.
                for row in connection.execute(
                    "SELECT p.extraction_id, 'page ' || p.page_number || ' block ' || b.block_index AS source_reference, b.text "
                    "FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                    f"WHERE p.extraction_id IN ({extraction_placeholders}) AND ("
                    "lower(b.text) LIKE '%claim reference%' OR lower(b.text) LIKE '%partial securities transfer%' "
                    "OR lower(b.text) LIKE '%recorded holding%' OR lower(b.text) LIKE '%fee pool%') "
                    "ORDER BY p.extraction_id, p.page_number, b.block_index",
                    extraction_ids,
                ).fetchall():
                    extraction_id = str(row["extraction_id"])
                    document_id = current_extractions.get(extraction_id)
                    if document_id is None:
                        continue
                    add_candidate(
                        document_id, extraction_id, str(row["source_reference"]),
                        "HISTORICAL REQUIRED EVENT SOURCE",
                        prioritize_for_question=True,
                    )
            candidates = reconciliation_candidates + candidates
            if not requirements.requires_cross_document_coverage:
                focused = StructuredEvidence(
                    connection, [(document.document_id, str(document.extraction_id), document.filename) for document in documents],
                ).focused_facts(question_terms(question), days=frozenset((date.month, date.day) for date in question_dates(question)))
                focused = [fact for fact in focused if current_extractions.get(fact[0][1]) == fact[0][0]]
                if focused:
                    focused_sources = {(citation[0], citation[2]) for citation, _description in focused}
                    candidates = [(*citation, description) for citation, description in focused] + [
                        candidate for candidate in candidates if (candidate[0], candidate[2]) not in focused_sources
                    ]
            if required_terms and not requirements.requires_cross_document_coverage:
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
                if requirements.requires_cross_document_coverage:
                    # Historical answers must retain one exact source block
                    # per billing fact. A fee-pool block cannot prove Fund A
                    # or Fund B's basis, or Fund C's exclusion. The derived
                    # Decimal result is added later only if every one of these
                    # direct blocks survives bounded selection.
                    billing_candidates: list[tuple[str, str, str, str, str]] = []
                    for document_id, extraction_id, reference, filename in billing_instruction.citations:
                        evidence = self._analysis._source_evidence(connection, extraction_id, reference)
                        if evidence is None:
                            continue
                        canonical_reference, source_text = evidence
                        # These parser-selected billing blocks are already
                        # fact-level locations. Preserve their literal text,
                        # rather than applying the event-oriented extractor
                        # that can repeat adjacent PDF lines.
                        excerpt = self._bounded_text(" ".join(source_text.split()), 320)
                        if not excerpt:
                            continue
                        label = billing_fact_labels.get(
                            (document_id, canonical_reference), "approved billing instruction fact",
                        )
                        billing_candidates.append(
                            (
                                document_id,
                                extraction_id,
                                canonical_reference,
                                filename,
                                f"DOCUMENT-BACKED BILLING FACT ({label}); EVIDENCE: {excerpt}",
                            ),
                        )
                    # Reinsert direct blocks after removing any ranked copy,
                    # without reusing the synthetic combined instruction as a
                    # substitute for their individual provenance.
                    candidates = [*billing_candidates, *candidates]
                else:
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
        if (
            self._is_overview_question(question)
            or intent.kind == INTENT_COMPARISON
            or requirements.requires_cross_document_coverage
        ):
            by_document: dict[str, list[tuple[str, str, str, str, str]]] = {}
            for candidate in candidates:
                by_document.setdefault(candidate[0], []).append(candidate)

            def historical_priority(candidate: tuple[str, str, str, str, str]) -> int:
                text = candidate[4].casefold()
                score = sum(marker in text for marker in (
                    "document-backed holdings", "fee pool", "eligible participant", "effective date", "partial securities",
                    "assigns", "claim reference", "recorded holding", "conflicts", "unresolved", "nominal units",
                ))
                # A reconstruction needs every dated Fund A/B/C row, not
                # merely one representative cell per spreadsheet.  Give those
                # direct rows absolute precedence over secondary narrative
                # excerpts.  The row facts are the non-negotiable evidence
                # floor for a historical balance answer.
                if "document-backed holdings row" in text:
                    score += 100
                if "document-backed billing fact" in text:
                    # Retain the fee pool, formula, each nominated basis, and
                    # any explicit exclusion as a single mandatory category.
                    # This remains below dated holdings rows, but ahead of
                    # supplementary narrative excerpts.
                    score += 80
                if "historical required event source" in text:
                    score += 50
                # A contradictory statement's narrative can say only that a
                # conflict exists, while its adjacent recorded-holding block
                # contains the actual competing balance.  Prefer that exact
                # balance as the one citable source from the document; the
                # dated snapshot from the other document is selected too.
                if "recorded holding" in text:
                    score += 3
                if "assigns" in text:
                    score += 3
                return score

            grouped = [
                sorted(items, key=historical_priority, reverse=True) if requirements.requires_cross_document_coverage else items
                for items in by_document.values()
            ]
            first_by_document = [items[0] for items in grouped]
            remainder = [candidate for items in grouped for candidate in items[1:]]
            if requirements.requires_cross_document_coverage:
                # Once every selected document has one source, use the
                # remaining bounded slots for the next most fact-bearing
                # records (for example the explicit conflict sentence or an
                # assignment's effective-date block), not merely the first
                # document's second excerpt.
                remainder.sort(key=historical_priority, reverse=True)
            candidates = first_by_document + remainder
            if structured is not None:
                # Every reconstruction step must stay citable: the structured
                # facts take the bounded slots before any narrative excerpt.
                structured_sources = {(citation[0], citation[2]) for citation, _description in structured.citations()}
                candidates = (
                    [candidate for candidate in candidates if (candidate[0], candidate[2]) in structured_sources]
                    + [candidate for candidate in candidates if (candidate[0], candidate[2]) not in structured_sources]
                )

        self._analysis._refresh_configuration()
        controls = self._analysis._cost_controls
        prompt_limit = (
            max(controls.max_input_characters, MAX_HISTORICAL_QUESTION_CHARACTERS)
            if requirements.requires_cross_document_coverage else controls.max_input_characters
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
                if description.startswith("DOCUMENT-BACKED HOLDINGS RECORD;"):
                    compact_description = self._bounded_text(description, 280) or "Document-backed holdings record"
                elif description.startswith("DOCUMENT-BACKED HOLDINGS ROW;"):
                    # A single row has one quantity and one source cell. Keep
                    # its full identity compact so all six Fund A/B/C rows can
                    # fit beside the four event records in a bounded request.
                    compact_description = self._bounded_text(description, 220) or "Document-backed holdings row"
                elif description.startswith("DOCUMENT-BACKED BILLING FACT"):
                    # Each fact is already a short literal PDF block. Keep it
                    # whole rather than squeezing multiple facts into one
                    # arbitrary synthesized instruction/citation.
                    compact_description = self._bounded_text(description, 520) or "Document-backed billing fact"
                elif description.startswith(("DOCUMENT-BACKED BILLING INSTRUCTION;", "DOCUMENT-BACKED HOLDINGS RECORD;")):
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
                    excerpt_limit = 150 if requirements.requires_cross_document_coverage else 240
                    prefix_limit = 60 if requirements.requires_cross_document_coverage else 250
                    prefix = self._bounded_text(description[:marker_index], prefix_limit) or "SOURCE CONTEXT"
                    raw_evidence = description[marker_index + len(evidence_marker):]
                    evidence = (
                        self._bounded_historical_excerpt(raw_evidence, excerpt_limit)
                        if requirements.requires_cross_document_coverage
                        else self._bounded_text(raw_evidence, excerpt_limit)
                    ) or ""
                    evidence = evidence.strip()
                    compact_description = f"{prefix}{evidence_marker} {evidence}"
                else:
                    compact_description = self._bounded_text(description, 500) or "Evidence excerpt"
                line = f"[{key}] DOCUMENT: {compact_filename}; SOURCE: {reference}; {compact_description}"
                # Historical reconstruction must cite every fact to a single
                # displayed source. Adjacent blocks can be useful for a
                # narrow lookup, but including uncitable neighbours here
                # would let a model attach a citation to the wrong block.
                nearby = None if requirements.requires_cross_document_coverage else neighbour_context.get((source_document_id, reference))
                if nearby:
                    # Neighbouring cells/blocks help interpret a short value
                    # (for example a bare quantity).  They are labelled so
                    # the key still identifies the one cited source.
                    line += f"\n  NEARBY CONTEXT FOR [{key}] (same source location; not separately citable): {nearby}"
                lines.append(line)
            evidence_document_ids = {target[0] for target in targets.values()}
            if requirements.requires_cross_document_coverage:
                # Keep enough bounded budget for a source from every required
                # document. The historical prompt itself supplies the same
                # evidence-only and conflict instructions.
                coverage_context = (
                    f"COVERAGE: {len(evidence_document_ids)}/{len(document_ids)} selected searchable documents are represented. "
                    "If any are absent, state the gap. Treat conflicts as separate records; never resolve a balance."
                )
            else:
                coverage_context = (
                    f"DOCUMENT SCOPE: {scope_label}. Use only the excerpts below. If the question needs information "
                    "from a document outside this scope, say that the current scope does not establish it.\n"
                    f"ANALYSIS REQUIREMENTS: {requirements.prompt_text()}.\n"
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

        follow_up_context, follow_up_note = self._budgeted_follow_up_context(
            follow_up, question, material_for([]), prompt_limit,
            historical=requirements.requires_cross_document_coverage,
        )
        if follow_up_note:
            notes.append(follow_up_note)
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
                historical_reconstruction=requirements.requires_cross_document_coverage,
            )
            if len(proposed_prompt) <= prompt_limit:
                selected_candidates.append(candidate)

        selected_targets, selected, citation_key_context, coverage_context = material_for(selected_candidates)
        if not selected_targets:
            raise MatterReviewError(
                "This question is within the input limit, but the current bounded AI budget leaves no room for "
                "locally cited evidence. Shorten the question or ask it in focused parts"
                + (", or ask this follow-up as a new question" if follow_up is not None else "")
                + "; no document content was sent."
            )
        billing_required_keys: tuple[str, ...] = ()
        derived_billing_context = ""
        if billing_instruction is not None and requirements.requires_cross_document_coverage:
            keys_by_source = {
                (document_id, reference): key
                for key, (document_id, _extraction_id, reference, _filename) in selected_targets.items()
            }
            required_sources = tuple(billing_fact_labels)
            missing_billing_sources = [
                (source, billing_fact_labels[source])
                for source in required_sources if source not in keys_by_source
            ]
            if not required_sources:
                coverage_context += (
                    "\nBILLING COVERAGE LIMITATION: The instruction could not be split into directly citable "
                    "fee-pool, rule, participant, and exclusion facts; do not calculate an allocation in this answer."
                )
            elif missing_billing_sources:
                omitted = ", ".join(f"{label} ({reference})" for (_document_id, reference), label in missing_billing_sources)
                coverage_context += (
                    "\nBILLING COVERAGE LIMITATION: Direct evidence was omitted by the bounded question limit: "
                    + omitted
                    + ". Do not state a calculated allocation without all required instruction facts."
                )
            else:
                billing_required_keys = tuple(keys_by_source[source] for source in required_sources)
                derived_billing_context = (
                    "DERIVED LOCAL CALCULATION (not an original document statement): "
                    + billing_instruction.derived_calculation_context
                    + " Cite every approved-instruction input key "
                    + ", ".join(billing_required_keys)
                    + " when reporting this calculation. The approved Fund B billing basis does not resolve any "
                    "competing holdings observation."
                )
        evidence_document_ids = {target[0] for target in selected_targets.values()}
        direct_holding_sources = {
            (document_id, reference)
            for document_id, _extraction_id, reference, _filename in (historical_holdings.citations if historical_holdings is not None else ())
        } | {
            (citation[0], citation[2])
            for citation, description in (structured.citations() if structured is not None else ())
            if description.startswith("DOCUMENT-BACKED HOLDINGS ROW")
        }
        selected_sources = {(document_id, reference) for document_id, _extraction_id, reference, _filename in selected_targets.values()}
        missing_direct_holding_sources = direct_holding_sources - selected_sources
        coverage_citations: list[tuple[str, str, str, str]] = []
        if requirements.requires_cross_document_coverage:
            cited_documents: set[str] = set()
            for document_id, extraction_id, reference, filename in selected_targets.values():
                if document_id not in cited_documents:
                    coverage_citations.append((document_id, extraction_id, reference, filename))
                    cited_documents.add(document_id)
        comparison_question = self._is_comparison_question(question) or requirements.requires_cross_document_coverage
        historical_fact_coverage_complete = (
            not requirements.requires_cross_document_coverage
            or ((historical_holdings is not None or structured is not None) and not missing_direct_holding_sources)
        )
        derived_reconstruction_context = ""
        reconstruction_fallback: tuple[str, tuple[str, ...]] | None = None
        if structured is not None:
            keys_by_source = {
                (document_id, reference): key
                for key, (document_id, _extraction_id, reference, _filename) in selected_targets.items()
            }
            reconstruction_text, used, reconstruction_complete = render_structured(
                structured.reconstruction_lines(),
                lambda citation: f"[{keys_by_source[(citation[0], citation[2])]}]" if (citation[0], citation[2]) in keys_by_source else None,
            )
            limitation = "" if reconstruction_complete else (
                "\nSome cited records were omitted by the bounded request; treat balances that depend on them as unsupported."
            )
            derived_reconstruction_context = (
                "DERIVED LOCAL RECONSTRUCTION (calculated locally from the cited records above; not an original "
                "document statement):\n" + reconstruction_text + limitation
            )
            used_keys = tuple(dict.fromkeys(keys_by_source[(citation[0], citation[2])] for citation in used))
            if used_keys:
                reconstruction_fallback = (
                    "The AI provider did not return a supported historical reconstruction, so this answer is calculated "
                    "locally from the cited extracted records only. It does not cover any other part of the question.\n\n"
                    + reconstruction_text + limitation,
                    used_keys,
                )
        comparison_coverage_complete = not comparison_question or (
            len(evidence_document_ids) == len(document_ids) and historical_fact_coverage_complete
        )
        selected_context = "\n".join(selected)
        evidence_context = (
            selected_context
            + (f"\n\n{derived_reconstruction_context}" if derived_reconstruction_context else "")
            + (f"\n\n{derived_billing_context}" if derived_billing_context else "")
        )
        context = (
            f"CITATION KEYS (return an E-key only):\n{citation_key_context}\n\n"
            f"{follow_up_context}{coverage_context}\n\nEVIDENCE-LINKED PROVISIONAL FINDINGS:\n{evidence_context}"
        )
        prompt = self._analysis._question_prompt(
            question, citation_key_context, "", f"{follow_up_context}{coverage_context}\n{evidence_context}",
            historical_reconstruction=requirements.requires_cross_document_coverage,
        )
        return {
            "context": context,
            "citationKeys": tuple(selected_targets),
            "citationTargets": selected_targets,
            # Fallback only reports literal local observations or a precise
            # limitation. It cannot create a legal conclusion, participant,
            # billing allocation, or inferred relationship.
            "localFallback": reconstruction_fallback or self._local_matter_question_fallback(
                question, selected, selected_targets,
                selected_document_count=len(document_ids), evidence_document_count=len(evidence_document_ids),
                missing_direct_holding_sources=len(missing_direct_holding_sources),
            ),
            "comparisonCoverageComplete": comparison_coverage_complete,
            # The primary classifier may also see legal-assignment language,
            # but a requested dated reconstruction needs its own visible
            # response type.  The additive requirements above still retain
            # the legal-assignment task inside this plan.
            "intent": "historical_reconstruction" if requirements.requires_cross_document_coverage else intent.kind,
            "analysisRequirements": requirements.as_dict(),
            "responseContract": (
                QuestionResponseContract(
                    historical_reconstruction=True,
                    instructions=self._historical_provider_instructions(requirements),
                    max_citations=min(16, len(selected_targets)),
                    required_billing_citation_keys=billing_required_keys,
                ) if requirements.requires_cross_document_coverage else None
            ),
            "notes": notes,
            # Locally calculated holding records shown and cited with the answer.
            "holdingsEvidence": legal_holdings.payload if legal_holdings is not None else None,
            "holdingsCitations": legal_holdings.citations if legal_holdings is not None else (),
            # Generic short answers can expose a locally revalidated coverage
            # reference per included document. Historical provider answers do
            # not reuse those references as citations: coverage alone is not
            # proof for an individual historical conclusion.
            "coverageCitations": tuple(coverage_citations),
            "evidenceItemCounts": self._evidence_item_counts(candidates, selected_candidates),
            "evidenceDocumentIds": tuple(sorted(evidence_document_ids)),
            # Every document whose content can reach the provider in this
            # request: cited excerpts plus any earlier answer reused as
            # follow-up context.  Consent must cover all of them.
            "transmittedDocumentIds": tuple(sorted(evidence_document_ids | follow_up_document_ids)),
            "searchTruncated": search_truncated,
            "inputTokens": controls.estimate_tokens(len(prompt)),
            # A six-source historical answer may need several concise,
            # separately cited sections. It remains within the configured
            # provider cap, while ordinary Q&A retains its shorter limit.
            "maxOutputTokens": (
                controls.max_output_tokens
                if requirements.requires_cross_document_coverage
                else min(800, controls.max_output_tokens)
            ),
            "scope": {
                "sourceItems": len(selected),
                "totalSourceItems": total,
                "excludedSourceItems": max(total - len(selected), 0),
                "truncated": total > len(selected),
                "documentsSelected": len(document_ids),
                "documentsWithEvidence": len(evidence_document_ids),
                "comparisonCoverageComplete": comparison_coverage_complete,
                "directHoldingSourcesRequired": len(direct_holding_sources),
                "directHoldingSourcesIncluded": len(direct_holding_sources) - len(missing_direct_holding_sources),
            },
        }

    @classmethod
    def _question_intent(cls, question: str) -> QuestionIntent:
        return classify_question(
            question,
            calculation=cls._is_document_billing_question(question),
            legal_status=cls._is_legal_status_question(question),
            change=cls._is_change_question(question),
        )

    @staticmethod
    def _analysis_requirements(question: str) -> AnalysisRequirements:
        """Extract additive factual requirements without deciding an outcome.

        This is deliberately smaller than a natural-language parser.  It is
        a retrieval guardrail for the recurrent pattern where one question
        asks about dated positions *and* a transfer *and* a legal assignment.
        """
        query = " ".join(primary_request_text(question).casefold().split())
        historical_chronology = any(phrase in query for phrase in (
            "chronologically", "chronological", "chronology", "timeline", "in chronological order",
        ))
        holdings_comparison = (
            "changes in holdings" in query
            or "change in holdings" in query
            or ("compare" in query and any(term in query for term in ("holding", "position", "subfund", "fund")))
        )
        historical_holdings = any(phrase in query for phrase in (
            "reconstruct holdings", "historical holdings", "holdings as of", "positions as of",
            "before and after", "historical changes", "for each date", "each as-of", "each as of",
        ))
        # Lawyers commonly request snapshot balances as “holdings on 31
        # January and 28 February”, rather than using the more formal “as
        # of” wording.  Two calendar dates plus a holdings target are still a
        # historical comparison across the current scope.  Treating those
        # dates as filename selectors would incorrectly narrow an all-file
        # question before evidence retrieval.
        requested_calendar_dates = question_dates(question)
        holdings_targeted = any(term in query for term in (
            "holding", "position", "subfund", "fund", "balance", "account",
        ))
        # "Reconstruct the holdings…", "reconstruction of each fund's balance".
        historical_holdings = historical_holdings or bool(re.search(r"\breconstruct\w*\b", query) and holdings_targeted)
        explicitly_requested_as_of_dates = (
            "for each date" in query
            or "each as-of" in query
            or "each as of" in query
            or query.count(" as of ") >= 2
            or ("as of" in query and ("before and after" in query or "between" in query))
        )
        transfer_requested = any(term in query for term in (
            "securities transfer", "security transfer", "transfer of", "transfer's effective", "transfer effective",
            "movement of securities", "securities movement",
        )) or bool(re.search(r"\btransfer(?:s|red)?\b", query))
        securities_transfer = transfer_requested and not any(phrase in query for phrase in (
            "no securities transfer", "without a securities transfer", "without securities transfer",
            "no security transfer", "without a security transfer", "does not move securities",
        ))
        claim_assignment = any(term in query for term in (
            "claim-right assignment", "claim right assignment", "claim-rights assignment", "claim rights assignment",
            "legal claim-right", "legal claim right", "assigned claim", "assignment of claim",
        ))
        contradictions = any(term in query for term in (
            "contradict", "contradiction", "conflict", "disputed", "dispute", "do not silently resolve",
        ))
        pro_rata_billing = MatterReviewService._is_document_billing_question(question)
        billing_impact = pro_rata_billing and any(term in query for term in (
            "affect", "impact", "eligibility", "applicable", "recalculate", "recalculation", "resolve", "resolution",
        ))
        invoice_status = any(term in query for term in (
            "invoiced", "issued invoice", "issued invoices", "invoice status", "outstanding", "paid", "payment",
        ))
        # Preserve the established deterministic two-snapshot comparison for
        # a simple “compare March and April holdings” request. Calendar dates
        # become a historical-reconstruction cue only when the same question
        # asks for an event's effective date or an explicit chronology.
        # Merely asking whether a change *proves* a transfer or assignment is
        # still a bounded holdings comparison, not an event reconstruction.
        effective_event_timeline = (
            "effective" in query and (securities_transfer or claim_assignment)
        )
        multiple_as_of_dates = explicitly_requested_as_of_dates or (
            len(requested_calendar_dates) >= 2
            and holdings_targeted
            and (historical_chronology or effective_event_timeline)
        )
        # A detailed request for balances on named dates is historical even
        # when it uses neither the word "reconstruct" nor "historical".
        # A two-snapshot comparison remains on the established deterministic
        # holdings-comparison path unless it separately requests chronology
        # or reconstruction.
        historical_holdings = historical_holdings or historical_chronology or (multiple_as_of_dates and holdings_targeted)
        return AnalysisRequirements(
            historical_holdings=historical_holdings,
            historical_chronology=historical_chronology,
            holdings_comparison=holdings_comparison,
            multiple_as_of_dates=multiple_as_of_dates,
            securities_transfer=securities_transfer,
            claim_assignment=claim_assignment,
            contradictions=contradictions,
            pro_rata_billing=pro_rata_billing,
            billing_impact=billing_impact,
            invoice_status=invoice_status,
        )

    @staticmethod
    def _historical_provider_instructions(requirements: AnalysisRequirements) -> str:
        """Trusted, bounded instructions for the historical response schema.

        The evidence context remains untrusted document data.  This contract
        reaches both provider adapters as instructions, rather than existing
        only in the local prompt-size estimate.
        """
        return (
            AIAnalysisService._historical_reconstruction_instructions()
            + " Produce a historical evidence reconstruction in the required section schema. "
            "Address every requested as-of date and identify each balance as documented, inferred, unresolved, or unsupported. "
            "For every documented event, state its type, effective date, document date when supplied and different, "
            "originating and receiving parties, the security or claim reference, amount or percentage, and direct source key. "
            "Only infer an intervening balance when a cited securities transfer explicitly supports the arithmetic; label it inferred. "
            "Do not call a change between holdings snapshots a transfer unless separate cited transfer evidence establishes it. "
            "For conflicting source records, add a contradiction section and do not choose a balance. "
            "Include a billing section only when the question requests billing and the approved rule and inputs are present. "
            "When billing impact is requested, compare documented events with the explicitly approved billing basis, "
            "retain unresolved conflicts, and say when recalculation needs lawyer confirmation. "
            "Treat issued invoices and outstanding balances as separate verified ledger facts, not proof supplied by an allocation. "
            "Use a distinct cited row for each fund's value, and refer to each fund by its documented name and account. "
            "When a DERIVED LOCAL RECONSTRUCTION is supplied, use its arithmetic and labels, and cite the source keys it cites. "
            "Set unknown true only if no supported finding exists at all. If some findings are supported, set unknown false "
            "and add an explicit unsupported or limitation section for every gap. Keep each section to one or two concise sentences. "
            "Do not infer facts absent from the evidence. "
            f"Requested analysis requirements: {requirements.prompt_text()}."
        )

    @staticmethod
    def _evidence_item_counts(
        candidates: Sequence[tuple[str, str, str, str, str]],
        included: Sequence[tuple[str, str, str, str, str]],
    ) -> Mapping[str, Mapping[str, int]]:
        """Return aggregate source-selection counts without retaining source text."""
        def category(candidate: tuple[str, str, str, str, str]) -> str:
            description = candidate[4].casefold()
            if "document-backed holdings row" in description:
                return "historicalHoldingRows"
            if "historical required event source" in description:
                return "requiredEventSources"
            if "document-backed billing instruction" in description or "document-backed billing fact" in description:
                return "billingInstructions"
            return "supplementaryEvidence"

        result: dict[str, dict[str, int]] = {}
        for candidate in candidates:
            result.setdefault(category(candidate), {"available": 0, "included": 0})["available"] += 1
        for candidate in included:
            result.setdefault(category(candidate), {"available": 0, "included": 0})["included"] += 1
        return {
            name: {"available": counts["available"], "included": counts["included"], "excluded": counts["available"] - counts["included"]}
            for name, counts in sorted(result.items())
        }

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

    def _historical_holdings_evidence(
        self, documents: Sequence[ScopedDocument], question: str,
    ) -> HistoricalHoldingsEvidence | None:
        """Build compact, traceable dated snapshots for a broader reconstruction.

        This does not infer a transfer or confirm an ownership fact. It only
        transcribes the deterministic spreadsheet rows that the provider needs
        in order to distinguish recorded snapshots from the separate notices.
        """
        spreadsheets = [
            document for document in documents
            if document.filename.rsplit(".", 1)[-1].casefold() in _SPREADSHEET_EXTENSIONS
        ]
        if not spreadsheets:
            return None
        extraction_of = {document.document_id: str(document.extraction_id) for document in spreadsheets}
        with self._database.connection() as connection:
            snapshots = HoldingsComparison(connection, [
                HoldingsDocument(document.document_id, document.filename, extraction_of[document.document_id])
                for document in spreadsheets
            ]).snapshot_records()
        if not snapshots:
            return None

        # Keep each quantity cell as its own candidate.  Collapsing all Fund
        # rows behind the first cell in a sheet made Fund B/C visible in the
        # prompt but impossible for the provider to cite directly.  A user
        # may explicitly name only one holdings workbook together with a
        # contradictory statement and billing instruction, so this works for
        # one or more selected spreadsheets rather than requiring a pair.
        citations: list[tuple[str, str, str, str]] = []
        names = {document.document_id: document.filename for document in spreadsheets}
        candidates: list[tuple[str, str, str, str, str]] = []
        for snapshot in snapshots:
            document_id = snapshot.get("documentId")
            quantity = snapshot.get("quantity")
            reference = snapshot.get("quantityReference")
            if not isinstance(document_id, str) or not isinstance(quantity, str) or not isinstance(reference, str):
                continue
            filename = names.get(document_id)
            if filename is None:
                continue
            entities = snapshot.get("entities")
            label = next(
                (
                    str(entity["name"])
                    for entity in entities
                    if isinstance(entity, Mapping) and entity.get("kind") != "investor" and isinstance(entity.get("name"), str)
                ),
                "Unnamed subfund",
            ) if isinstance(entities, Sequence) else "Unnamed subfund"
            account = snapshot.get("account") if isinstance(snapshot.get("account"), str) else "account unknown"
            isin = snapshot.get("isin") if isinstance(snapshot.get("isin"), str) else "ISIN unknown"
            snapshot_date = snapshot.get("date") if isinstance(snapshot.get("date"), str) else "date unknown"
            try:
                display_quantity = grouped_quantity(Decimal(quantity))
            except InvalidOperation:
                continue
            citation = (document_id, extraction_of[document_id], reference, filename)
            if citation not in citations:
                citations.append(citation)
            candidates.append((
                document_id,
                extraction_of[document_id],
                reference,
                filename,
                "DOCUMENT-BACKED HOLDINGS ROW; EVIDENCE: "
                f"Snapshot {snapshot_date}; {label}; nominal {display_quantity}; account {account}; ISIN {isin}.",
            ))
        return HistoricalHoldingsEvidence(tuple(candidates), tuple(citations)) if candidates else None

    @staticmethod
    def _historical_evidence_excerpt(source_text: str) -> str:
        """Keep the fact-bearing lines of a long event notice within the prompt cap."""
        lines = [" ".join(line.split()) for line in source_text.splitlines() if line.strip()]
        cues = (
            "effective date", "as-of date", "partial securities transfer", "nominal", "transfer", "account",
            "claim reference", "assigns", "does not move securities", "recorded holding", "conflicts", "unresolved",
            "fee pool", "allocation rule", "eligible participant", "excluded", "billing period",
        )
        selected: list[str] = []
        for index, line in enumerate(lines):
            if any(cue in line.casefold() for cue in cues):
                # PDF extraction preserves visual line breaks, not semantic
                # sentences. Keep the next two lines as well so an event's
                # destination, assignee, or amount is not dropped merely
                # because it wrapped in the original PDF.
                selected.extend(lines[index:index + 3])
        if not selected:
            selected = lines[:2]
        return " ".join(selected)[:480]

    @staticmethod
    def _bounded_historical_excerpt(value: str, maximum: int) -> str:
        """Preserve both sides of a long event statement in a fixed prompt budget.

        Transfer and assignment notices commonly identify the event at the
        start and the receiving fund, assignee, or effective date at the end.
        Keeping only a prefix can silently remove the material fact that the
        user asked to reconstruct.
        """
        compact = " ".join(value.split())
        if len(compact) <= maximum:
            return compact
        if maximum < 12:
            return compact[:maximum]
        head_length = max((maximum - 3) * 3 // 5, 1)
        tail_length = maximum - 3 - head_length
        return f"{compact[:head_length].rstrip()} … {compact[-tail_length:].lstrip()}"

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
                # An instruction ("NOT APPROVED") or a contextual label ("Fund B")
                # is not a document name, so its absence is not a mismatch.
                if not is_contextual_phrase(variants[0]):
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
        missing_direct_holding_sources: int = 0,
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

        requirements = self._analysis_requirements(question)
        if requirements.requires_cross_document_coverage:
            if evidence_document_count < selected_document_count or missing_direct_holding_sources:
                excerpts = "; ".join(f"“{self._analysis._bounded_question_excerpt(text)}”" for _key, text in evidence[:2])
                missing_documents = max(selected_document_count - evidence_document_count, 0)
                fact_gap = (
                    f" It also lacks direct, separately citable support for {missing_direct_holding_sources} requested holdings row"
                    f"{'s' if missing_direct_holding_sources != 1 else ''}."
                    if missing_direct_holding_sources else ""
                )
                return (
                    f"This historical reconstruction requires evidence across {selected_document_count} selected documents, "
                    f"but the bounded evidence set represents only {evidence_document_count}"
                    + (f" (missing {missing_documents} document{'s' if missing_documents != 1 else ''})" if missing_documents else "")
                    + "."
                    + fact_gap
                    + " It cannot establish all requested "
                    f"holdings, transfers, assignments, conflicts, or billing facts without silently omitting records. "
                    f"The cited excerpts currently available are: {excerpts}.",
                    tuple(key for key, _text in evidence[:2]),
                )
            return (
                "The provider did not return a supported answer for this historical reconstruction. The bounded evidence "
                "set covered every selected document, but no holdings, transfer, claim-right assignment, contradiction, or "
                "billing conclusion has been substituted locally. Review the cited evidence or retry the provider.",
                tuple(key for key, _text in evidence[:min(2, len(evidence))]),
            )

        legal_status = self._is_legal_status_question(question)
        change = self._is_change_question(question)
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

    def _billing_instructions(self, connection: sqlite3.Connection, documents: Sequence[ScopedDocument]) -> list[tuple[BillingInstruction, ScopedDocument, int]]:
        """Approved pro-rata instructions in scope, one per PDF page, with their page number."""
        found = []
        for document in documents:
            pages: dict[int, list[tuple[int, str]]] = {}
            for row in connection.execute(
                "SELECT p.page_number, b.block_index, b.text FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                "WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
                (str(document.extraction_id),),
            ):
                pages.setdefault(int(row["page_number"]), []).append((int(row["block_index"]), str(row["text"] or "")))
            for page, blocks in pages.items():
                instruction = parse_billing_instruction(blocks)
                if instruction is not None:
                    found.append((instruction, document, page))
        return found

    def _instruction_billing_plan(self, documents: Sequence[ScopedDocument], question: str) -> Mapping[str, object] | None:
        """Allocate the single approved instruction in scope, or a read-only NOT APPROVED scenario.

        Used only when the established instruction parser does not already
        recognise the instruction.  Amounts are exact; each fact cites its
        block; a participant label is linked to a fund only when exactly one
        fund of the approving investor carries its series marker.
        """
        if self._document_billing_answer(documents, question) is not None:
            return None
        with self._database.connection() as connection:
            instructions = self._billing_instructions(connection, documents)
            if not instructions:
                return None
            if len(instructions) > 1:
                names = ", ".join(sorted({document.filename for _instruction, document, _page in instructions}))
                return self._local_plan(LocalAnswer(
                    INTENT_CALCULATION, f"{len(instructions)} approved billing instructions are in scope ({names}). Select the one to use; ClaimFlow does not choose between billing instructions.",
                    (), "local-billing-instruction",
                ), len(documents))
            instruction, document, page = instructions[0]
            evidence = StructuredEvidence(connection, [(item.document_id, str(item.extraction_id), item.filename) for item in documents])
            rule_status = connection.execute(
                "SELECT lifecycle_status FROM billing_rule_candidates WHERE source_document_id = ? ORDER BY CASE lifecycle_status WHEN 'approved' THEN 0 ELSE 1 END LIMIT 1",
                (document.document_id,),
            ).fetchone()
        cite = lambda block: (document.document_id, str(document.extraction_id), f"page {page} block {block}", document.filename)  # noqa: E731
        query = " ".join(primary_request_text(question).casefold().split())
        hypothetical = bool(_HYPOTHETICAL_CUE.search(query))
        bases = {participant.label: participant.basis for participant in instruction.participants}
        fee_pool = instruction.fee_pool
        changes: list[str] = []
        if hypothetical:
            for match in _BASIS_OVERRIDE.finditer(question):
                label, amount = (match.group("label") or match.group("label2")), (match.group("amount") or match.group("amount2"))
                participant = self._matching_participant(label, instruction, evidence)
                value = Decimal(amount.replace(",", ""))
                if participant is not None and value > 0:
                    changes.append(f"{participant.label}'s eligible basis from {grouped_quantity(participant.basis)} to {grouped_quantity(value)}")
                    bases[participant.label] = value
            pool = _POOL_OVERRIDE.search(question)
            if pool:
                value = Decimal(pool.group("amount").replace(",", ""))
                if value > 0:
                    changes.append(f"the fee pool from {instruction.currency} {fee_pool:,.2f} to {instruction.currency} {value:,.2f}")
                    fee_pool = value
            if not changes:
                return self._local_plan(LocalAnswer(
                    INTENT_CALCULATION,
                    "This reads as a hypothetical billing scenario, but no revised eligible basis or fee pool for a named participant could be read from it. "
                    "State the change, for example “if Participant B's eligible basis were 700,000”. Nothing was calculated or saved.",
                    (), "local-billing-scenario",
                ), len(documents))
        approved = dict(allocate(instruction.fee_pool, [(participant.label, participant.basis) for participant in instruction.participants]))
        scenario = dict(allocate(fee_pool, [(participant.label, bases[participant.label]) for participant in instruction.participants]))
        currency = instruction.currency
        lines: list[Line] = []
        if hypothetical:
            lines.append(Line(
                "NOT APPROVED — HYPOTHETICAL SCENARIO. This read-only calculation changes " + "; ".join(changes)
                + " as the question asks. It does not alter the approved instruction, any saved calculation or any invoice."
            ))
        header = f"{'Scenario allocation' if hypothetical else 'Allocation'} of the approved {currency} {instruction.fee_pool:,.2f} fee pool"
        if hypothetical and fee_pool != instruction.fee_pool:
            header = f"Scenario allocation of a hypothetical {currency} {fee_pool:,.2f} fee pool (approved: {currency} {instruction.fee_pool:,.2f})"
        period_cites = [cite(instruction.blocks[name]) for name in ("fee_pool", "period") if name in instruction.blocks]
        lines.append(Line(
            f"{header} for {self._display_date(instruction.period_start)} – {self._display_date(instruction.period_end)} "
            + " ".join(f"{{{index}}}" for index in range(len(period_cites))) + ":",
            tuple(dict.fromkeys(period_cites)),
        ))
        for participant in instruction.participants:
            resolved = evidence.resolve_label(participant.label, instruction.approved_by)
            mapping, mapping_cites = "", []
            if resolved is not None:
                fund, accounts = resolved
                mapping = f" ({_escape(fund)}, account {_escape(' / '.join(accounts))}"
                holding = evidence.holding_on(accounts, instruction.period_end, participant.basis)
                if holding is not None:
                    mapping += f"; its {self._display_date(instruction.period_end)} holding is the same {grouped_quantity(participant.basis)} {{1}}"
                    mapping_cites.append(holding)
                mapping += ")"
            basis = bases[participant.label]
            text = f"   • {_escape(participant.label)}{mapping}: eligible nominal {grouped_quantity(basis)}"
            if hypothetical and basis != participant.basis:
                text += f" (hypothetical; approved basis {grouped_quantity(participant.basis)})"
            text += f" → {currency} {scenario[participant.label]:,.2f}"
            if hypothetical:
                text += f" (approved: {currency} {approved[participant.label]:,.2f})"
            lines.append(Line(text + " {0}", (cite(participant.block), *mapping_cites)))
        for exclusion in instruction.exclusions:
            lines.append(Line(f"   • Excluded: {_escape(exclusion.label)} — {_escape(exclusion.reason)} {{0}}", (cite(exclusion.block),)))
        total_basis = sum(bases.values(), Decimal("0"))
        rule_cite = (cite(instruction.blocks["rule"]),) if "rule" in instruction.blocks else ()
        lines.append(Line(
            f"Total eligible basis {grouped_quantity(total_basis)}; total allocated {currency} {sum(scenario.values(), Decimal('0')):,.2f}. "
            "Rule: each participant's eligible nominal ÷ total eligible nominal × fee pool, rounded to cents so the allocations equal the pool"
            + (" {0}." if rule_cite else "."),
            rule_cite,
        ))
        approval_cites = [cite(instruction.blocks[name]) for name in ("approval", "issue_date") if name in instruction.blocks]
        if instruction.approved_by or instruction.issue_date:
            lines.append(Line(
                "The instruction states it was approved" + (f" by {_escape(instruction.approved_by)}" if instruction.approved_by else "")
                + (f" and is dated {self._display_date(instruction.issue_date)}" if instruction.issue_date else "") + " "
                + " ".join(f"{{{index}}}" for index in range(len(dict.fromkeys(approval_cites)))) + ".",
                tuple(dict.fromkeys(approval_cites)),
            ))
        for block, _text in instruction.statements[:1]:
            lines.append(Line("It authorises a calculation only; it is not an invoice and does not identify invoice recipients {0}.", (cite(block),)))
        status = rule_status["lifecycle_status"] if rule_status is not None else None
        lines.append(Line(
            "In ClaimFlow this billing rule is approved for saved calculations; this answer saves nothing." if status == "approved"
            else "In ClaimFlow this billing rule has not been approved on the Billing screen yet; this answer calculates from the document and saves nothing."
        ))
        text, citations, complete = render_structured(lines, lambda citation: f"[{citation[3]}, {citation[2]}]")
        return self._local_plan(LocalAnswer(
            INTENT_CALCULATION, text, tuple(citations), "local-billing-scenario" if hypothetical else "local-billing-instruction",
            frozenset(citation[0] for citation in citations), not complete,
        ), len(documents))

    @staticmethod
    def _matching_participant(label: str, instruction: BillingInstruction, evidence: StructuredEvidence):
        """The instruction participant a question's label refers to, by name, marker or fund."""
        label = label.strip()
        exact = [item for item in instruction.participants if item.label.casefold() == label.casefold()]
        if len(exact) == 1:
            return exact[0]
        marker = participant_marker(label)
        by_marker = [item for item in instruction.participants if marker and participant_marker(item.label) == marker]
        if len(by_marker) == 1:
            return by_marker[0]
        target = evidence.resolve_label(label, instruction.approved_by)
        by_fund = [item for item in instruction.participants if target and evidence.resolve_label(item.label, instruction.approved_by) == target]
        return by_fund[0] if len(by_fund) == 1 else None

    @staticmethod
    def _display_date(value: str | None) -> str:
        if not value:
            return "date not stated"
        parsed = datetime.fromisoformat(value).date()
        return f"{parsed.day} {parsed.strftime('%b')} {parsed.year}"

    def _invoice_recipient_plan(self, documents: Sequence[ScopedDocument]) -> Mapping[str, object] | None:
        """What the documents and records establish about who receives invoices."""
        lines: list[Line] = []
        positive: list[Line] = []
        negative: list[Line] = []
        with self._database.connection() as connection:
            for document in documents:
                for row in connection.execute(
                    "SELECT p.page_number, b.block_index, b.text FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                    "WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
                    (str(document.extraction_id),),
                ):
                    text = " ".join(str(row["text"] or "").split())
                    if not _INVOICE_STATEMENT.search(text):
                        continue
                    citation = (document.document_id, str(document.extraction_id), f"page {int(row['page_number'])} block {int(row['block_index'])}", document.filename)
                    sentence = next((part for part in re.split(r"(?<=[.!?])\s+", text) if _INVOICE_STATEMENT.search(part)), text)
                    target = negative if re.search(r"\bnot\b|\bno\b|\bnever\b", sentence, re.IGNORECASE) else positive
                    target.append(Line(f"   • {_escape(document.filename)} states: “{_escape(sentence[:220])}” {{0}}", (citation,)))
            invoices = int(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0])
            recipients = int(connection.execute("SELECT COUNT(*) FROM invoice_recipients").fetchone()[0])
            evidence = StructuredEvidence(connection, [(document.document_id, str(document.extraction_id), document.filename) for document in documents])
        if positive:
            lines.append(Line("A document names an invoice recipient, but ClaimFlow has not confirmed it as the invoice recipient:"))
            lines.extend(positive)
        else:
            lines.append(Line("The documents used do not identify who should receive the invoices."))
        lines.extend(negative)
        for assignment in evidence.assignments:
            lines.append(Line(
                f"   • {_escape(assignment.assignor)} assigned claim rights under {_escape(assignment.claim_reference)} to {_escape(assignment.assignee)}, "
                f"effective {self._display_date(assignment.effective)} {{0}}. An assignment of claim rights does not by itself make "
                f"{_escape(assignment.assignee)} responsible for, or the recipient of, invoices, and no document used links it to invoicing.",
                (assignment.main_citation,),
            ))
        lines.append(Line(
            f"   • ClaimFlow holds {invoices or 'no'} issued invoice{'s' if invoices != 1 else ''} and "
            f"{recipients or 'no'} confirmed invoice recipient{'s' if recipients != 1 else ''}."
        ))
        lines.append(Line("Invoice recipients need a separate documented agreement or instruction before any invoice is issued."))
        text, citations, complete = render_structured(lines, lambda citation: f"[{citation[3]}, {citation[2]}]")
        return self._local_plan(LocalAnswer(
            INTENT_CALCULATION, text, tuple(citations), "local-invoice-recipients",
            frozenset(citation[0] for citation in citations), not complete,
        ), len(documents))

    def _claim_assignment_plan(self, documents: Sequence[ScopedDocument], question: str) -> Mapping[str, object] | None:
        """Who received claim rights, the dates and scope, and whether securities moved."""
        with self._database.connection() as connection:
            evidence = StructuredEvidence(connection, [(document.document_id, str(document.extraction_id), document.filename) for document in documents])
            terms = question_terms(question)
            scoped = evidence.scope(terms=terms) if terms else evidence
            if scoped.assignments:
                evidence = scoped
            lines = evidence.claim_lines()
            statements_found = False
            if not evidence.assignments:
                for document in documents:
                    for row in connection.execute(
                        "SELECT p.page_number, b.block_index, b.text FROM pdf_page_extractions p JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
                        "WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
                        (str(document.extraction_id),),
                    ):
                        text = " ".join(str(row["text"] or "").split())
                        sentence = next((part for part in re.split(r"(?<=[.!?])\s+", text) if re.search(r"\b(?:does|do|did)\s+not\s+(?:assign|transfer)\b[^.]*\bclaim", part, re.IGNORECASE)), None)
                        if sentence:
                            statements_found = True
                            lines.append(Line(
                                f"   • {_escape(document.filename)} states: “{_escape(sentence[:220])}” {{0}}",
                                ((document.document_id, str(document.extraction_id), f"page {int(row['page_number'])} block {int(row['block_index'])}", document.filename),),
                            ))
            if not evidence.assignments and not statements_found:
                return None
            text, citations, complete = render_structured(lines, lambda citation: f"[{citation[3]}, {citation[2]}]")
        return self._local_plan(LocalAnswer(
            INTENT_LEGAL_STATUS, text, tuple(citations), "local-claim-assignments",
            frozenset(citation[0] for citation in citations), not complete,
        ), len(documents))

    @staticmethod
    def _conflict_request(question: str, follow_up: Mapping[str, str] | None) -> str | None:
        """"identify" for a request to list holdings conflicts, "assess" for which value to trust.

        A trust question qualifies when it is about a position or value, or
        follows a conflict question.
        """
        def conflict(text: str) -> bool:
            query = " ".join(primary_request_text(text).casefold().split())
            return bool(_CONFLICT_CUE.search(query) and _POSITION_CUE.search(query))

        query = " ".join(primary_request_text(question).casefold().split())
        if _TRUST_CUE.search(query) and (conflict(question) or _POSITION_CUE.search(query) or (follow_up is not None and conflict(follow_up["question"]))):
            return "assess"
        return "identify" if conflict(question) else None

    def _conflict_plan(
        self, documents: Sequence[ScopedDocument], question: str, follow_up: Mapping[str, str] | None, *, assess: bool,
    ) -> Mapping[str, object] | None:
        """Answer a holdings-conflict question locally from the competing extracted records.

        Each competing value keeps its own citation; differences and the
        expected balance are exact Decimal arithmetic.  An assessment of which
        value is better supported is never presented as a resolution.  Falls
        through (None) when there are no records, or when a trust question
        finds no conflict to assess.
        """
        terms = question_terms(question) | (question_terms(follow_up["question"]) if follow_up is not None else frozenset())
        start, end = self._question_window(question)
        with self._database.connection() as connection:
            evidence = StructuredEvidence(connection, [(document.document_id, str(document.extraction_id), document.filename) for document in documents])
            if not evidence.has_records:
                return None
            scoped = evidence.scope(start=start, end=end, terms=terms)
            evidence = scoped if scoped.has_records else evidence
            lines = evidence.conflict_lines(assess=assess)
            if assess and len(lines) == 1:
                return None
            text, citations, complete = render_structured(
                [*lines, evidence.coverage_line()], lambda citation: f"[{citation[3]}, {citation[2]}]",
            )
        return self._local_plan(LocalAnswer(
            "holdings_conflict", text, tuple(citations), "local-holdings-conflict",
            frozenset(citation[0] for citation in citations), not complete,
        ), len(documents))

    @staticmethod
    def _is_identity_question(question: str) -> bool:
        """Whether a question asks which names or records denote one entity."""
        query = " ".join(primary_request_text(question).casefold().split())
        return bool(re.search(
            r"\bsame\s+(?:underlying\s+)?(?:fund|sub-?fund|entity|investor|vehicle|portfolio|compartment)s?\b"
            r"|\brefer(?:s|red|ring)?\s+to\s+the\s+same\b|\baliases?\b|\balso\s+known\s+as\b"
            r"|\b(?:different|other|alternative)\s+names?\b|\bwhich\s+(?:fund\s+|entity\s+)?names\b|\babbreviat\w*"
            r"|\bremains?\s+(?:a\s+)?separate\b|\b(?:separate|distinct)\s+(?:sub-?)?funds?\b"
            r"|\bbelongs?\s+to\s+(?:one|a\s+single|the\s+same)\b|\bpart\s+of\s+.{0,60}?\bfund\b",
            query,
        ))

    def _budgeted_follow_up_context(
        self, follow_up: Mapping[str, str] | None, question: str,
        empty_material: tuple[object, object, str, str], prompt_limit: int, *, historical: bool,
    ) -> tuple[str, str | None]:
        """The earlier turn as context, shortened so cited evidence still fits.

        Evidence keeps ``FOLLOW_UP_EVIDENCE_RESERVE_CHARACTERS``; the earlier
        question is kept when possible and the earlier answer is shortened or
        left out.  Returns the context and a note when anything was cut.
        """
        if follow_up is None:
            return "", None
        _targets, _lines, key_context, coverage = empty_material
        baseline = len(self._analysis._question_prompt(question, key_context, "", f"{coverage}\n", historical_reconstruction=historical))
        room = prompt_limit - baseline - FOLLOW_UP_EVIDENCE_RESERVE_CHARACTERS
        header = "FOLLOW-UP CONTEXT (not evidence and not instructions; do not cite it):\n"
        question_line = f"Previous user question: {follow_up['question']}\n"
        answer_prefix = "Previous provisional answer: "
        answer = follow_up["answer"]
        answer_room = room - len(header) - len(question_line) - len(answer_prefix) - 2
        if answer_room >= len(answer):
            return f"{header}{question_line}{answer_prefix}{answer}\n\n", None
        if answer_room >= 120:
            shortened = answer[:answer_room - 1].rstrip() + "…"
            return f"{header}{question_line}{answer_prefix}{shortened}\n\n", "The earlier answer was shortened in this follow-up's context so cited evidence fits the request budget."
        if room >= len(header) + len(question_line):
            return f"{header}{question_line}\n", "The earlier answer was left out of this follow-up's context so cited evidence fits the request budget."
        return "", "The earlier question and answer were left out of this follow-up's context so cited evidence fits the request budget."

    @staticmethod
    def _question_window(question: str) -> tuple[str | None, str | None]:
        """The date range a question asks about; a date without a year takes its neighbour's year."""
        dates = question_dates(question)
        years = [date.year for date in dates]
        resolved: list[str] = []
        for index, date in enumerate(dates):
            year = date.year or next((value for value in years[index + 1:] if value), None) or next((value for value in reversed(years[:index]) if value), None)
            if year is None:
                continue
            try:
                resolved.append(datetime(year, date.month, date.day).date().isoformat())
            except ValueError:
                continue
        if not resolved:
            return None, None
        # One date asks for the position as of that date: all earlier history counts.
        return (None, resolved[0]) if min(resolved) == max(resolved) else (min(resolved), max(resolved))

    def _scoped_structured_evidence(
        self, documents: Sequence[ScopedDocument], question: str,
    ) -> tuple[StructuredEvidence | None, Sequence[ScopedDocument], list[str]]:
        """Structured records for the funds and dates asked about, and the documents that matter.

        Documents with no in-scope record and no mention of an in-scope
        account, fund or investor are left out and listed in a note, so other
        matters in the same workspace neither enter the answer nor make its
        coverage look incomplete.
        """
        start, end = self._question_window(question)
        terms = question_terms(question)
        with self._database.connection() as connection:
            evidence = StructuredEvidence(connection, [(document.document_id, str(document.extraction_id), document.filename) for document in documents])
            if not evidence.has_records:
                return None, documents, []
            scoped = evidence.scope(start=start, end=end, terms=terms)
            if not scoped.has_records:
                return None, documents, []
            if not (start or end or terms):
                return scoped, documents, []
            relevant = set(scoped.relevant_document_ids())
            mentions = [term.casefold() for term in scoped.scope_terms()]
            for document in documents:
                if document.document_id not in relevant and self._document_mentions(connection, str(document.extraction_id), mentions):
                    relevant.add(document.document_id)
        kept = [document for document in documents if document.document_id in relevant]
        excluded = [document.filename for document in documents if document.document_id not in relevant]
        if not kept or not excluded:
            return scoped, documents, []
        listed = ", ".join(excluded[:8]) + (f" and {len(excluded) - 8} more" if len(excluded) > 8 else "")
        return scoped, kept, [
            f"Not used: {len(excluded)} selected {'document has' if len(excluded) == 1 else 'documents have'} no records for the funds "
            f"or dates asked about ({listed})."
        ]

    @staticmethod
    def _document_mentions(connection: sqlite3.Connection, extraction_id: str, terms: Sequence[str]) -> bool:
        for term in terms:
            pattern = f"%{term}%"
            if connection.execute(
                "SELECT 1 FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id "
                "WHERE p.extraction_id = ? AND lower(b.text) LIKE ? LIMIT 1",
                (extraction_id, pattern),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM extracted_content WHERE extraction_id = ? AND lower(value_text) LIKE ? LIMIT 1",
                (extraction_id, pattern),
            ).fetchone():
                return True
        return False

    def _identity_plan(self, documents: Sequence[ScopedDocument], question: str) -> Mapping[str, object] | None:
        """Answer a fund-identity question locally from cited extracted records.

        Names are grouped by account identifier and linked only where a
        document states so for that account.  Nothing is sent to a provider.
        """
        with self._database.connection() as connection:
            start, end = self._question_window(question)
            evidence = StructuredEvidence(connection, [(document.document_id, str(document.extraction_id), document.filename) for document in documents])
            scoped = evidence.scope(start=start, end=end, terms=question_terms(question), include_namesakes=True)
            evidence = scoped if scoped.has_records else evidence
            lines = evidence.identity_lines()
            if not lines:
                return None
            text, citations, complete = render_structured(
                [*lines, evidence.coverage_line()], lambda citation: f"[{citation[3]}, {citation[2]}]",
            )
        return self._local_plan(LocalAnswer(
            "identity_resolution", text, tuple(citations), "local-identity-resolution",
            frozenset(citation[0] for citation in citations), not complete,
        ), len(documents))

    @staticmethod
    def _is_comparison_question(question: str) -> bool:
        query = question.casefold()
        return any(term in query for term in (
            "compare", "comparison", "each isin", "for each isin", "each available date",
            "quantities differ", "which quantities differ", "differences between", "difference between",
            "reconstruct holdings", "historical holdings", "for each date", "before and after", "historical changes",
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
        self, citations: Sequence[Mapping[str, str]], targets: object, *, origin: str | None = None,
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
                    resolved: dict[str, str] = {
                        "documentId": document_id,
                        "filename": filename,
                        "sourceReference": canonical_reference,
                        "evidenceSnippet": excerpt,
                    }
                    if origin is not None:
                        resolved["origin"] = origin
                    validated.append(resolved)
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
        requirements = MatterReviewService._analysis_requirements(question)
        terms: set[str] = set()
        if requirements.pro_rata_billing:
            terms |= {
                "billing", "pro-rata", "pro rata", "fee pool", "allocation", "eligible", "nominal",
                "invoice", "currency", "withdraw", "excluded",
            }
        if requirements.claim_assignment or MatterReviewService._is_legal_status_question(question):
            terms |= {
                "litig", "claim", "right", "assign", "transfer", "withdraw", "settle", "mandate", "participat",
                "entitle", "abtret", "übertrag", "uebertrag", "klage", "anspruch", "verfahren", "vergleich",
                "austritt", "beteilig", "beend", "kündig", "kuendig",
            }
        if requirements.historical_holdings or requirements.securities_transfer or MatterReviewService._is_change_question(question):
            terms |= {
                "change", "changed", "transfer", "trade", "transaction", "movement", "purchase", "sale",
                "sell", "redeem", "redemption", "liquidat", "merge", "split", "delete", "remov", "close",
                "subfund", "sub-fund", "fund", "account", "holding", "position", "quantity", "share",
                "unit", "isin", "date", "effective", "statement", "contradict", "conflict", "dispute",
                "übertrag", "uebertrag", "änder", "aender", "bestand", "konto", "verkauf", "kauf", "tilgung",
                "liquid", "teilfonds", "teil-fonds", "fonds", "depot", "anteil", "anteile", "wertpapier",
                "zuteil", "transaktion", "rückzahlung", "rueckzahlung", "abschreib", "herabsetz", "umtausch",
                "kündigung", "kuendigung",
            }
        if terms:
            return frozenset(terms | {identifier.casefold() for identifier in lookup_identifiers(question)})
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
        if not isinstance(question, str):
            raise MatterReviewError("Enter a question of no more than 5,000 characters.")
        question = question.strip()
        if not question or len(question) > MAX_MATTER_QUESTION_CHARACTERS:
            raise MatterReviewError("Enter a question of no more than 5,000 characters.")
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
