"""Confirmation-gated, provenance-backed local reconciliation.

This service is the only path from extracted observations to durable holdings,
securities transfers, and claim-right assignments. Imports and AI analysis may
stage candidates, but never silently create confirmed business records.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence

from ..storage.service import StorageService
from .identity import IdentityResolver, TargetType, normalize_account_reference, normalize_alias
from .pdf_fields import column_name, extract_page_aliases, extract_page_migrations, extract_page_records, find_amounts, find_dates, labelled_value, parse_date


_CANDIDATE_TYPES = frozenset({"holding_snapshot", "securities_transfer", "claim_assignment"})
# Bump when deterministic reading rules change, so imported documents are
# read again (see ``reread_stale``).  Keep it a plain dated label.
READING_RULES_VERSION = "2026-10-25.5"
_ORIGINS = frozenset({"deterministic", "ai_validated"})
_VALIDATION_STATUSES = frozenset({"supported", "validated_ai", "review_required"})
_PDF_BLOCK_REFERENCE = re.compile(r"^page\s+(\d+)\s+block\s+(\d+)$", re.IGNORECASE)
_PDF_PAGE_REFERENCE = re.compile(r"^page\s+(\d+)$", re.IGNORECASE)
_SHEET_RANGE_REFERENCE = re.compile(
    r"^(?P<sheet>.+)!(?P<start_column>[A-Z]+)(?P<start_row>[1-9][0-9]*)"
    r"(?::(?P<end_column>[A-Z]+)(?P<end_row>[1-9][0-9]*))?$"
)


class ReconciliationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReconciliationService:
    """Stages evidence-supported candidates and confirms explicitly approved records."""

    def __init__(self, storage: StorageService):
        if storage.database is None:
            raise ReconciliationError("STORAGE_UNAVAILABLE", "Local storage is not ready.")
        self._database = storage.database
        self._storage = storage

    def generate(self, params: object) -> Mapping[str, object]:
        """Generate bounded candidates from one already-parsed local document."""
        if not isinstance(params, dict) or set(params) != {"documentId", "includeAi"}:
            raise ReconciliationError("INVALID_DOCUMENT", "Provide a document id and whether validated AI proposals may be used.")
        document_id, include_ai = params["documentId"], params["includeAi"]
        if not isinstance(document_id, str) or not document_id or not isinstance(include_ai, bool):
            raise ReconciliationError("INVALID_DOCUMENT", "The candidate-generation request is invalid.")
        with self._database.connection() as connection:
            context = self._current_extraction(connection, document_id)
            if context is None:
                raise ReconciliationError("DOCUMENT_NOT_PARSED", "The document has no parsed extraction to reconcile.")
            generated = self._deterministic_candidates(connection, context)
            if include_ai:
                generated.extend(self._ai_candidates(connection, context))
        alias_statements = self._stage_alias_statements(context)
        staged: list[Mapping[str, object]] = []
        skipped: list[str] = []
        unchanged = 0
        produced: set[str] = set()
        for candidate in generated:
            existing = self._already_staged_elsewhere(candidate)
            if existing is not None:
                unchanged += 1
                produced.add(existing)
                continue
            try:
                result = self.stage(candidate)
                staged.append(result)
                produced.add(str(result["id"]))
            except ReconciliationError as error:
                # A partially understood source remains local and reviewable;
                # importing a document must not fail because it has no safe
                # business-record interpretation.
                skipped.append(error.code)
        superseded = self._record_read(context, produced)
        return {
            "documentId": document_id,
            "extractionId": context["extraction_id"],
            "parserVersion": context["parser_version"],
            "generated": len(generated),
            "staged": staged,
            "skipped": sorted(skipped),
            "unchanged": unchanged,
            "aliasStatements": alias_statements,
            "superseded": superseded,
        }

    def _record_read(self, context: sqlite3.Row, produced: set[str]) -> int:
        """Record this reading; supersede unconfirmed deterministic candidates it no longer produces.

        A better reading (for example a fuller fund name) must replace, not
        add to, the earlier interpretation of the same document.  Confirmed,
        rejected and AI-origin candidates are never superseded.
        """
        now, read_id = datetime.now(UTC).isoformat(), str(uuid.uuid4())
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO reconciliation_document_reads (id, source_document_id, extraction_id, rules_version, candidate_count, read_at) VALUES (?, ?, ?, ?, ?, ?)",
                (read_id, context["source_document_id"], context["extraction_id"], READING_RULES_VERSION, len(produced), now),
            )
            stale = [
                str(row["id"]) for row in connection.execute(
                    "SELECT c.id FROM reconciliation_candidates c WHERE c.source_document_id = ? AND c.lifecycle_status = 'candidate' AND c.origin = 'deterministic' "
                    "AND NOT EXISTS (SELECT 1 FROM reconciliation_candidate_supersessions s WHERE s.candidate_id = c.id)",
                    (context["source_document_id"],),
                )
                if str(row["id"]) not in produced
            ]
            for candidate_id in stale:
                connection.execute(
                    "INSERT INTO reconciliation_candidate_supersessions (candidate_id, read_id, rules_version, superseded_at) VALUES (?, ?, ?, ?)",
                    (candidate_id, read_id, READING_RULES_VERSION, now),
                )
                self._audit(connection, "reconciliation_candidate_superseded", "reconciliation_candidate", candidate_id, "system", now, {
                    "read_id": read_id, "rules_version": READING_RULES_VERSION,
                })
        return len(stale)

    def reread_stale(self, limit: int = 500) -> Mapping[str, object]:
        """Read documents again whose current extraction predates the current reading rules.

        Runs at backend start.  It stages candidates for review only; nothing
        is confirmed, and confirmed or rejected candidates are untouched.
        """
        with self._database.connection() as connection:
            document_ids = [str(row["id"]) for row in connection.execute(
                "SELECT d.id FROM source_documents d WHERE d.file_format IN ('pdf','xlsx','xls','csv') "
                "AND EXISTS (SELECT 1 FROM document_extractions e WHERE e.source_document_id = d.id AND e.status = 'parsed') "
                "AND NOT EXISTS (SELECT 1 FROM reconciliation_document_reads r JOIN document_extractions e ON e.id = r.extraction_id "
                "  WHERE r.source_document_id = d.id AND r.rules_version = ? AND e.id = (SELECT latest.id FROM document_extractions latest "
                "  WHERE latest.source_document_id = d.id AND latest.status = 'parsed' ORDER BY latest.completed_at DESC, latest.id DESC LIMIT 1)) "
                "ORDER BY d.imported_at, d.id LIMIT ?",
                (READING_RULES_VERSION, limit),
            )]
        read, failed = 0, 0
        for document_id in document_ids:
            try:
                self.generate({"documentId": document_id, "includeAi": False})
                self._generate_billing(document_id)
                read += 1
            except ReconciliationError:
                failed += 1
        if document_ids:
            now = datetime.now(UTC).isoformat()
            with self._database.transaction() as connection:
                self._audit(connection, "reconciliation_documents_reread", "reconciliation_batch", str(uuid.uuid4()), "system", now, {
                    "documentsRead": read, "documentsFailed": failed, "rulesVersion": READING_RULES_VERSION, "trigger": "reading_rules_changed",
                })
        return {"documentsRead": read, "documentsFailed": failed}

    def reread_documents(self, params: object) -> Mapping[str, object]:
        """Re-run record extraction over the stored text of imported documents.

        Uses each document's existing parsed extraction (the vault copy is not
        re-parsed) with the current reading rules.  It only stages new
        candidates for review: nothing is confirmed, and earlier candidates,
        including rejected ones, are kept unchanged.
        """
        if not isinstance(params, dict) or set(params) != {"acknowledged"} or params.get("acknowledged") is not True:
            raise ReconciliationError("CONFIRMATION_REQUIRED", "Explicit acknowledgement is required to re-read documents.")
        limit = 500
        with self._database.connection() as connection:
            document_ids = [str(row["id"]) for row in connection.execute(
                "SELECT d.id FROM source_documents d WHERE d.file_format IN ('pdf','xlsx','xls','csv') "
                "AND EXISTS (SELECT 1 FROM document_extractions e WHERE e.source_document_id = d.id AND e.status = 'parsed') "
                "ORDER BY d.imported_at, d.id LIMIT ?",
                (limit + 1,),
            )]
        read, failed, with_new, new_candidates = 0, 0, 0, 0
        for document_id in document_ids[:limit]:
            with self._database.connection() as connection:
                before = self._candidate_count(connection)
            try:
                self.generate({"documentId": document_id, "includeAi": False})
                self._generate_billing(document_id)
            except ReconciliationError:
                failed += 1
                continue
            with self._database.connection() as connection:
                added = self._candidate_count(connection) - before
            read += 1
            new_candidates += added
            with_new += added > 0
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            summary = {
                "documentsRead": read, "documentsFailed": failed, "documentsWithNewCandidates": with_new,
                "newCandidates": new_candidates, "truncated": len(document_ids) > limit,
            }
            self._audit(connection, "reconciliation_documents_reread", "reconciliation_batch", str(uuid.uuid4()), "user", now, summary)
        return summary

    def stage(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict):
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate fields are invalid.")
        required = {"type", "sourceDocumentId", "sourceReference", "evidenceSnippet", "documentIssueDate", "origin", "payload"}
        optional = {"extractionId", "parserVersion", "confidence", "validationStatus"}
        if not required <= set(params) or not set(params) <= required | optional:
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate fields are invalid.")
        kind, payload = params["type"], params["payload"]
        if (
            kind not in _CANDIDATE_TYPES
            or not isinstance(payload, dict)
            or not all(isinstance(params[key], str) and params[key].strip() for key in ("sourceDocumentId", "sourceReference", "evidenceSnippet"))
            or not (params["documentIssueDate"] == "" or self._date(params["documentIssueDate"]))
            or params["origin"] not in _ORIGINS
        ):
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate evidence is incomplete.")
        self._validate(str(kind), payload)
        validation_status = params.get("validationStatus", "validated_ai" if params["origin"] == "ai_validated" else "supported")
        if validation_status not in _VALIDATION_STATUSES:
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate validation status is invalid.")
        confidence = params.get("confidence", 1.0 if params["origin"] == "deterministic" else None)
        if confidence is not None and (not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1):
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate confidence is invalid.")

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest, now = hashlib.sha256(encoded.encode("utf-8")).hexdigest(), datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            extraction = self._candidate_extraction(connection, str(params["sourceDocumentId"]), params)
            references = self._references(str(params["sourceReference"]))
            if not references or not self._reference_texts(connection, str(extraction["id"]), references):
                raise ReconciliationError("INVALID_EVIDENCE", "Candidate source reference is not stored in this extraction.")
            document_text = self._document_text(connection, str(extraction["id"]))
            resolved_account = (
                kind == "holding_snapshot" and isinstance(payload.get("account"), str)
                and IdentityResolver(connection).resolve("account", payload["account"]).status == "matched"
            )
            if not self._payload_supported(str(kind), payload, str(params["documentIssueDate"]), document_text, resolved_account):
                raise ReconciliationError("UNSUPPORTED_EVIDENCE", "Candidate business values are not all supported by the local source document.")
            if params["origin"] == "ai_validated":
                self._require_validated_ai_proposals(connection, str(params["sourceDocumentId"]), str(extraction["id"]), references)
            row = connection.execute(
                "SELECT id FROM reconciliation_candidates WHERE source_document_id = ? AND candidate_type = ? AND source_reference = ? AND payload_sha256 = ?",
                (params["sourceDocumentId"], kind, params["sourceReference"], digest),
            ).fetchone()
            candidate_id = str(row["id"]) if row else str(uuid.uuid4())
            if row is None:
                connection.execute(
                    "INSERT INTO reconciliation_candidates (id, candidate_type, lifecycle_status, source_document_id, document_issue_date, origin, source_reference, evidence_snippet, payload_json, payload_sha256, created_at, confirmed_at, extraction_id, parser_version, confidence, validation_status) "
                    "VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                    (candidate_id, kind, params["sourceDocumentId"], params["documentIssueDate"], params["origin"], params["sourceReference"], params["evidenceSnippet"], encoded, digest, now, extraction["id"], extraction["parser_version"], confidence, validation_status),
                )
                self._audit(connection, "reconciliation_candidate_staged", str(kind), candidate_id, "system", now, {
                    "origin": params["origin"], "extraction_id": extraction["id"], "validation_status": validation_status,
                })
            if kind == "holding_snapshot":
                # Contradictory source observations must become visible before
                # either one is confirmed.  The helper is idempotent, so it
                # also repairs a previously interrupted generation attempt.
                self._sync_provisional_holding_conflicts(connection, candidate_id, now)
            self._sync_identity_questions(connection, candidate_id, now)
        return self.candidate({"candidateId": candidate_id})

    def candidate(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"candidateId"} or not isinstance(params["candidateId"], str):
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate id is invalid.")
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT c.*, d.imported_at FROM reconciliation_candidates c JOIN source_documents d ON d.id = c.source_document_id WHERE c.id = ?",
                (params["candidateId"],),
            ).fetchone()
        if row is None:
            raise ReconciliationError("CANDIDATE_NOT_FOUND", "Candidate was not found.")
        return self._candidate_result(row)

    def candidates(self, params: object) -> Mapping[str, object]:
        """Return a bounded local review queue; no source facts are changed."""
        if not isinstance(params, dict) or set(params) - {"status"}:
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate filters are invalid.")
        status = params.get("status")
        if status is not None and status not in {"candidate", "confirmed", "rejected"}:
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate status is invalid.")
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT c.*, d.imported_at FROM reconciliation_candidates c JOIN source_documents d ON d.id = c.source_document_id "
                "WHERE NOT EXISTS (SELECT 1 FROM reconciliation_candidate_supersessions s WHERE s.candidate_id = c.id AND c.lifecycle_status = 'candidate') "
                + ("AND c.lifecycle_status = ? " if status else "")
                + "ORDER BY CASE c.lifecycle_status WHEN 'candidate' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, c.created_at, c.id LIMIT 500",
                (status,) if status else (),
            ).fetchall()
            results = [self._candidate_result(row) for row in rows]
            for result, row in zip(results, rows):
                result["reviewReason"] = self._stored_review_reason(connection, row)
            potential_conflicts = self._potential_candidate_conflicts(results)
            potential_conflicts.update(self._persisted_candidate_conflicts(connection))
            for result in results:
                result["potentialConflict"] = potential_conflicts.get(str(result["id"]))
        return {"candidates": results, "truncated": len(rows) == 500}

    def confirm(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"candidateId", "acknowledged"} or params.get("acknowledged") is not True or not isinstance(params.get("candidateId"), str):
            raise ReconciliationError("CONFIRMATION_REQUIRED", "Explicit confirmation is required.")
        now = datetime.now(UTC).isoformat()
        # Questions are committed in their own transaction so the refusal
        # below leaves the "same as?" question visible in the issue queue.
        with self._database.transaction() as connection:
            row = self._candidate_row(connection, str(params["candidateId"]))
            pending_identity = row["lifecycle_status"] == "candidate" and self._sync_identity_questions(connection, str(row["id"]), now) > 0
        if pending_identity:
            raise ReconciliationError("IDENTITY_REVIEW_REQUIRED", "A name or account in this record may refer to an existing record. Answer the open identity question first.")
        with self._database.transaction() as connection:
            row = self._candidate_row(connection, str(params["candidateId"]))
            if row["lifecycle_status"] == "confirmed":
                return {"id": row["id"], "type": row["candidate_type"], "status": "confirmed"}
            if row["lifecycle_status"] == "rejected":
                raise ReconciliationError("CANDIDATE_REJECTED", "A rejected candidate cannot be confirmed.")
            if self._superseded(connection, str(row["id"])):
                raise ReconciliationError("CANDIDATE_SUPERSEDED", "This reading was replaced when the document was read again; review the current record instead.")
            if self._candidate_has_open_holding_conflict(connection, str(row["id"])):
                raise ReconciliationError("CONFLICT_REQUIRES_REVIEW", "A conflicting dated holding must remain provisional until reviewed.")
            if self._holding_conflict(connection, row, json.loads(str(row["payload_json"]))):
                raise ReconciliationError("CONFLICT_REQUIRES_REVIEW", "A conflicting dated holding must remain provisional until reviewed.")
            result = self._confirm_row(connection, row, now)
            self._audit(connection, "reconciliation_confirmed", str(row["candidate_type"]), str(result["id"]), "user", now, {"candidate_id": row["id"]})
            return result

    def apply_policy(self, params: object) -> Mapping[str, object]:
        """Confirm a reviewed batch of deterministic, non-conflicting candidates.

        This is deliberately not automatic confirmation. It gives a lawyer one
        bounded acknowledgement for routine holdings/transfers while keeping
        legal assignments and exceptions in the review queue.
        """
        if (
            not isinstance(params, dict) or set(params) != {"candidateIds", "policy", "acknowledged"}
            or params.get("policy") != "reviewed_batch" or params.get("acknowledged") is not True
            or not isinstance(params.get("candidateIds"), list) or not params["candidateIds"]
            or len(params["candidateIds"]) > 100 or any(not isinstance(value, str) for value in params["candidateIds"])
        ):
            raise ReconciliationError("CONFIRMATION_REQUIRED", "A reviewed batch requires explicit acknowledgement.")
        candidate_ids = list(dict.fromkeys(params["candidateIds"]))
        now, confirmed, review_required = datetime.now(UTC).isoformat(), [], []
        with self._database.transaction() as connection:
            rows = [self._candidate_row(connection, candidate_id) for candidate_id in candidate_ids]
            rows.sort(key=self._policy_order)
            for row in rows:
                if row["lifecycle_status"] == "rejected":
                    review_required.append({"candidateId": str(row["id"]), "reason": "candidate_rejected"})
                    continue
                reason = self._policy_review_reason(connection, row)
                if reason is not None:
                    self._mark_review_required(connection, row, reason, now)
                    review_required.append({"candidateId": str(row["id"]), "reason": reason})
                    continue
                if row["lifecycle_status"] == "confirmed":
                    confirmed.append({"id": row["id"], "type": row["candidate_type"], "status": "confirmed"})
                    continue
                confirmed.append(self._confirm_row(connection, row, now))
            self._audit(connection, "reconciliation_reviewed_batch_confirmed", "reconciliation_batch", str(uuid.uuid4()), "user", now, {
                "candidate_ids": candidate_ids, "confirmed_count": len(confirmed), "review_required_count": len(review_required),
            })
        return {"policy": "reviewed_batch", "confirmed": confirmed, "reviewRequired": review_required}

    def reject(self, params: object) -> Mapping[str, object]:
        """Audit a rejection while retaining the candidate and its provenance."""
        if (
            not isinstance(params, dict) or set(params) != {"candidateId", "reason", "acknowledged"}
            or params.get("acknowledged") is not True or not isinstance(params.get("candidateId"), str)
            or not isinstance(params.get("reason"), str) or not params["reason"].strip()
            or len(params["reason"].strip()) > 500
        ):
            raise ReconciliationError("CONFIRMATION_REQUIRED", "An explicit rejection reason is required.")
        now, reason = datetime.now(UTC).isoformat(), params["reason"].strip()
        with self._database.transaction() as connection:
            row = self._candidate_row(connection, str(params["candidateId"]))
            if row["lifecycle_status"] == "confirmed":
                raise ReconciliationError("CANDIDATE_CONFIRMED", "A confirmed record must be corrected through a separate audited workflow.")
            if row["lifecycle_status"] != "rejected":
                connection.execute("UPDATE reconciliation_candidates SET lifecycle_status = 'rejected' WHERE id = ?", (row["id"],))
                self._audit(connection, "reconciliation_candidate_rejected", str(row["candidate_type"]), str(row["id"]), "user", now, {"reason": reason})
        return self.candidate({"candidateId": str(params["candidateId"])})

    def identity_questions(self, params: object) -> Mapping[str, object]:
        """Return "same as?" questions raised when a name nearly matches a record."""
        if not isinstance(params, dict) or set(params) - {"status"}:
            raise ReconciliationError("INVALID_IDENTITY_QUESTION", "Identity question filters are invalid.")
        status = params.get("status", "open")
        if status not in {"open", "linked", "distinct"}:
            raise ReconciliationError("INVALID_IDENTITY_QUESTION", "Identity question status is invalid.")
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT q.*, c.candidate_type, c.source_reference, c.document_issue_date FROM identity_link_questions q "
                "JOIN reconciliation_candidates c ON c.id = q.candidate_id WHERE q.status = ? ORDER BY q.created_at, q.id LIMIT 200",
                (status,),
            ).fetchall()
            questions = [self._identity_question_result(connection, row) for row in rows]
        return {"questions": questions, "truncated": len(rows) == 200}

    def resolve_identity_question(self, params: object) -> Mapping[str, object]:
        """Record the user's answer; "same" stores the spelling as an alias.

        Nothing is merged retroactively: the answer only changes how this
        spelling resolves from now on, and it is audited.
        """
        if (
            not isinstance(params, dict) or set(params) != {"questionId", "decision", "acknowledged"}
            or params.get("acknowledged") is not True or not isinstance(params.get("questionId"), str)
            or params.get("decision") not in {"same", "distinct"}
        ):
            raise ReconciliationError("CONFIRMATION_REQUIRED", "An explicit same-or-distinct answer is required.")
        decision = str(params["decision"])
        status = "linked" if decision == "same" else "distinct"
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT q.*, c.document_issue_date FROM identity_link_questions q JOIN reconciliation_candidates c ON c.id = q.candidate_id WHERE q.id = ?",
                (params["questionId"],),
            ).fetchone()
            if row is None:
                raise ReconciliationError("IDENTITY_QUESTION_NOT_FOUND", "Identity question was not found.")
            if row["status"] != "open":
                if row["status"] != status:
                    raise ReconciliationError("IDENTITY_QUESTION_RESOLVED", "This identity question was already answered differently.")
            else:
                if decision == "same":
                    if self._identity_target_label(connection, str(row["target_type"]), str(row["suggested_target_id"])) is None:
                        raise ReconciliationError("IDENTITY_TARGET_NOT_FOUND", "The suggested record no longer exists.")
                if decision == "same" and row["target_type"] != "account":
                    IdentityResolver(connection).record_alias(
                        row["target_type"], str(row["suggested_target_id"]), str(row["observed_text"]), origin="user_confirmed",
                        source_document_id=str(row["source_document_id"]), first_seen_on=row["document_issue_date"] or None, now=now,
                    )
                connection.execute("UPDATE identity_link_questions SET status = ?, resolved_at = ? WHERE id = ?", (status, now, row["id"]))
                connection.execute(
                    "UPDATE issues SET status = 'resolved', resolved_at = ? WHERE status = 'open' AND affected_type = 'identity_link_question' AND affected_id = ?",
                    (now, row["id"]),
                )
                self._audit(connection, "identity_link_question_answered", str(row["target_type"]), str(row["suggested_target_id"]), "user", now, {
                    "question_id": row["id"], "decision": decision,
                })
                self._refresh_review_status(connection, now)
            result_row = connection.execute(
                "SELECT q.*, c.candidate_type, c.source_reference, c.document_issue_date FROM identity_link_questions q "
                "JOIN reconciliation_candidates c ON c.id = q.candidate_id WHERE q.id = ?",
                (row["id"],),
            ).fetchone()
            return self._identity_question_result(connection, result_row)

    def backfill_provisional_holding_conflicts(self, params: object) -> Mapping[str, object]:
        """Explicitly repair issue records for candidates created before v15.

        This does not confirm, reject, delete, or alter any domain record. It
        is deliberately an acknowledged maintenance operation so a caller must
        name the target local workspace rather than triggering it on startup.
        """
        if not isinstance(params, dict) or set(params) != {"acknowledged"} or params.get("acknowledged") is not True:
            raise ReconciliationError("CONFIRMATION_REQUIRED", "Explicit acknowledgement is required for conflict backfill.")
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            before = connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open' AND reason = 'conflicting dated holding'").fetchone()[0]
            rows = connection.execute(
                "SELECT id FROM reconciliation_candidates WHERE candidate_type = 'holding_snapshot' ORDER BY created_at, id"
            ).fetchall()
            for row in rows:
                self._sync_provisional_holding_conflicts(connection, str(row["id"]), now)
            after = connection.execute("SELECT COUNT(*) FROM issues WHERE status = 'open' AND reason = 'conflicting dated holding'").fetchone()[0]
            conflicts = connection.execute("SELECT COUNT(*) FROM reconciliation_holding_conflicts WHERE status = 'open'").fetchone()[0]
        return {
            "scannedCandidates": len(rows), "openConflicts": conflicts,
            "issuesCreated": int(after) - int(before),
        }

    def holdings_as_of(self, params: object) -> Mapping[str, object]:
        """Return an evidence-backed view as of a date without inventing events.

        A snapshot dated exactly on the requested date is documented.  A later
        date starts from the latest earlier snapshot and applies only confirmed
        transfers.  That result is labelled reconstructed or inferred, rather
        than asserting that no undocumented intervening transaction occurred.
        """
        filters = self._as_of_filters(params)
        day = str(filters.pop("asOfDate"))
        with self._database.connection() as connection:
            rows = self._holding_rows(connection, filters, as_of_date=day)
            latest: dict[tuple[str, str], sqlite3.Row] = {}
            for row in rows:
                # Rows are chronologically ascending so the final row is the
                # latest documented snapshot for this account/security pair.
                latest[(str(row["account_id"]), str(row["security_id"]))] = row

            positions: list[Mapping[str, object]] = []
            for (account_id, security_id), row in latest.items():
                quantity = Decimal(str(row["quantity"]))
                snapshot_date = str(row["position_date"])
                transfers = self._transfer_rows_for_position(
                    connection, account_id, security_id, snapshot_date, day
                )
                applied: list[Mapping[str, object]] = []
                same_day: list[Mapping[str, object]] = []
                for transfer in transfers:
                    event = self._transfer_result(connection, transfer)
                    if str(transfer["effective_date"]) == snapshot_date:
                        # A dated statement and a same-day transfer establish
                        # no reliable intraday ordering. Never net it here.
                        same_day.append(event)
                        continue
                    if transfer["from_account_id"] == account_id:
                        quantity -= Decimal(str(transfer["quantity"]))
                    if transfer["to_account_id"] == account_id:
                        quantity += Decimal(str(transfer["quantity"]))
                    applied.append(event)

                if snapshot_date == day:
                    reconstruction_state = "documented"
                    assumptions: list[str] = []
                elif applied:
                    reconstruction_state = "reconstructed"
                    assumptions = [
                        "This balance starts with the latest documented snapshot and applies only the listed confirmed securities transfers.",
                        "Undocumented intervening events cannot be ruled out.",
                    ]
                else:
                    reconstruction_state = "inferred"
                    assumptions = [
                        "This carries forward the latest documented snapshot; no conclusion is made about undocumented intervening events.",
                    ]
                if same_day:
                    assumptions.append("A transfer and the source snapshot share a date, so their intraday ordering is unresolved.")

                positions.append({
                    "investor": row["investor"],
                    "subfund": row["subfund"],
                    "account": row["external_reference"],
                    "isin": row["isin"],
                    "quantity": self._decimal(quantity),
                    "snapshotDate": snapshot_date,
                    # Keep the original status field stable for existing
                    # callers; reconstructionState carries temporal meaning.
                    "status": "confirmed",
                    "competingQuantities": [],
                    "reconstructionState": reconstruction_state,
                    "assumptions": assumptions,
                    "snapshot": self._holding_result(connection, row),
                    "appliedTransfers": applied,
                    "sameDateTransfers": same_day,
                })

            earliest = self._earliest_snapshot_date(connection, filters)
            limitations = []
            if not positions and earliest is not None and day < earliest:
                limitations.append("The requested date is before the earliest documented holdings snapshot; holdings are unknown.")
            return {
                "asOfDate": day,
                "filters": filters,
                "positions": positions,
                "limitations": limitations,
            }

    def holdings_timeline(self, params: object) -> Mapping[str, object]:
        """Return documented snapshot and transfer events in one local timeline."""
        filters = self._timeline_filters(params)
        with self._database.connection() as connection:
            snapshots = [self._holding_result(connection, row) for row in self._holding_rows(connection, filters)]
            transfers = [self._transfer_result(connection, row) for row in self._transfer_rows(connection, filters)]
            events = [
                {"kind": "documented_holding_snapshot", "date": item["positionDate"], "record": item}
                for item in snapshots
            ] + [
                {"kind": "securities_transfer", "date": item["effectiveDate"], "record": item}
                for item in transfers
            ]
            events.sort(key=lambda item: (str(item["date"]), str(item["kind"]), str(item["record"]["id"])))
            return {
                "filters": filters,
                "events": events[:500],
                "truncated": len(events) > 500,
                "limitations": [
                    "The timeline contains confirmed records only. Absence of an event is not evidence that no event occurred."
                ],
            }

    def transfer_history(self, params: object) -> Mapping[str, object]:
        filters = self._timeline_filters(params)
        with self._database.connection() as connection:
            events = [self._transfer_result(connection, row) for row in self._transfer_rows(connection, filters)]
        return {"filters": filters, "events": events[:500], "truncated": len(events) > 500}

    def claim_assignment_history(self, params: object) -> Mapping[str, object]:
        filters = self._assignment_filters(params)
        with self._database.connection() as connection:
            rows = self._assignment_rows(connection, filters)
            events = [self._assignment_result(connection, row) for row in rows]
        return {
            "filters": filters,
            "events": events[:500],
            "truncated": len(events) > 500,
            "limitation": "Claim-right events are separate from securities holdings and never change a securities balance.",
        }

    def open_discrepancies(self, params: object) -> Mapping[str, object]:
        filters = self._timeline_filters(params)
        with self._database.connection() as connection:
            issues = self._open_discrepancy_results(connection, filters)
        return {"filters": filters, "issues": issues[:200], "truncated": len(issues) > 200}

    def reconciliation_result(self, params: object) -> Mapping[str, object]:
        """Compose the bounded read-only reconciliation result for one date."""
        filters = self._as_of_filters(params)
        day = str(filters["asOfDate"])
        shared = {key: value for key, value in filters.items() if key != "asOfDate"}
        assignment_filters = {
            key: value for key, value in shared.items()
            if key in {"isin", "claimReference", "fromEntity", "toEntity"}
        }
        return {
            "asOfDate": day,
            "holdings": self.holdings_as_of(filters),
            "timeline": self.holdings_timeline({**shared, "toDate": day}),
            "securitiesTransfers": self.transfer_history({**shared, "toDate": day}),
            "claimRightAssignments": self.claim_assignment_history({**assignment_filters, "toDate": day}),
            "openDiscrepancies": self.open_discrepancies(shared),
        }

    def _generate_billing(self, document_id: str) -> None:
        """Stage the document's approved billing instruction, if any (never approves it)."""
        from .billing import BillingError, BillingService

        try:
            BillingService(self._storage).generate({"documentId": document_id})
        except BillingError:
            pass

    @staticmethod
    def _superseded(connection: sqlite3.Connection, candidate_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM reconciliation_candidate_supersessions WHERE candidate_id = ?", (candidate_id,),
        ).fetchone() is not None

    @staticmethod
    def _candidate_count(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT COUNT(*) FROM reconciliation_candidates").fetchone()[0])

    def _already_staged_elsewhere(self, candidate: Mapping[str, object]) -> str | None:
        """The id of the same record already staged from this document under another reference.

        Reading rules may cite different blocks over time; the same fact from
        the same document and origin must not become a second candidate.  An
        exact repeat is still passed to ``stage`` so its idempotent repair of
        conflicts and identity questions runs.
        """
        payload = candidate.get("payload")
        if not isinstance(payload, dict):
            return None
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT id FROM reconciliation_candidates WHERE source_document_id = ? AND candidate_type = ? AND origin = ? AND payload_sha256 = ? AND source_reference <> ? "
                "ORDER BY CASE lifecycle_status WHEN 'confirmed' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, created_at LIMIT 1",
                (candidate.get("sourceDocumentId"), candidate.get("type"), candidate.get("origin"), digest, candidate.get("sourceReference")),
            ).fetchone()
            return str(row["id"]) if row is not None else None

    # Read-only historical-query helpers ---------------------------------

    @classmethod
    def _as_of_filters(cls, params: object) -> dict[str, str]:
        if not isinstance(params, dict) or "asOfDate" not in params:
            raise ReconciliationError("INVALID_DATE", "Use YYYY-MM-DD for the as-of date.")
        allowed = {"asOfDate", "investor", "subfund", "account", "isin"}
        if set(params) - allowed or not cls._date(params.get("asOfDate")):
            raise ReconciliationError("INVALID_DATE", "Use YYYY-MM-DD for the as-of date.")
        return cls._identity_filters(params, date_keys=("asOfDate",))

    @classmethod
    def _timeline_filters(cls, params: object) -> dict[str, str]:
        if not isinstance(params, dict):
            raise ReconciliationError("INVALID_FILTER", "Timeline filters are invalid.")
        allowed = {"fromDate", "toDate", "investor", "subfund", "account", "isin"}
        if set(params) - allowed:
            raise ReconciliationError("INVALID_FILTER", "Timeline filters are invalid.")
        result = cls._identity_filters(params, date_keys=("fromDate", "toDate"))
        if result.get("fromDate") and result.get("toDate") and result["fromDate"] > result["toDate"]:
            raise ReconciliationError("INVALID_DATE_RANGE", "The start date must not be after the end date.")
        return result

    @classmethod
    def _assignment_filters(cls, params: object) -> dict[str, str]:
        if not isinstance(params, dict):
            raise ReconciliationError("INVALID_FILTER", "Assignment filters are invalid.")
        allowed = {"fromDate", "toDate", "claimReference", "fromEntity", "toEntity", "isin"}
        if set(params) - allowed:
            raise ReconciliationError("INVALID_FILTER", "Assignment filters are invalid.")
        result = cls._identity_filters(params, date_keys=("fromDate", "toDate"))
        for key in ("claimReference", "fromEntity", "toEntity"):
            value = params.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 256:
                    raise ReconciliationError("INVALID_FILTER", "Assignment filters are invalid.")
                result[key] = value.strip()
        if result.get("fromDate") and result.get("toDate") and result["fromDate"] > result["toDate"]:
            raise ReconciliationError("INVALID_DATE_RANGE", "The start date must not be after the end date.")
        return result

    @classmethod
    def _identity_filters(cls, params: Mapping[str, object], *, date_keys: Sequence[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key in date_keys:
            value = params.get(key)
            if value is not None:
                if not cls._date(value):
                    raise ReconciliationError("INVALID_DATE", "Dates must use YYYY-MM-DD.")
                result[key] = str(value)
        for key in ("investor", "subfund", "account", "isin"):
            value = params.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ReconciliationError("INVALID_FILTER", "Identity filters are invalid.")
            normalized = value.strip()
            if key == "isin":
                normalized = normalized.upper().replace(" ", "")
                if len(normalized) != 12:
                    raise ReconciliationError("INVALID_FILTER", "The ISIN filter is invalid.")
            result[key] = normalized
        return result

    def _holding_rows(
        self, connection: sqlite3.Connection, filters: Mapping[str, str], *, as_of_date: str | None = None
    ) -> list[sqlite3.Row]:
        clauses, values = ["1 = 1"], []
        if as_of_date is not None:
            clauses.append("p.position_date <= ?")
            values.append(as_of_date)
        if filters.get("fromDate"):
            clauses.append("p.position_date >= ?")
            values.append(filters["fromDate"])
        if filters.get("toDate"):
            clauses.append("p.position_date <= ?")
            values.append(filters["toDate"])
        for column, key in (("investor_entity.display_name", "investor"), ("fund.name", "subfund"), ("a.external_reference", "account"), ("s.isin", "isin")):
            if filters.get(key):
                clauses.append(f"{column} = ?")
                values.append(filters[key])
        return connection.execute(
            "SELECT p.*, a.external_reference, s.isin, fund.name AS subfund, investor_entity.display_name AS investor "
            "FROM holding_positions p "
            "JOIN accounts a ON a.id = p.account_id "
            "JOIN investors i ON i.id = a.investor_id "
            "JOIN legal_entities investor_entity ON investor_entity.id = i.legal_entity_id "
            "LEFT JOIN investment_vehicles fund ON fund.id = a.investment_vehicle_id "
            "JOIN securities s ON s.id = p.security_id "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY p.position_date, a.external_reference, s.isin, p.created_at, p.id",
            tuple(values),
        ).fetchall()

    def _transfer_rows(self, connection: sqlite3.Connection, filters: Mapping[str, str]) -> list[sqlite3.Row]:
        clauses, values = ["1 = 1"], []
        if filters.get("fromDate"):
            clauses.append("t.effective_date >= ?")
            values.append(filters["fromDate"])
        if filters.get("toDate"):
            clauses.append("t.effective_date <= ?")
            values.append(filters["toDate"])
        if filters.get("investor"):
            clauses.append("(from_entity.display_name = ? OR to_entity.display_name = ?)")
            values.extend((filters["investor"], filters["investor"]))
        if filters.get("subfund"):
            clauses.append("(from_fund.name = ? OR to_fund.name = ?)")
            values.extend((filters["subfund"], filters["subfund"]))
        if filters.get("account"):
            clauses.append("(from_account.external_reference = ? OR to_account.external_reference = ?)")
            values.extend((filters["account"], filters["account"]))
        if filters.get("isin"):
            clauses.append("s.isin = ?")
            values.append(filters["isin"])
        return connection.execute(
            "SELECT t.*, s.isin, from_account.external_reference AS from_account, to_account.external_reference AS to_account, "
            "from_fund.name AS from_subfund, to_fund.name AS to_subfund, "
            "from_entity.display_name AS from_investor, to_entity.display_name AS to_investor "
            "FROM securities_transfers t "
            "JOIN securities s ON s.id = t.security_id "
            "JOIN accounts from_account ON from_account.id = t.from_account_id "
            "JOIN accounts to_account ON to_account.id = t.to_account_id "
            "JOIN investors from_investor ON from_investor.id = from_account.investor_id "
            "JOIN investors to_investor ON to_investor.id = to_account.investor_id "
            "JOIN legal_entities from_entity ON from_entity.id = from_investor.legal_entity_id "
            "JOIN legal_entities to_entity ON to_entity.id = to_investor.legal_entity_id "
            "LEFT JOIN investment_vehicles from_fund ON from_fund.id = from_account.investment_vehicle_id "
            "LEFT JOIN investment_vehicles to_fund ON to_fund.id = to_account.investment_vehicle_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY t.effective_date, t.created_at, t.id",
            tuple(values),
        ).fetchall()

    def _transfer_rows_for_position(
        self, connection: sqlite3.Connection, account_id: str, security_id: str, snapshot_date: str, day: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT t.*, s.isin, from_account.external_reference AS from_account, to_account.external_reference AS to_account, "
            "from_fund.name AS from_subfund, to_fund.name AS to_subfund, "
            "from_entity.display_name AS from_investor, to_entity.display_name AS to_investor "
            "FROM securities_transfers t JOIN securities s ON s.id = t.security_id "
            "JOIN accounts from_account ON from_account.id = t.from_account_id "
            "JOIN accounts to_account ON to_account.id = t.to_account_id "
            "JOIN investors from_investor ON from_investor.id = from_account.investor_id "
            "JOIN investors to_investor ON to_investor.id = to_account.investor_id "
            "JOIN legal_entities from_entity ON from_entity.id = from_investor.legal_entity_id "
            "JOIN legal_entities to_entity ON to_entity.id = to_investor.legal_entity_id "
            "LEFT JOIN investment_vehicles from_fund ON from_fund.id = from_account.investment_vehicle_id "
            "LEFT JOIN investment_vehicles to_fund ON to_fund.id = to_account.investment_vehicle_id "
            "WHERE t.security_id = ? AND (t.from_account_id = ? OR t.to_account_id = ?) "
            "AND t.effective_date >= ? AND t.effective_date <= ? ORDER BY t.effective_date, t.created_at, t.id",
            (security_id, account_id, account_id, snapshot_date, day),
        ).fetchall()

    def _assignment_rows(self, connection: sqlite3.Connection, filters: Mapping[str, str]) -> list[sqlite3.Row]:
        clauses, values = ["1 = 1"], []
        if filters.get("fromDate"):
            clauses.append("assignment.effective_date >= ?")
            values.append(filters["fromDate"])
        if filters.get("toDate"):
            clauses.append("assignment.effective_date <= ?")
            values.append(filters["toDate"])
        if filters.get("claimReference"):
            clauses.append("claim.claim_reference = ?")
            values.append(filters["claimReference"])
        if filters.get("fromEntity"):
            clauses.append("from_entity.display_name = ?")
            values.append(filters["fromEntity"])
        if filters.get("toEntity"):
            clauses.append("to_entity.display_name = ?")
            values.append(filters["toEntity"])
        if filters.get("isin"):
            clauses.append("security.isin = ?")
            values.append(filters["isin"])
        return connection.execute(
            "SELECT assignment.*, claim.claim_reference, security.isin, from_entity.display_name AS from_entity, to_entity.display_name AS to_entity "
            "FROM claim_right_assignments assignment "
            "JOIN claim_rights claim ON claim.id = assignment.claim_right_id "
            "LEFT JOIN securities security ON security.id = claim.related_security_id "
            "LEFT JOIN legal_entities from_entity ON from_entity.id = assignment.from_entity_id "
            "LEFT JOIN legal_entities to_entity ON to_entity.id = assignment.to_entity_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY assignment.effective_date, assignment.created_at, assignment.id",
            tuple(values),
        ).fetchall()

    def _holding_result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Mapping[str, object]:
        return {
            "id": row["id"], "investor": row["investor"], "subfund": row["subfund"],
            "account": row["external_reference"], "isin": row["isin"], "quantity": row["quantity"],
            "positionDate": row["position_date"], "source": self._record_provenance(connection, "holding_position", str(row["id"]), row["candidate_id"]),
        }

    def _transfer_result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Mapping[str, object]:
        return {
            "id": row["id"], "effectiveDate": row["effective_date"], "isin": row["isin"], "quantity": row["quantity"],
            "from": {"investor": row["from_investor"], "subfund": row["from_subfund"], "account": row["from_account"]},
            "to": {"investor": row["to_investor"], "subfund": row["to_subfund"], "account": row["to_account"]},
            "source": self._record_provenance(connection, "securities_transfer", str(row["id"]), row["candidate_id"]),
        }

    def _assignment_result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Mapping[str, object]:
        return {
            "id": row["id"], "claimReference": row["claim_reference"], "isin": row["isin"],
            "fromEntity": row["from_entity"], "toEntity": row["to_entity"],
            "percentage": row["assignment_percentage"], "effectiveDate": row["effective_date"],
            "source": self._record_provenance(connection, "claim_right_assignment", str(row["id"]), row["candidate_id"]),
            "limitation": "This legal claim-right event does not evidence a securities movement.",
        }

    def _record_provenance(
        self, connection: sqlite3.Connection, target_type: str, target_id: str, candidate_id: object
    ) -> Mapping[str, object]:
        candidate = None
        if isinstance(candidate_id, str) and candidate_id:
            candidate = connection.execute(
                "SELECT c.*, d.imported_at FROM reconciliation_candidates c JOIN source_documents d ON d.id = c.source_document_id WHERE c.id = ?",
                (candidate_id,),
            ).fetchone()
        evidence = connection.execute(
            "SELECT id, source_document_id, source_location, description, created_at FROM evidence_references WHERE target_type = ? AND target_id = ? ORDER BY created_at, id",
            (target_type, target_id),
        ).fetchall()
        return {
            "candidateId": candidate_id if isinstance(candidate_id, str) else None,
            "sourceDocumentId": candidate["source_document_id"] if candidate is not None else None,
            "extractionId": candidate["extraction_id"] if candidate is not None else None,
            "parserVersion": candidate["parser_version"] if candidate is not None else None,
            "sourceReference": candidate["source_reference"] if candidate is not None else None,
            "documentIssueDate": candidate["document_issue_date"] if candidate is not None else None,
            "documentImportedAt": candidate["imported_at"] if candidate is not None else None,
            "candidateCreatedAt": candidate["created_at"] if candidate is not None else None,
            "confirmedAt": candidate["confirmed_at"] if candidate is not None else None,
            "evidenceReferences": [
                {"id": item["id"], "sourceDocumentId": item["source_document_id"], "sourceReference": item["source_location"], "description": item["description"], "createdAt": item["created_at"]}
                for item in evidence
            ],
        }

    def _open_discrepancy_results(self, connection: sqlite3.Connection, filters: Mapping[str, str]) -> list[Mapping[str, object]]:
        issues = connection.execute(
            "SELECT issue.* FROM issues issue WHERE issue.status = 'open' AND issue.reason = 'conflicting dated holding' ORDER BY issue.created_at, issue.id"
        ).fetchall()
        results: list[Mapping[str, object]] = []
        represented_conflicts: set[str] = set()
        for issue in issues:
            conflict_id = self._issue_conflict_id(connection, issue)
            if conflict_id is not None:
                if conflict_id in represented_conflicts:
                    continue
                result = self._provisional_conflict_result(connection, issue, conflict_id, filters)
                if result is not None:
                    results.append(result)
                    represented_conflicts.add(conflict_id)
                continue
            candidate = connection.execute(
                "SELECT c.*, d.imported_at FROM reconciliation_candidates c JOIN source_documents d ON d.id = c.source_document_id WHERE c.id = ? AND c.candidate_type = 'holding_snapshot'",
                (issue["affected_id"],),
            ).fetchone()
            if candidate is None:
                continue
            payload = json.loads(str(candidate["payload_json"]))
            if any(payload.get(key) != value for key, value in (("account", filters.get("account")), ("subfund", filters.get("subfund")), ("isin", filters.get("isin"))) if value is not None):
                continue
            if filters.get("investor") and payload.get("investor") not in {None, filters["investor"]}:
                continue
            date_value = str(payload["positionDate"])
            if filters.get("fromDate") and date_value < filters["fromDate"]:
                continue
            if filters.get("toDate") and date_value > filters["toDate"]:
                continue
            matching = connection.execute(
                "SELECT p.*, a.external_reference, s.isin, fund.name AS subfund, investor_entity.display_name AS investor "
                "FROM holding_positions p JOIN accounts a ON a.id = p.account_id JOIN investors i ON i.id = a.investor_id "
                "JOIN legal_entities investor_entity ON investor_entity.id = i.legal_entity_id "
                "LEFT JOIN investment_vehicles fund ON fund.id = a.investment_vehicle_id JOIN securities s ON s.id = p.security_id "
                "WHERE a.external_reference = ? AND s.isin = ? AND p.position_date = ? ORDER BY p.created_at, p.id",
                (payload["account"], payload["isin"], date_value),
            ).fetchall()
            competing = [self._holding_result(connection, row) for row in matching]
            candidate_quantity = Decimal(str(payload["quantity"]))
            differences = [self._decimal(abs(Decimal(str(row["quantity"])) - candidate_quantity)) for row in matching]
            audit = connection.execute(
                "SELECT id, event_type, target_type, target_id, occurred_at FROM audit_events WHERE target_id = ? OR (target_type = 'reconciliation_batch' AND details_json LIKE ?) ORDER BY occurred_at, id",
                (candidate["id"], f'%{candidate["id"]}%'),
            ).fetchall()
            results.append({
                "id": issue["id"], "status": "unresolved", "reason": issue["reason"], "severity": issue["severity"],
                "asOfDate": date_value, "account": payload["account"], "subfund": payload["subfund"], "isin": payload["isin"],
                "candidateValue": self._decimal(candidate_quantity),
                "candidate": self._candidate_result(candidate),
                "documentedCompetingValues": competing,
                "differenceAmounts": differences,
                "sourceDocumentId": issue["source_document_id"], "createdAt": issue["created_at"], "resolvedAt": issue["resolved_at"],
                "auditReferences": [
                    {"id": row["id"], "eventType": row["event_type"], "targetType": row["target_type"], "targetId": row["target_id"], "occurredAt": row["occurred_at"]}
                    for row in audit
                ],
            })
        return results

    @staticmethod
    def _issue_conflict_id(connection: sqlite3.Connection, issue: sqlite3.Row) -> str | None:
        if issue["affected_type"] == "reconciliation_holding_conflict":
            row = connection.execute(
                "SELECT id FROM reconciliation_holding_conflicts WHERE id = ?", (issue["affected_id"],)
            ).fetchone()
            return str(row["id"]) if row is not None else None
        if issue["affected_type"] == "reconciliation_candidate":
            row = connection.execute(
                "SELECT conflict_id FROM reconciliation_holding_conflict_candidates WHERE candidate_id = ?",
                (issue["affected_id"],),
            ).fetchone()
            return str(row["conflict_id"]) if row is not None else None
        return None

    def _provisional_conflict_result(
        self, connection: sqlite3.Connection, issue: sqlite3.Row, conflict_id: str, filters: Mapping[str, str]
    ) -> Mapping[str, object] | None:
        header = connection.execute(
            "SELECT * FROM reconciliation_holding_conflicts WHERE id = ? AND status = 'open'", (conflict_id,)
        ).fetchone()
        if header is None:
            return None
        rows = connection.execute(
            "SELECT link.*, candidate.*, document.imported_at FROM reconciliation_holding_conflict_candidates link "
            "JOIN reconciliation_candidates candidate ON candidate.id = link.candidate_id "
            "JOIN source_documents document ON document.id = candidate.source_document_id "
            "WHERE link.conflict_id = ? ORDER BY link.quantity, link.candidate_id",
            (conflict_id,),
        ).fetchall()
        if len(rows) < 2:
            return None
        payloads = [json.loads(str(row["payload_json"])) for row in rows]
        if filters.get("account") and header["account_reference"] != filters["account"]:
            return None
        if filters.get("isin") and header["security_isin"] != filters["isin"]:
            return None
        if filters.get("subfund") and not any(payload.get("subfund") == filters["subfund"] for payload in payloads):
            return None
        if filters.get("investor") and not any(payload.get("investor") == filters["investor"] for payload in payloads):
            return None
        date_value = str(header["position_date"])
        if filters.get("fromDate") and date_value < filters["fromDate"]:
            return None
        if filters.get("toDate") and date_value > filters["toDate"]:
            return None
        quantities = [Decimal(str(row["quantity"])) for row in rows]
        candidates = [self._candidate_result(row) for row in rows]
        observations = [
            {
                "candidateId": candidate["id"], "quantity": self._decimal(Decimal(str(row["quantity"]))),
                "investor": payload.get("investor"), "subfund": payload.get("subfund"),
                "account": payload.get("account"), "isin": payload.get("isin"), "asOfDate": payload.get("positionDate"),
                "source": {
                    "sourceDocumentId": candidate["sourceDocumentId"], "extractionId": candidate["extractionId"],
                    "parserVersion": candidate["parserVersion"], "sourceReference": candidate["sourceReference"],
                    "documentIssueDate": candidate["documentIssueDate"], "documentImportedAt": candidate["documentImportedAt"],
                    "candidateCreatedAt": candidate["createdAt"],
                },
            }
            for row, candidate, payload in zip(rows, candidates, payloads)
        ]
        candidate_ids = [str(candidate["id"]) for candidate in candidates]
        placeholders = ",".join("?" for _ in candidate_ids)
        audit = connection.execute(
            "SELECT id, event_type, target_type, target_id, occurred_at FROM audit_events "
            "WHERE target_id = ? OR target_id IN (" + placeholders + ") "
            "ORDER BY occurred_at, id",
            (conflict_id, *candidate_ids),
        ).fetchall()
        primary = candidates[0]
        return {
            "id": issue["id"], "conflictId": conflict_id, "status": "unresolved", "reason": issue["reason"],
            "severity": issue["severity"], "asOfDate": date_value, "account": header["account_reference"],
            "subfund": header["subfund_name"], "investor": header["investor_name"], "isin": header["security_isin"],
            "candidateValue": observations[0]["quantity"], "candidate": primary,
            # Kept for compatibility with the existing screen, but these are
            # competing source observations, not necessarily confirmed facts.
            "documentedCompetingValues": observations[1:], "observations": observations,
            "candidateIds": candidate_ids, "sourceDocumentIds": sorted({str(row["source_document_id"]) for row in rows}),
            "differenceAmounts": [self._decimal(max(quantities) - min(quantities))],
            "sourceDocumentId": issue["source_document_id"], "createdAt": header["created_at"],
            "updatedAt": header["updated_at"], "resolvedAt": header["resolved_at"],
            "auditReferences": [
                {"id": row["id"], "eventType": row["event_type"], "targetType": row["target_type"], "targetId": row["target_id"], "occurredAt": row["occurred_at"]}
                for row in audit
            ],
        }

    def _earliest_snapshot_date(self, connection: sqlite3.Connection, filters: Mapping[str, str]) -> str | None:
        rows = self._holding_rows(connection, filters)
        return str(rows[0]["position_date"]) if rows else None

    # Candidate generation -------------------------------------------------

    def _deterministic_candidates(self, connection: sqlite3.Connection, context: sqlite3.Row) -> list[Mapping[str, object]]:
        if context["file_format"] in {"xlsx", "xls", "csv"}:
            return self._spreadsheet_candidates(connection, context)
        if context["file_format"] == "pdf":
            return self._pdf_candidates(connection, context)
        return []

    def _spreadsheet_candidates(self, connection: sqlite3.Connection, context: sqlite3.Row) -> list[Mapping[str, object]]:
        rows = connection.execute(
            "SELECT sheet_name, row_number, column_number, value_text FROM extracted_content WHERE extraction_id = ? AND content_kind IN ('spreadsheet_cell', 'csv_cell') AND is_empty = 0 ORDER BY worksheet_index, sheet_name, row_number, column_number",
            (context["extraction_id"],),
        ).fetchall()
        by_sheet: dict[str, dict[int, dict[int, str]]] = {}
        for row in rows:
            if row["sheet_name"] is not None and row["row_number"] is not None and row["column_number"] is not None:
                by_sheet.setdefault(str(row["sheet_name"]), {}).setdefault(int(row["row_number"]), {})[int(row["column_number"])] = str(row["value_text"] or "")
        candidates: list[Mapping[str, object]] = []
        for sheet, grid in by_sheet.items():
            headers = list(self._holding_headers(grid))
            metadata = self._sheet_metadata(sheet, grid, skip_rows={row for row, _columns in headers})
            sheet_issue = metadata.get("issue_date") if isinstance(metadata.get("issue_date"), str) else ""
            sheet_investor = metadata.get("investor") if isinstance(metadata.get("investor"), str) else None
            for index, (header_row, columns) in enumerate(headers):
                next_header = headers[index + 1][0] if index + 1 < len(headers) else None
                for row_number in sorted(number for number in grid if number > header_row and (next_header is None or number < next_header)):
                    values = {name: grid[row_number].get(column, "").strip() for name, column in columns.items()}
                    if not any(values.values()):
                        continue
                    if not all(values.get(name) for name in ("account", "isin", "quantity", "position_date")):
                        continue
                    position_date, quantity = self._parse_date(values["position_date"]), self._quantity(values["quantity"])
                    isin = values["isin"].upper().replace(" ", "")
                    if position_date is None or quantity is None or len(isin) != 12:
                        continue
                    investor = values.get("investor") or sheet_investor
                    refs = [*metadata["references"], self._sheet_row_reference(sheet, row_number, min(columns.values()), max(columns.values()))]
                    candidates.append(self._candidate_input(context, "holding_snapshot", "; ".join(dict.fromkeys(refs)), f"Structured holdings row {sheet} {row_number}", str(sheet_issue), "deterministic", {
                        "investor": investor or None, "subfund": values.get("subfund") or None, "account": values["account"], "isin": isin, "quantity": quantity, "positionDate": position_date,
                    }, 1.0, "supported"))
        return candidates

    @staticmethod
    def _pdf_pages(connection: sqlite3.Connection, extraction_id: str) -> tuple[dict[int, list[tuple[int, str]]], dict[int, dict[int, tuple[float, float, float, float]]]]:
        rows = connection.execute(
            "SELECT p.page_number, b.block_index, b.text, b.x0, b.y0, b.x1, b.y1 FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
            (extraction_id,),
        ).fetchall()
        pages: dict[int, list[tuple[int, str]]] = {}
        geometry: dict[int, dict[int, tuple[float, float, float, float]]] = {}
        for row in rows:
            pages.setdefault(int(row["page_number"]), []).append((int(row["block_index"]), str(row["text"] or "")))
            if None not in (row["x0"], row["y0"], row["x1"], row["y1"]):
                geometry.setdefault(int(row["page_number"]), {})[int(row["block_index"])] = (float(row["x0"]), float(row["y0"]), float(row["x1"]), float(row["y1"]))
        return pages, geometry

    @staticmethod
    def _pdf_document_context(pages: Mapping[int, Sequence[tuple[int, str]]]) -> tuple[tuple[str, int, int] | None, str | None]:
        """The document's first labelled issue date (with location) and its single labelled investor."""
        document_issue: tuple[str, int, int] | None = None
        investors: set[str] = set()
        for page_number, blocks in pages.items():
            issue_value, issue_block = labelled_value(blocks, "issue_date")
            if document_issue is None and isinstance(issue_value, str) and issue_block is not None:
                document_issue = (issue_value, page_number, issue_block)
            investor_value, _block = labelled_value(blocks, "investor")
            if isinstance(investor_value, str):
                investors.add(investor_value)
        return document_issue, (next(iter(investors)) if len(investors) == 1 else None)

    def _pdf_candidates(self, connection: sqlite3.Connection, context: sqlite3.Row) -> list[Mapping[str, object]]:
        """Read holdings, transfers, and assignments from varied PDF wording.

        A page without its own labelled issue date uses the document's first
        one; with none at all the issue date is recorded as not stated.
        References name only the blocks that support each record.
        """
        pages, geometry = self._pdf_pages(connection, str(context["extraction_id"]))
        document_issue, document_investor = self._pdf_document_context(pages)
        candidates: list[Mapping[str, object]] = []
        for page_number, blocks in pages.items():
            records, page_issue, page_issue_block = extract_page_records(blocks, document_investor, geometry.get(page_number))
            issue = (page_issue, page_number, page_issue_block) if page_issue is not None and page_issue_block is not None else document_issue
            for record in records:
                references = [f"page {page_number} block {index}" for index in record.block_indexes]
                if issue is not None and f"page {issue[1]} block {issue[2]}" not in references:
                    references.append(f"page {issue[1]} block {issue[2]}")
                candidates.append(self._candidate_input(context, record.kind, "; ".join(references), record.snippet, issue[0] if issue else "", "deterministic", record.payload, 1.0, "supported"))
        return candidates

    def _stage_alias_statements(self, context: sqlite3.Row) -> int:
        """Keep a PDF's explicit alias statements as source-linked evidence.

        Every alias, canonical name and account must appear in the stored
        source text.  Statements are evidence for later matching only.
        """
        if context["file_format"] != "pdf":
            return 0
        now, staged = datetime.now(UTC).isoformat(), 0
        with self._database.transaction() as connection:
            extraction_id = str(context["extraction_id"])
            pages, geometry = self._pdf_pages(connection, extraction_id)
            document_issue, _investor = self._pdf_document_context(pages)
            text = self._normalise_text(self._document_text(connection, extraction_id))
            for page_number, blocks in pages.items():
                for statement in extract_page_aliases(blocks, geometry.get(page_number)):
                    values = [*statement.aliases, statement.canonical, *([statement.account] if statement.account else [])]
                    if not all(self._normalise_text(value) in text for value in values):
                        continue
                    reference = "; ".join(f"page {page_number} block {index}" for index in statement.block_indexes)
                    normalized_account = normalize_account_reference(statement.account) if statement.account else None
                    for alias in statement.aliases:
                        exists = connection.execute(
                            "SELECT 1 FROM identity_alias_evidence WHERE source_document_id = ? AND target_type = ? AND normalized_alias = ? AND normalized_canonical = ? AND COALESCE(normalized_account, '') = ?",
                            (context["source_document_id"], statement.target_type, normalize_alias(statement.target_type, alias), normalize_alias(statement.target_type, statement.canonical), normalized_account or ""),
                        ).fetchone()
                        if exists is not None:
                            continue
                        evidence_id = str(uuid.uuid4())
                        connection.execute(
                            "INSERT INTO identity_alias_evidence (id, target_type, alias_text, normalized_alias, canonical_text, normalized_canonical, account_reference, normalized_account, source_document_id, extraction_id, source_reference, evidence_snippet, document_issue_date, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (evidence_id, statement.target_type, alias, normalize_alias(statement.target_type, alias), statement.canonical, normalize_alias(statement.target_type, statement.canonical),
                             statement.account, normalized_account, context["source_document_id"], extraction_id, reference, statement.snippet, document_issue[0] if document_issue else None, now),
                        )
                        self._audit(connection, "identity_alias_statement_staged", statement.target_type, evidence_id, "system", now, {"source_document_id": context["source_document_id"]})
                        staged += 1
                for migration in extract_page_migrations(blocks, geometry.get(page_number), _investor):
                    values = [migration.old_account, migration.new_account, *(value for value in (migration.investor, migration.fund) if value)]
                    if not all(self._normalise_text(value) in text for value in values):
                        continue
                    old_key, new_key = normalize_account_reference(migration.old_account), normalize_account_reference(migration.new_account)
                    if connection.execute(
                        "SELECT 1 FROM account_migration_evidence WHERE source_document_id = ? AND normalized_old_account = ? AND normalized_new_account = ? AND COALESCE(effective_date, '') = ?",
                        (context["source_document_id"], old_key, new_key, migration.effective_date or ""),
                    ).fetchone() is not None:
                        continue
                    evidence_id = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO account_migration_evidence (id, old_account_reference, normalized_old_account, new_account_reference, normalized_new_account, effective_date, investor_text, fund_text, source_document_id, extraction_id, source_reference, evidence_snippet, document_issue_date, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (evidence_id, migration.old_account, old_key, migration.new_account, new_key, migration.effective_date, migration.investor, migration.fund,
                         context["source_document_id"], extraction_id, "; ".join(f"page {page_number} block {index}" for index in migration.block_indexes),
                         migration.snippet, document_issue[0] if document_issue else None, now),
                    )
                    self._audit(connection, "account_migration_statement_staged", "account", evidence_id, "system", now, {"source_document_id": context["source_document_id"]})
                    staged += 1
            if staged:
                # Candidates waiting only on a "same as?" answer may now be documented.
                self._refresh_review_status(connection, now)
        return staged

    def _ai_candidates(self, connection: sqlite3.Connection, context: sqlite3.Row) -> list[Mapping[str, object]]:
        """Assemble AI candidates only from complete, independently validated fields."""
        issue_date = self._document_issue_date(connection, context)
        if issue_date is None:
            return []
        rows = connection.execute(
            "SELECT proposal_type, field_name, value_json, confidence, source_reference FROM ai_proposals p JOIN ai_processing_jobs j ON j.id = p.job_id WHERE j.source_document_id = ? AND j.extraction_id = ? AND j.status = 'completed' AND p.validation_status = 'validated' AND p.is_unknown = 0 ORDER BY p.created_at, p.id",
            (context["source_document_id"], context["extraction_id"]),
        ).fetchall()
        grouped: dict[str, dict[str, list[sqlite3.Row]]] = {}
        for row in rows:
            grouped.setdefault(str(row["proposal_type"]), {}).setdefault(self._field_key(str(row["field_name"])), []).append(row)

        def one(group: str, *names: str) -> sqlite3.Row | None:
            for name in names:
                matches = grouped.get(group, {}).get(self._field_key(name), [])
                if len(matches) == 1:
                    return matches[0]
            return None

        def value(row: sqlite3.Row | None) -> str | None:
            if row is None:
                return None
            try:
                parsed = json.loads(str(row["value_json"]))
            except json.JSONDecodeError:
                return None
            return str(parsed).strip() if isinstance(parsed, (str, int, float)) and str(parsed).strip() else None

        candidates: list[Mapping[str, object]] = []
        holding_rows = [one("investor_legal_entity", "name", "investor"), one("investment_vehicle_subfund", "name", "subfund"), one("account", "account_identifier", "account"), one("security", "isin"), one("holding_position", "quantity", "nominal_amount"), one("holding_position", "as_of_date", "position_date")]
        holding_values = [value(row) for row in holding_rows]
        if all(holding_values) and self._quantity(str(holding_values[4])) and self._parse_date(str(holding_values[5])):
            candidates.append(self._ai_candidate_input(context, "holding_snapshot", holding_rows, issue_date, {
                "investor": holding_values[0], "subfund": holding_values[1], "account": holding_values[2], "isin": str(holding_values[3]).upper(), "quantity": self._quantity(str(holding_values[4])), "positionDate": self._parse_date(str(holding_values[5])),
            }))
        for proposal_type, candidate_type, fields in (
            ("securities_transfer", "securities_transfer", ("investor", "from_subfund", "from_account", "to_subfund", "to_account", "isin", "quantity", "effective_date")),
            ("claim_right_assignment", "claim_assignment", ("claim_reference", "from_entity", "to_entity", "isin", "effective_date")),
        ):
            event_rows, values = [one(proposal_type, field) for field in fields], []
            values = [value(row) for row in event_rows]
            if not all(values):
                continue
            if candidate_type == "securities_transfer":
                quantity, effective = self._quantity(str(values[6])), self._parse_date(str(values[7]))
                if quantity is None or effective is None:
                    continue
                payload = {"investor": values[0], "fromSubfund": values[1], "fromAccount": values[2], "toSubfund": values[3], "toAccount": values[4], "isin": str(values[5]).upper(), "quantity": quantity, "effectiveDate": effective}
            else:
                effective = self._parse_date(str(values[4]))
                if effective is None:
                    continue
                payload = {"claimReference": values[0], "fromEntity": values[1], "toEntity": values[2], "isin": str(values[3]).upper(), "effectiveDate": effective}
            candidates.append(self._ai_candidate_input(context, candidate_type, event_rows, issue_date, payload))
        return candidates

    # Confirmation ---------------------------------------------------------

    def _confirm_row(self, connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> Mapping[str, object]:
        payload, kind = json.loads(str(row["payload_json"])), str(row["candidate_type"])
        result = {"holding_snapshot": self._holding, "securities_transfer": self._transfer, "claim_assignment": self._assignment}[kind](connection, row, payload, now)
        connection.execute("UPDATE reconciliation_candidates SET lifecycle_status = 'confirmed', confirmed_at = ? WHERE id = ?", (now, row["id"]))
        return result

    def _holding(self, connection: sqlite3.Connection, row: sqlite3.Row, payload: Mapping[str, object], now: str) -> Mapping[str, object]:
        account = self._account(connection, row, payload.get("investor"), payload.get("subfund"), str(payload["account"]), now)
        security, quantity = self._security(connection, str(payload["isin"]), now), self._decimal(Decimal(str(payload["quantity"])))
        existing = connection.execute("SELECT id, quantity FROM holding_positions WHERE account_id = ? AND security_id = ? AND position_date = ? ORDER BY created_at", (account, security, payload["positionDate"])).fetchall()
        duplicate = next((item for item in existing if str(item["quantity"]) == quantity), None)
        if duplicate is not None:
            self._evidence(connection, row, "holding_position", str(duplicate["id"]), now)
            return {"id": duplicate["id"], "type": "holding_snapshot", "status": "confirmed", "duplicateEvidence": True}
        if existing:
            raise ReconciliationError("CONFLICT_REQUIRES_REVIEW", "A conflicting dated holding must remain provisional until reviewed.")
        position_id = str(uuid.uuid4())
        connection.execute("INSERT INTO holding_positions (id, account_id, security_id, position_date, quantity, source_document_id, created_at, candidate_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (position_id, account, security, payload["positionDate"], quantity, row["source_document_id"], now, row["id"]))
        self._evidence(connection, row, "holding_position", position_id, now)
        return {"id": position_id, "type": "holding_snapshot", "status": "confirmed"}

    def _transfer(self, connection: sqlite3.Connection, row: sqlite3.Row, payload: Mapping[str, object], now: str) -> Mapping[str, object]:
        source = self._account(connection, row, payload.get("investor"), payload.get("fromSubfund"), str(payload["fromAccount"]), now)
        destination = self._account(connection, row, payload.get("investor"), payload.get("toSubfund"), str(payload["toAccount"]), now)
        security, quantity = self._security(connection, str(payload["isin"]), now), Decimal(str(payload["quantity"]))
        lineage = self._account_lineage(connection, source)
        prior = self._lineage_snapshot(connection, lineage, security, str(payload["effectiveDate"]))
        if prior is None:
            raise ReconciliationError("IMPOSSIBLE_TRANSFER", "Transfer has no confirmed source balance.")
        available = Decimal(str(prior["quantity"]))
        lineage_ids = {account for account, _replaced in lineage}
        for transfer in connection.execute("SELECT from_account_id, to_account_id, quantity FROM securities_transfers WHERE security_id = ? AND effective_date > ? AND effective_date <= ? ORDER BY effective_date, id", (security, prior["position_date"], payload["effectiveDate"])):
            if transfer["from_account_id"] in lineage_ids:
                available -= Decimal(str(transfer["quantity"]))
            if transfer["to_account_id"] in lineage_ids:
                available += Decimal(str(transfer["quantity"]))
        if available < quantity:
            raise ReconciliationError("IMPOSSIBLE_TRANSFER", "Transfer exceeds the confirmed source balance after earlier transfers.")
        duplicate = connection.execute("SELECT id FROM securities_transfers WHERE from_account_id = ? AND to_account_id = ? AND security_id = ? AND effective_date = ? AND quantity = ?", (source, destination, security, payload["effectiveDate"], self._decimal(quantity))).fetchone()
        transfer_id = str(duplicate["id"]) if duplicate else str(uuid.uuid4())
        if duplicate is None:
            connection.execute("INSERT INTO securities_transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (transfer_id, row["id"], source, destination, security, payload["effectiveDate"], self._decimal(quantity), row["source_document_id"], now))
        self._evidence(connection, row, "securities_transfer", transfer_id, now)
        return {"id": transfer_id, "type": "securities_transfer", "status": "confirmed", "duplicateEvidence": duplicate is not None}

    def _assignment(self, connection: sqlite3.Connection, row: sqlite3.Row, payload: Mapping[str, object], now: str) -> Mapping[str, object]:
        security = self._security(connection, str(payload["isin"]), now)
        from_entity, to_entity = self._entity(connection, row, str(payload["fromEntity"]), now), self._entity(connection, row, str(payload["toEntity"]), now)
        claim = connection.execute("SELECT id FROM claim_rights WHERE claim_reference = ?", (payload["claimReference"],)).fetchone()
        claim_id = str(claim["id"]) if claim else str(uuid.uuid4())
        if claim is None:
            connection.execute("INSERT INTO claim_rights VALUES (?, ?, ?, ?, 'assigned', NULL, ?)", (claim_id, payload["claimReference"], from_entity, security, now))
        percentage = payload.get("percentage")
        duplicate = connection.execute("SELECT id FROM claim_right_assignments WHERE claim_right_id = ? AND from_entity_id = ? AND to_entity_id = ? AND effective_date = ? AND COALESCE(assignment_percentage, '') = ?", (claim_id, from_entity, to_entity, payload["effectiveDate"], "" if percentage is None else str(percentage))).fetchone()
        assignment_id = str(duplicate["id"]) if duplicate else str(uuid.uuid4())
        if duplicate is None:
            connection.execute("INSERT INTO claim_right_assignments (id, claim_right_id, from_entity_id, to_entity_id, effective_date, source_document_id, created_at, candidate_id, assignment_percentage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (assignment_id, claim_id, from_entity, to_entity, payload["effectiveDate"], row["source_document_id"], now, row["id"], percentage))
        self._evidence(connection, row, "claim_right_assignment", assignment_id, now)
        return {"id": assignment_id, "type": "claim_assignment", "status": "confirmed", "duplicateEvidence": duplicate is not None}

    # Policy and issue handling -------------------------------------------

    def _policy_review_reason(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        if row["lifecycle_status"] == "confirmed":
            return None
        if self._superseded(connection, str(row["id"])):
            return "candidate_superseded"
        if self._candidate_has_open_holding_conflict(connection, str(row["id"])):
            return "conflicting_dated_holding"
        if self._sync_identity_questions(connection, str(row["id"]), datetime.now(UTC).isoformat()):
            return "identity_link_requires_review"
        if self._identity_review(connection, str(row["candidate_type"]), json.loads(str(row["payload_json"])))[1]:
            return "account_identity_conflict"
        if self._account_details_incomplete(connection, str(row["candidate_type"]), json.loads(str(row["payload_json"]))):
            return "account_details_incomplete"
        if row["origin"] != "deterministic":
            return "ai_origin_requires_individual_review"
        if row["candidate_type"] == "claim_assignment":
            return "legal_assignment_requires_individual_review"
        payload = json.loads(str(row["payload_json"]))
        if self._holding_conflict(connection, row, payload):
            return "conflicting_dated_holding"
        if row["candidate_type"] == "securities_transfer" and not self._transfer_has_source_balance(connection, payload):
            return "transfer_source_balance_unavailable"
        return None

    def _holding_conflict(self, connection: sqlite3.Connection, row: sqlite3.Row, payload: Mapping[str, object]) -> bool:
        if row["candidate_type"] != "holding_snapshot":
            return False
        account = self._resolved_account_id(connection, payload.get("account"))
        security = connection.execute("SELECT id FROM securities WHERE isin = ?", (payload.get("isin"),)).fetchone()
        if account is None or security is None:
            return False
        quantities = connection.execute("SELECT quantity FROM holding_positions WHERE account_id = ? AND security_id = ? AND position_date = ?", (account, security["id"], payload.get("positionDate"))).fetchall()
        return any(str(item["quantity"]) != self._decimal(Decimal(str(payload["quantity"]))) for item in quantities)

    def _sync_provisional_holding_conflicts(
        self, connection: sqlite3.Connection, candidate_id: str, now: str
    ) -> str | None:
        """Persist a source-observation conflict without confirming a holding.

        Candidate rows remain the source of truth for each observation.  The
        additive conflict/link tables only group incompatible observations and
        retain the exact provenance present when the contradiction was found.
        """
        candidate = self._candidate_row(connection, candidate_id)
        if candidate["candidate_type"] != "holding_snapshot":
            return None
        payload = json.loads(str(candidate["payload_json"]))
        identity = self._holding_conflict_identity(payload)
        if identity is None:
            return None
        account_key, isin, position_date = identity
        account = str(payload["account"]).strip()
        observations: list[tuple[sqlite3.Row, Mapping[str, object], Decimal]] = []
        rows = connection.execute(
            "SELECT * FROM reconciliation_candidates WHERE candidate_type = 'holding_snapshot' ORDER BY created_at, id"
        ).fetchall()
        for row in rows:
            value = json.loads(str(row["payload_json"]))
            if self._holding_conflict_identity(value) != identity:
                continue
            try:
                quantity = Decimal(str(value["quantity"]))
            except (InvalidOperation, KeyError):
                continue
            observations.append((row, value, quantity))
        if len({quantity for _, _, quantity in observations}) < 2:
            return None

        # Reuse a conflict an observation already belongs to, so spelling
        # variants of one account reference never split a contradiction.
        observation_ids = [str(row["id"]) for row, _payload, _quantity in observations]
        conflict = connection.execute(
            "SELECT conflict.* FROM reconciliation_holding_conflicts conflict "
            "JOIN reconciliation_holding_conflict_candidates link ON link.conflict_id = conflict.id "
            f"WHERE link.candidate_id IN ({','.join('?' for _ in observation_ids)}) ORDER BY conflict.created_at, conflict.id LIMIT 1",
            observation_ids,
        ).fetchone()
        key_material = json.dumps({"account": account_key, "isin": isin, "positionDate": position_date}, sort_keys=True, separators=(",", ":"))
        conflict_key = str(conflict["conflict_key"]) if conflict is not None else hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        if conflict is None:
            conflict = connection.execute(
                "SELECT * FROM reconciliation_holding_conflicts WHERE conflict_key = ?", (conflict_key,)
            ).fetchone()
        created = conflict is None
        conflict_id = str(conflict["id"]) if conflict is not None else str(uuid.uuid4())
        investor = self._shared_observation_value(observations, "investor")
        subfund = self._shared_observation_value(observations, "subfund")
        if conflict is None:
            connection.execute(
                "INSERT INTO reconciliation_holding_conflicts "
                "(id, conflict_key, status, account_reference, security_isin, position_date, investor_name, subfund_name, created_at, updated_at, resolved_at) "
                "VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, NULL)",
                (conflict_id, conflict_key, account, isin, position_date, investor, subfund, now, now),
            )

        changed = created
        for row, _payload, quantity in observations:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO reconciliation_holding_conflict_candidates "
                "(conflict_id, candidate_id, quantity, source_document_id, extraction_id, parser_version, source_reference, document_issue_date, linked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conflict_id, row["id"], self._decimal(quantity), row["source_document_id"], row["extraction_id"], row["parser_version"], row["source_reference"], row["document_issue_date"], now),
            )
            changed = changed or cursor.rowcount == 1

        # A rejected observation remains linked, but only active observations
        # are marked as requiring review.  Nothing here confirms a record.
        active_ids = [str(row["id"]) for row, _payload, _quantity in observations if row["lifecycle_status"] != "rejected"]
        if active_ids:
            connection.execute(
                "UPDATE reconciliation_candidates SET validation_status = 'review_required' "
                f"WHERE id IN ({','.join('?' for _ in active_ids)})",
                active_ids,
            )
        if changed:
            connection.execute(
                "UPDATE reconciliation_holding_conflicts SET updated_at = ?, investor_name = ?, subfund_name = ? WHERE id = ?",
                (now, investor, subfund, conflict_id),
            )
            representative = min(observations, key=lambda item: (str(item[0]["source_document_id"]), str(item[0]["source_reference"]), str(item[0]["id"])))[0]
            self._ensure_holding_conflict_issue(connection, conflict_id, representative, now)
            self._audit(connection, "reconciliation_holding_conflict_detected", "reconciliation_holding_conflict", conflict_id, "system", now, {
                "candidate_ids": sorted(str(row["id"]) for row, _payload, _quantity in observations),
                "source_document_ids": sorted({str(row["source_document_id"]) for row, _payload, _quantity in observations}),
            })
        return conflict_id

    @staticmethod
    def _holding_conflict_identity(payload: Mapping[str, object]) -> tuple[str, str, str] | None:
        account, isin, position_date = payload.get("account"), payload.get("isin"), payload.get("positionDate")
        if not all(isinstance(value, str) and value.strip() for value in (account, isin, position_date)):
            return None
        return (normalize_account_reference(str(account)), str(isin).upper().replace(" ", ""), str(position_date))

    @staticmethod
    def _shared_observation_value(
        observations: Sequence[tuple[sqlite3.Row, Mapping[str, object], Decimal]], key: str
    ) -> str | None:
        values = [payload.get(key) for _row, payload, _quantity in observations]
        if not values or not all(isinstance(value, str) and value.strip() for value in values):
            return None
        normalized = {str(value).strip() for value in values}
        return next(iter(normalized)) if len(normalized) == 1 else None

    @staticmethod
    def _candidate_has_open_holding_conflict(connection: sqlite3.Connection, candidate_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM reconciliation_holding_conflict_candidates link "
            "JOIN reconciliation_holding_conflicts conflict ON conflict.id = link.conflict_id "
            "WHERE link.candidate_id = ? AND conflict.status = 'open' LIMIT 1",
            (candidate_id,),
        ).fetchone() is not None

    def _ensure_holding_conflict_issue(
        self, connection: sqlite3.Connection, conflict_id: str, representative: sqlite3.Row, now: str
    ) -> None:
        # Databases created before this migration may already have one legacy
        # candidate issue. Reuse it through the link table rather than adding
        # a second open issue for the same underlying contradiction.
        legacy = connection.execute(
            "SELECT 1 FROM issues issue JOIN reconciliation_holding_conflict_candidates link "
            "ON link.candidate_id = issue.affected_id WHERE issue.status = 'open' "
            "AND issue.reason = 'conflicting dated holding' AND issue.affected_type = 'reconciliation_candidate' "
            "AND link.conflict_id = ? LIMIT 1",
            (conflict_id,),
        ).fetchone()
        if legacy is None:
            self._issue(connection, representative, "reconciliation_holding_conflict", conflict_id, "conflicting dated holding", now)

    def _persisted_candidate_conflicts(self, connection: sqlite3.Connection) -> dict[str, Mapping[str, object]]:
        rows = connection.execute(
            "SELECT conflict.*, link.candidate_id, link.quantity, link.source_document_id, link.source_reference, "
            "link.document_issue_date, candidate.payload_json FROM reconciliation_holding_conflicts conflict "
            "JOIN reconciliation_holding_conflict_candidates link ON link.conflict_id = conflict.id "
            "JOIN reconciliation_candidates candidate ON candidate.id = link.candidate_id "
            "WHERE conflict.status = 'open' ORDER BY conflict.created_at, conflict.id, link.candidate_id"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["id"]), []).append(row)
        result: dict[str, Mapping[str, object]] = {}
        for conflict_rows in grouped.values():
            header = conflict_rows[0]
            quantities = [Decimal(str(row["quantity"])) for row in conflict_rows]
            detail = {
                "type": "competing_dated_holding", "account": header["account_reference"], "isin": header["security_isin"],
                "asOfDate": header["position_date"], "difference": self._decimal(max(quantities) - min(quantities)),
                "observations": [
                    {
                        "candidateId": row["candidate_id"], "quantity": self._decimal(Decimal(str(row["quantity"]))),
                        "investor": json.loads(str(row["payload_json"])).get("investor"),
                        "subfund": json.loads(str(row["payload_json"])).get("subfund"),
                        "sourceDocumentId": row["source_document_id"], "sourceReference": row["source_reference"],
                        "documentIssueDate": row["document_issue_date"],
                    }
                    for row in conflict_rows
                ],
            }
            for row in conflict_rows:
                result[str(row["candidate_id"])] = detail
        return result

    def _transfer_has_source_balance(self, connection: sqlite3.Connection, payload: Mapping[str, object]) -> bool:
        account = self._resolved_account_id(connection, payload.get("fromAccount"))
        security = connection.execute("SELECT id FROM securities WHERE isin = ?", (payload.get("isin"),)).fetchone()
        if security is None:
            return False
        if account is not None:
            lineage = self._account_lineage(connection, account)
        else:
            migration = self._documented_migration(connection, IdentityResolver(connection), payload.get("fromAccount"))
            if migration is None:
                return False
            lineage = [(str(migration["old_account_id"]), migration["effective_date"])] + self._account_lineage(connection, str(migration["old_account_id"]))[1:]
        return self._lineage_snapshot(connection, lineage, str(security["id"]), str(payload.get("effectiveDate"))) is not None

    @staticmethod
    def _lineage_snapshot(connection: sqlite3.Connection, lineage: Sequence[tuple[str, str | None]], security_id: str, as_of: str) -> sqlite3.Row | None:
        """The latest confirmed holding across an account and its predecessors.

        A predecessor's holding counts only up to the date it was replaced.
        """
        best: sqlite3.Row | None = None
        for account_id, replaced_on in lineage:
            limit = min(as_of, replaced_on) if replaced_on else as_of
            row = connection.execute(
                "SELECT quantity, position_date FROM holding_positions WHERE account_id = ? AND security_id = ? AND position_date <= ? ORDER BY position_date DESC, created_at DESC LIMIT 1",
                (account_id, security_id, limit),
            ).fetchone()
            if row is not None and (best is None or str(row["position_date"]) > str(best["position_date"])):
                best = row
        return best

    def _mark_review_required(self, connection: sqlite3.Connection, row: sqlite3.Row, reason: str, now: str) -> None:
        connection.execute("UPDATE reconciliation_candidates SET validation_status = 'review_required' WHERE id = ?", (row["id"],))
        # A legal assignment or AI-origin observation is review-required by
        # policy, not an inconsistency. Persist an issue only when the source
        # set itself is contradictory or operationally impossible.
        if reason == "conflicting_dated_holding" and self._candidate_has_open_holding_conflict(connection, str(row["id"])):
            return
        if reason in {"conflicting_dated_holding", "transfer_source_balance_unavailable", "account_identity_conflict", "account_details_incomplete"}:
            self._issue(connection, row, "reconciliation_candidate", str(row["id"]), reason.replace("_", " "), now)

    def _stored_review_reason(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        if row["lifecycle_status"] == "rejected":
            audit = connection.execute(
                "SELECT details_json FROM audit_events WHERE event_type = 'reconciliation_candidate_rejected' AND target_id = ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if audit is not None:
                try:
                    return str(json.loads(str(audit["details_json"])).get("reason") or "Rejected during review.")
                except json.JSONDecodeError:
                    pass
            return "Rejected during review."
        if row["validation_status"] != "review_required":
            return None
        if row["candidate_type"] == "claim_assignment":
            return "Legal claim-right assignments require individual confirmation."
        if self._candidate_has_open_holding_conflict(connection, str(row["id"])):
            return "Conflicting dated holding evidence requires review."
        if self._identity_review(connection, str(row["candidate_type"]), json.loads(str(row["payload_json"])))[0]:
            return "A name or account may refer to an existing record. Answer the identity question in Issues."
        issue = connection.execute(
            "SELECT reason FROM issues WHERE status = 'open' AND affected_type = 'reconciliation_candidate' AND affected_id = ? ORDER BY created_at, id LIMIT 1",
            (row["id"],),
        ).fetchone()
        return str(issue["reason"]) if issue is not None else "This candidate requires individual review."

    @staticmethod
    def _policy_order(row: sqlite3.Row) -> tuple[int, str, str, str]:
        payload = json.loads(str(row["payload_json"]))
        return ({"holding_snapshot": 0, "securities_transfer": 1, "claim_assignment": 2}[str(row["candidate_type"])], str(payload.get("positionDate") or payload.get("effectiveDate") or "9999-12-31"), str(row["document_issue_date"]), str(row["id"]))

    # Identity, provenance, and storage helpers --------------------------

    def _account(self, connection: sqlite3.Connection, row: sqlite3.Row, investor: object, subfund: object, reference: str, now: str) -> str:
        """Resolve or create the one account a reference identifies.

        A known account keeps its established investor and subfund; a
        different spelling of either is accepted only once it resolves to the
        same record (directly or through a user-confirmed alias).
        """
        identities = IdentityResolver(connection)
        resolution = identities.resolve("account", reference)
        if resolution.status == "ambiguous":
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "Account identity is ambiguous.")
        if resolution.status == "matched":
            existing = connection.execute(
                "SELECT a.id, a.investment_vehicle_id, i.legal_entity_id FROM accounts a JOIN investors i ON i.id = a.investor_id WHERE a.id = ?",
                (resolution.target_id,),
            ).fetchone()
            for target_type, text, expected in (("investment_vehicle", subfund, existing["investment_vehicle_id"]), ("legal_entity", investor, existing["legal_entity_id"])):
                if not isinstance(text, str) or not text.strip():
                    continue
                if self._established_name(connection, identities, target_type, text, expected, reference, existing["legal_entity_id"]) != "same":
                    raise ReconciliationError("AMBIGUOUS_IDENTITY", "The account identifier conflicts with its established investor or subfund.")
                self._record_alias(identities, row, target_type, str(expected), text, now)
            self._record_alias(identities, row, "account", str(existing["id"]), reference, now)
            return str(existing["id"])
        migration = self._documented_migration(connection, identities, reference)
        if migration is not None:
            if isinstance(investor, str) and investor.strip() and not identities.is_alias_of("legal_entity", migration["legal_entity_id"], investor):
                raise ReconciliationError("AMBIGUOUS_IDENTITY", "The documented account migration names a different investor.")
            if isinstance(subfund, str) and subfund.strip() and self._established_name(
                connection, identities, "investment_vehicle", subfund, migration["vehicle_id"], migration["old_reference"], migration["legal_entity_id"],
            ) != "same":
                raise ReconciliationError("AMBIGUOUS_IDENTITY", "The documented account migration names a different fund.")
            account_id = str(uuid.uuid4())
            connection.execute("INSERT INTO accounts VALUES (?, ?, NULL, ?, ?, ?)", (account_id, migration["investor_id"], migration["vehicle_id"], reference, now))
            connection.execute(
                "INSERT INTO account_relationships (id, relationship_type, from_account_id, to_account_id, effective_date, evidence_id, created_at) VALUES (?, 'account_migration', ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), migration["old_account_id"], account_id, migration["effective_date"], migration["evidence_id"], now),
            )
            self._audit(connection, "account_migration_applied", "account", account_id, "system", now, {
                "from_account_id": migration["old_account_id"], "evidence_id": migration["evidence_id"], "candidate_id": row["id"],
            })
            self._record_alias(identities, row, "account", account_id, reference, now)
            if isinstance(subfund, str) and subfund.strip():
                self._record_alias(identities, row, "investment_vehicle", migration["vehicle_id"], subfund, now)
            return account_id
        if not isinstance(investor, str) or not investor.strip():
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "The investor is not established for this account.")
        if not isinstance(subfund, str) or not subfund.strip():
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "The subfund is not established for this account.")
        entity_id = self._entity(connection, row, investor, now)
        investor_row = connection.execute("SELECT id FROM investors WHERE legal_entity_id = ?", (entity_id,)).fetchone()
        investor_id = str(investor_row["id"]) if investor_row else str(uuid.uuid4())
        if investor_row is None:
            connection.execute("INSERT INTO investors VALUES (?, ?, NULL, ?)", (investor_id, entity_id, now))
        existing = self._existing_fund_for_new_account(connection, identities, subfund, entity_id, reference)
        if existing is not None and existing[1] == "ask":
            raise ReconciliationError("IDENTITY_REVIEW_REQUIRED", "This new account uses an existing fund's name. Answer the open identity question first.")
        vehicle_id = existing[0] if existing is not None and existing[1] == "same" else self._vehicle(connection, row, subfund, now, entity_id, allow_existing=existing is None)
        if existing is not None and existing[1] == "distinct":
            self._audit(connection, "identity_separate_fund_created", "investment_vehicle", vehicle_id, "system", now, {"account_reference": reference, "candidate_id": row["id"]})
        account_id = str(uuid.uuid4())
        connection.execute("INSERT INTO accounts VALUES (?, ?, NULL, ?, ?, ?)", (account_id, investor_id, vehicle_id, reference, now))
        self._record_alias(identities, row, "account", account_id, reference, now)
        return account_id

    def _entity(self, connection: sqlite3.Connection, row: sqlite3.Row, name: str, now: str) -> str:
        identities = IdentityResolver(connection)
        entity_id = self._resolved_or_new(identities, "legal_entity", name)
        if entity_id is None:
            entity_id = str(uuid.uuid4())
            connection.execute("INSERT INTO legal_entities VALUES (?, ?, NULL, 'organization', NULL, ?)", (entity_id, name, now))
        self._record_alias(identities, row, "legal_entity", entity_id, name, now)
        return entity_id

    def _vehicle(self, connection: sqlite3.Connection, row: sqlite3.Row, name: str, now: str, investor_entity_id: str | None, *, allow_existing: bool = True) -> str:
        """Resolve a fund only among the investor's own (or unattached) funds.

        Two investors' funds that share a name are never merged by name.
        ``allow_existing=False`` creates a separate fund (the user declared it distinct).
        """
        identities = IdentityResolver(connection)
        vehicle_id = self._resolved_or_new(identities, "investment_vehicle", name, self._vehicle_scope(connection, investor_entity_id)) if allow_existing else None
        if vehicle_id is None:
            vehicle_id = str(uuid.uuid4())
            connection.execute("INSERT INTO investment_vehicles (id, name, legal_entity_id, has_separate_legal_personality, created_at, vehicle_type) VALUES (?, ?, NULL, 0, ?, 'subfund')", (vehicle_id, name, now))
        self._record_alias(identities, row, "investment_vehicle", vehicle_id, name, now)
        return vehicle_id

    @staticmethod
    def _resolved_or_new(identities: IdentityResolver, target_type: TargetType, name: str, within: set[str] | None = None) -> str | None:
        """Return the matched record id, or None when a new record is safe."""
        resolution = identities.resolve(target_type, name, within=within)
        if resolution.status == "ambiguous":
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "Entity identity is ambiguous.")
        if resolution.status == "ask":
            # Confirmation paths sync questions first; this guards a race.
            raise ReconciliationError("IDENTITY_REVIEW_REQUIRED", "A name may refer to an existing record. Answer the open identity question first.")
        return resolution.target_id if resolution.status == "matched" else None

    def _documented_migration(self, connection: sqlite3.Connection, identities: IdentityResolver, reference: object) -> dict[str, str | None] | None:
        """The known predecessor account a document says ``reference`` replaced.

        Applies only when the predecessor account exists, any investor and
        fund the statement names belong to it, and no statement names a
        different predecessor fund for the same new account.
        """
        if not isinstance(reference, str) or not reference.strip():
            return None
        rows = connection.execute(
            "SELECT * FROM account_migration_evidence WHERE normalized_new_account = ? ORDER BY created_at, rowid",
            (normalize_account_reference(reference),),
        ).fetchall()
        found: list[dict[str, str | None]] = []
        for row in rows:
            old_id = self._resolved_account_id(connection, row["old_account_reference"])
            if old_id is None:
                continue
            old = connection.execute(
                "SELECT a.id, a.external_reference, a.investor_id, a.investment_vehicle_id, i.legal_entity_id FROM accounts a JOIN investors i ON i.id = a.investor_id WHERE a.id = ?",
                (old_id,),
            ).fetchone()
            if old["investment_vehicle_id"] is None:
                continue
            if row["investor_text"] and not identities.is_alias_of("legal_entity", str(old["legal_entity_id"]), str(row["investor_text"])):
                continue
            if row["fund_text"] and self._established_name(
                connection, identities, "investment_vehicle", str(row["fund_text"]), old["investment_vehicle_id"], str(old["external_reference"]), old["legal_entity_id"],
            ) != "same":
                continue
            found.append({
                "evidence_id": str(row["id"]), "old_account_id": str(old["id"]), "old_reference": str(old["external_reference"]),
                "investor_id": str(old["investor_id"]), "vehicle_id": str(old["investment_vehicle_id"]), "legal_entity_id": str(old["legal_entity_id"]),
                "effective_date": row["effective_date"],
            })
        if not found or len({item["vehicle_id"] for item in found}) != 1:
            return None
        return found[0]

    def _existing_fund_for_new_account(
        self, connection: sqlite3.Connection, identities: IdentityResolver, subfund: str, investor_entity_id: str | None, reference: str,
    ) -> tuple[str, str] | None:
        """Whether a new account's fund name already belongs to a fund held through another account.

        Returns (fund id, "same") when the user confirmed it, (account id to
        ask about, "ask") when undecided, ("", "distinct") when the user
        declared the account a separate fund, or None when no such fund
        exists.  A shared name and investor never link a new account on their
        own.
        """
        resolution = identities.resolve("investment_vehicle", subfund, within=self._vehicle_scope(connection, investor_entity_id))
        if resolution.status != "matched" or resolution.target_id is None:
            return None
        accounts = [str(row["id"]) for row in connection.execute(
            "SELECT id FROM accounts WHERE investment_vehicle_id = ? ORDER BY created_at, id", (resolution.target_id,),
        )]
        if not accounts:
            return None
        decisions = {
            str(row["status"]) for row in connection.execute(
                f"SELECT status FROM identity_link_questions WHERE target_type = 'account' AND normalized_observed = ? AND suggested_target_id IN ({','.join('?' for _ in accounts)}) AND status <> 'open'",
                (normalize_account_reference(reference), *accounts),
            )
        }
        if "linked" in decisions:
            return resolution.target_id, "same"
        if "distinct" in decisions:
            return "", "distinct"
        return accounts[0], "ask"

    def _account_lineage(self, connection: sqlite3.Connection, account_id: str) -> list[tuple[str, str | None]]:
        """The account and its documented predecessors, each with the date it was replaced."""
        lineage: list[tuple[str, str | None]] = [(account_id, None)]
        seen = {account_id}
        current = account_id
        while True:
            row = connection.execute(
                "SELECT from_account_id, effective_date FROM account_relationships WHERE relationship_type = 'account_migration' AND to_account_id = ? ORDER BY created_at LIMIT 1",
                (current,),
            ).fetchone()
            if row is None or str(row["from_account_id"]) in seen:
                return lineage
            current = str(row["from_account_id"])
            seen.add(current)
            lineage.append((current, row["effective_date"]))

    @staticmethod
    def _vehicle_scope(connection: sqlite3.Connection, investor_entity_id: str | None) -> set[str]:
        """Funds a name may match for this investor: its own funds and unattached ones."""
        return {
            str(row["id"]) for row in connection.execute(
                "SELECT v.id FROM investment_vehicles v WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.investment_vehicle_id = v.id) "
                "OR EXISTS (SELECT 1 FROM accounts a JOIN investors i ON i.id = a.investor_id WHERE a.investment_vehicle_id = v.id AND i.legal_entity_id = ?)",
                (investor_entity_id,),
            )
        }

    def _established_name(
        self, connection: sqlite3.Connection, identities: IdentityResolver, target_type: TargetType, text: str,
        expected_id: object, account_reference: str, holder_entity_id: object,
    ) -> str:
        """Whether ``text`` names the record already established for an account.

        "same": a recorded alias of that record, a name a document explicitly
        states is that record for this same account, or "<holder> - <name>".
        "other": exclusively a different record's name.  "unknown": neither,
        so the user is asked.  Name similarity alone never returns "same".
        """
        if expected_id is None:
            return "other"
        expected = str(expected_id)
        variants = [text]
        if target_type == "investment_vehicle" and holder_entity_id is not None:
            prefix = re.match(r"^\s*(?P<holder>.+?)(?:\s+[-–—:]\s+|['’]s\s+)(?P<rest>.+)$", text)
            if prefix and identities.is_alias_of("legal_entity", str(holder_entity_id), prefix.group("holder")):
                variants.append(prefix.group("rest"))
        for variant in variants:
            if identities.is_alias_of(target_type, expected, variant) or self._documented_alias(connection, identities, target_type, variant, expected, account_reference):
                return "same"
        resolution = identities.resolve(target_type, text)
        return "other" if resolution.status == "matched" and resolution.target_id != expected else "unknown"

    @staticmethod
    def _documented_alias(
        connection: sqlite3.Connection, identities: IdentityResolver, target_type: TargetType, text: str, expected_id: str, account_reference: str,
    ) -> bool:
        """A document states ``text`` is the expected record for this account.

        The statement's account must equal the record's account, its canonical
        name must already belong to the expected record, and no statement may
        attach the same alias to a different account.
        """
        rows = connection.execute(
            "SELECT canonical_text, normalized_account FROM identity_alias_evidence WHERE target_type = ? AND normalized_alias = ?",
            (target_type, normalize_alias(target_type, text)),
        ).fetchall()
        wanted = normalize_account_reference(account_reference)
        stated_accounts = {row["normalized_account"] for row in rows if row["normalized_account"]}
        if not rows or stated_accounts != {wanted}:
            return False
        return any(
            identities.is_alias_of(target_type, expected_id, str(row["canonical_text"]))
            for row in rows if row["normalized_account"] == wanted
        )

    @staticmethod
    def _record_alias(identities: IdentityResolver, row: sqlite3.Row, target_type: TargetType, target_id: str, value: str, now: str) -> None:
        identities.record_alias(
            target_type, target_id, value, origin="observed",
            source_document_id=str(row["source_document_id"]), first_seen_on=row["document_issue_date"] or None, now=now,
        )

    @staticmethod
    def _resolved_account_id(connection: sqlite3.Connection, reference: object) -> str | None:
        if not isinstance(reference, str) or not reference.strip():
            return None
        resolution = IdentityResolver(connection).resolve("account", reference)
        return resolution.target_id if resolution.status == "matched" else None

    def _identity_review(
        self, connection: sqlite3.Connection, kind: str, payload: Mapping[str, object]
    ) -> tuple[list[tuple[TargetType, str, str, str]], bool]:
        """Find names that need a "same as?" answer before confirmation.

        Returns ``(questions, conflict)``.  Each question is (target type,
        observed text, suggested record id, match basis).  ``conflict`` means a
        known account is attributed to a party or subfund that is a different
        record, or that the user has declared distinct; that is a genuine
        inconsistency, not a naming question.
        """
        identities = IdentityResolver(connection)
        questions: list[tuple[TargetType, str, str, str]] = []
        conflict = False

        def party(target_type: TargetType, text: object, within: set[str] | None = None) -> None:
            if isinstance(text, str) and text.strip():
                resolution = identities.resolve(target_type, text, within=within)
                if resolution.status == "ask":
                    questions.append((target_type, text.strip(), str(resolution.target_id), str(resolution.basis)))

        def account(investor: object, subfund: object, reference: object) -> None:
            nonlocal conflict
            account_id = self._resolved_account_id(connection, reference)
            if account_id is None:
                migration = self._documented_migration(connection, identities, reference)
                if migration is not None:
                    # A documented successor account: same fund, same investor.
                    if isinstance(investor, str) and investor.strip() and not identities.is_alias_of("legal_entity", migration["legal_entity_id"], investor):
                        conflict = True
                    if isinstance(subfund, str) and subfund.strip():
                        verdict = self._established_name(connection, identities, "investment_vehicle", subfund, migration["vehicle_id"], migration["old_reference"], migration["legal_entity_id"])
                        if verdict == "other":
                            conflict = True
                        elif verdict == "unknown":
                            questions.append(("investment_vehicle", subfund.strip(), migration["vehicle_id"], "same_account_reference"))
                    return
                party("legal_entity", investor)
                investor_id = None
                if isinstance(investor, str) and investor.strip():
                    resolved = identities.resolve("legal_entity", investor)
                    investor_id = resolved.target_id if resolved.status == "matched" else None
                party("investment_vehicle", subfund, self._vehicle_scope(connection, investor_id))
                if isinstance(subfund, str) and subfund.strip() and isinstance(reference, str):
                    pending = self._existing_fund_for_new_account(connection, identities, subfund, investor_id, reference)
                    if pending is not None and pending[1] == "ask":
                        questions.append(("account", reference.strip(), pending[0], "existing_fund_new_account"))
                return
            established = connection.execute(
                "SELECT a.investment_vehicle_id, i.legal_entity_id FROM accounts a JOIN investors i ON i.id = a.investor_id WHERE a.id = ?",
                (account_id,),
            ).fetchone()
            checks: tuple[tuple[TargetType, object, object], ...] = (
                ("investment_vehicle", subfund, established["investment_vehicle_id"]),
                ("legal_entity", investor, established["legal_entity_id"]),
            )
            for target_type, text, expected in checks:
                if not isinstance(text, str) or not text.strip():
                    continue
                verdict = self._established_name(connection, identities, target_type, text, expected, str(reference), established["legal_entity_id"])
                if verdict == "same":
                    continue
                if verdict == "other" or self._declared_distinct(connection, target_type, text, str(expected)):
                    conflict = True
                else:
                    questions.append((target_type, text.strip(), str(expected), "same_account_reference"))

        if kind == "holding_snapshot":
            account(payload.get("investor"), payload.get("subfund"), payload.get("account"))
        elif kind == "securities_transfer":
            account(payload.get("investor"), payload.get("fromSubfund"), payload.get("fromAccount"))
            account(payload.get("investor"), payload.get("toSubfund"), payload.get("toAccount"))
        elif kind == "claim_assignment":
            party("legal_entity", payload.get("fromEntity"))
            party("legal_entity", payload.get("toEntity"))
        return list(dict.fromkeys(questions)), conflict

    def _refresh_review_status(self, connection: sqlite3.Connection, now: str) -> None:
        """Return deterministic candidates to routine review once nothing blocks them."""
        rows = connection.execute(
            "SELECT * FROM reconciliation_candidates WHERE lifecycle_status = 'candidate' AND validation_status = 'review_required' AND origin = 'deterministic' ORDER BY created_at, id"
        ).fetchall()
        for row in rows:
            if self._policy_review_reason(connection, row) is None:
                connection.execute("UPDATE reconciliation_candidates SET validation_status = 'supported' WHERE id = ?", (row["id"],))

    @staticmethod
    def _identity_target_label(connection: sqlite3.Connection, target_type: str, target_id: str) -> str | None:
        query = {
            "legal_entity": "SELECT display_name AS label FROM legal_entities WHERE id = ?",
            "investment_vehicle": "SELECT name AS label FROM investment_vehicles WHERE id = ?",
            "account": "SELECT external_reference AS label FROM accounts WHERE id = ?",
        }[target_type]
        row = connection.execute(query, (target_id,)).fetchone()
        return None if row is None else str(row["label"])

    def _identity_question_result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Mapping[str, object]:
        aliases = connection.execute(
            "SELECT alias_text, origin, source_document_id, first_seen_on FROM identity_aliases WHERE target_type = ? AND target_id = ? ORDER BY created_at, alias_text",
            (row["target_type"], row["suggested_target_id"]),
        ).fetchall()
        return {
            "id": row["id"], "status": row["status"], "targetType": row["target_type"],
            "observedText": row["observed_text"], "matchBasis": row["match_basis"],
            "suggested": {
                "id": row["suggested_target_id"],
                "label": self._identity_target_label(connection, str(row["target_type"]), str(row["suggested_target_id"])),
                "aliases": [
                    {"text": alias["alias_text"], "origin": alias["origin"], "sourceDocumentId": alias["source_document_id"], "firstSeenOn": alias["first_seen_on"]}
                    for alias in aliases
                ],
            },
            "candidateId": row["candidate_id"], "candidateType": row["candidate_type"],
            "sourceDocumentId": row["source_document_id"], "sourceReference": row["source_reference"],
            "documentIssueDate": row["document_issue_date"] or None, "createdAt": row["created_at"], "resolvedAt": row["resolved_at"],
        }

    def _account_details_incomplete(self, connection: sqlite3.Connection, kind: str, payload: Mapping[str, object]) -> bool:
        """An unknown account can only be created when the source names its investor and subfund."""
        sides = {
            "holding_snapshot": (("account", "subfund"),),
            "securities_transfer": (("fromAccount", "fromSubfund"), ("toAccount", "toSubfund")),
        }.get(kind, ())
        for account_key, subfund_key in sides:
            if self._resolved_account_id(connection, payload.get(account_key)) is None and not (
                isinstance(payload.get("investor"), str) and isinstance(payload.get(subfund_key), str)
            ) and self._documented_migration(connection, IdentityResolver(connection), payload.get(account_key)) is None:
                return True
        return False

    @staticmethod
    def _declared_distinct(connection: sqlite3.Connection, target_type: str, text: str, target_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM identity_link_questions WHERE target_type = ? AND normalized_observed = ? AND suggested_target_id = ? AND status = 'distinct'",
            (target_type, normalize_alias(target_type, text), target_id),
        ).fetchone() is not None

    def _sync_identity_questions(self, connection: sqlite3.Connection, candidate_id: str, now: str) -> int:
        """Persist open "same as?" questions for a provisional candidate.

        Idempotent: one question per observed spelling and suggested record,
        shared by every candidate that raises it.  Returns how many questions
        this candidate is waiting on.
        """
        row = self._candidate_row(connection, candidate_id)
        if row["lifecycle_status"] != "candidate":
            return 0
        questions, _conflict = self._identity_review(connection, str(row["candidate_type"]), json.loads(str(row["payload_json"])))
        for target_type, text, suggested_id, basis in questions:
            normalized = normalize_alias(target_type, text)
            existing = connection.execute(
                "SELECT id FROM identity_link_questions WHERE target_type = ? AND normalized_observed = ? AND suggested_target_id = ?",
                (target_type, normalized, suggested_id),
            ).fetchone()
            if existing is not None:
                continue
            question_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO identity_link_questions (id, target_type, observed_text, normalized_observed, suggested_target_id, match_basis, candidate_id, source_document_id, status, created_at, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL)",
                (question_id, target_type, text, normalized, suggested_id, basis, row["id"], row["source_document_id"], now),
            )
            self._issue(connection, row, "identity_link_question", question_id, "possible same identity", now)
            self._audit(connection, "identity_link_question_raised", target_type, suggested_id, "system", now, {
                "question_id": question_id, "candidate_id": row["id"], "match_basis": basis,
            })
        if questions:
            connection.execute("UPDATE reconciliation_candidates SET validation_status = 'review_required' WHERE id = ?", (row["id"],))
        return len(questions)

    @staticmethod
    def _security(connection: sqlite3.Connection, isin: str, now: str) -> str:
        row = connection.execute("SELECT id FROM securities WHERE isin = ?", (isin,)).fetchone()
        if row:
            return str(row["id"])
        security_id = str(uuid.uuid4())
        connection.execute("INSERT INTO securities VALUES (?, ?, NULL, ?)", (security_id, isin, now))
        return security_id

    @staticmethod
    def _evidence(connection: sqlite3.Connection, row: sqlite3.Row, target_type: str, target_id: str, now: str) -> None:
        existing = connection.execute("SELECT id FROM evidence_references WHERE source_document_id = ? AND target_type = ? AND target_id = ? AND source_location = ?", (row["source_document_id"], target_type, target_id, row["source_reference"])).fetchone()
        if existing is None:
            connection.execute("INSERT INTO evidence_references VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), row["source_document_id"], target_type, target_id, row["source_reference"], row["evidence_snippet"], now))

    @staticmethod
    def _issue(connection: sqlite3.Connection, row: sqlite3.Row, target_type: str, target_id: str, reason: str, now: str) -> None:
        existing = connection.execute("SELECT id FROM issues WHERE status = 'open' AND affected_type = ? AND affected_id = ? AND reason = ?", (target_type, target_id, reason)).fetchone()
        if existing is None:
            connection.execute("INSERT INTO issues VALUES (?, 'high', 'open', ?, ?, ?, ?, ?, NULL)", (str(uuid.uuid4()), target_type, target_id, reason, row["source_document_id"], now))

    @staticmethod
    def _audit(connection: sqlite3.Connection, event_type: str, target_type: str, target_id: str, actor_type: str, now: str, details: Mapping[str, object]) -> None:
        connection.execute("INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), event_type, target_type, target_id, actor_type, now, json.dumps(details, sort_keys=True)))

    def _candidate_extraction(self, connection: sqlite3.Connection, document_id: str, params: Mapping[str, object]) -> sqlite3.Row:
        extraction_id = params.get("extractionId")
        if extraction_id is not None and not isinstance(extraction_id, str):
            raise ReconciliationError("INVALID_CANDIDATE", "Candidate extraction id is invalid.")
        query = "SELECT e.id, e.parser_version FROM document_extractions e WHERE e.source_document_id = ? AND e.status = 'parsed' " + ("AND e.id = ? " if extraction_id else "") + "ORDER BY e.completed_at DESC, e.id DESC LIMIT 1"
        row = connection.execute(query, (document_id, extraction_id) if extraction_id else (document_id,)).fetchone()
        if row is None:
            raise ReconciliationError("INVALID_EVIDENCE", "Candidate extraction is unavailable.")
        if params.get("parserVersion") is not None and params["parserVersion"] != row["parser_version"]:
            raise ReconciliationError("INVALID_EVIDENCE", "Candidate parser version does not match its extraction.")
        return row

    def _current_extraction(self, connection: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT d.id AS source_document_id, d.file_format, d.imported_at, e.id AS extraction_id, e.parser_version FROM source_documents d JOIN document_extractions e ON e.source_document_id = d.id WHERE d.id = ? AND e.status = 'parsed' ORDER BY e.completed_at DESC, e.id DESC LIMIT 1", (document_id,)).fetchone()

    @staticmethod
    def _candidate_row(connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM reconciliation_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise ReconciliationError("CANDIDATE_NOT_FOUND", "Candidate was not found.")
        return row

    @staticmethod
    def _candidate_result(row: sqlite3.Row) -> Mapping[str, object]:
        return {"id": row["id"], "type": row["candidate_type"], "status": row["lifecycle_status"], "origin": row["origin"], "sourceDocumentId": row["source_document_id"], "documentIssueDate": row["document_issue_date"] or None, "payload": json.loads(row["payload_json"]), "sourceReference": row["source_reference"], "evidenceSnippet": row["evidence_snippet"], "extractionId": row["extraction_id"], "parserVersion": row["parser_version"], "confidence": row["confidence"], "validationStatus": row["validation_status"], "documentImportedAt": row["imported_at"], "createdAt": row["created_at"], "confirmedAt": row["confirmed_at"]}

    @classmethod
    def _potential_candidate_conflicts(
        cls, candidates: Sequence[Mapping[str, object]]
    ) -> dict[str, Mapping[str, object]]:
        """Describe conflicting provisional snapshots without changing records.

        This is a read-only review aid. It deliberately marks every competing
        observation rather than selecting one by import order, issue date, or
        source type. Confirmation remains governed by the transactional policy.
        """
        groups: dict[tuple[str, str, str], list[tuple[Mapping[str, object], Mapping[str, object], Decimal]]] = {}
        for candidate in candidates:
            if candidate.get("type") != "holding_snapshot" or candidate.get("status") != "candidate":
                continue
            payload = candidate.get("payload")
            if not isinstance(payload, Mapping):
                continue
            account, isin, position_date, quantity = (
                payload.get("account"), payload.get("isin"), payload.get("positionDate"), payload.get("quantity")
            )
            if not all(isinstance(value, str) and value for value in (account, isin, position_date, quantity)):
                continue
            try:
                nominal = Decimal(str(quantity))
            except InvalidOperation:
                continue
            groups.setdefault((normalize_account_reference(account), isin, position_date), []).append((candidate, payload, nominal))

        conflicts: dict[str, Mapping[str, object]] = {}
        for (_account_key, isin, position_date), observations in groups.items():
            account = str(observations[0][1]["account"])
            if len({quantity for _, _, quantity in observations}) < 2:
                continue
            ordered = sorted(observations, key=lambda item: (item[2], str(item[0]["id"])))
            values = [item[2] for item in ordered]
            detail = {
                "type": "competing_dated_holding",
                "account": account,
                "isin": isin,
                "asOfDate": position_date,
                "difference": cls._decimal(max(values) - min(values)),
                "observations": [
                    {
                        "candidateId": candidate["id"],
                        "quantity": cls._decimal(quantity),
                        "investor": payload.get("investor"),
                        "subfund": payload.get("subfund"),
                        "sourceDocumentId": candidate["sourceDocumentId"],
                        "sourceReference": candidate["sourceReference"],
                        "documentIssueDate": candidate["documentIssueDate"],
                    }
                    for candidate, payload, quantity in ordered
                ],
            }
            for candidate, _, _ in observations:
                conflicts[str(candidate["id"])] = detail
        return conflicts

    # Evidence helpers -----------------------------------------------------

    def _require_validated_ai_proposals(self, connection: sqlite3.Connection, document_id: str, extraction_id: str, references: Sequence[str]) -> None:
        placeholders = ",".join("?" for _ in references)
        rows = connection.execute("SELECT DISTINCT p.source_reference FROM ai_proposals p JOIN ai_processing_jobs j ON j.id = p.job_id WHERE j.source_document_id = ? AND j.extraction_id = ? AND p.validation_status = 'validated' AND p.source_reference IN (" + placeholders + ")", (document_id, extraction_id, *references)).fetchall()
        if {str(row["source_reference"]) for row in rows} != set(references):
            raise ReconciliationError("UNVALIDATED_AI_PROPOSAL", "AI-origin candidates require validated proposals for every cited field.")

    def _reference_texts(self, connection: sqlite3.Connection, extraction_id: str, references: Sequence[str]) -> list[str]:
        texts: list[str] = []
        for reference in references:
            exact = connection.execute("SELECT value_text FROM extracted_content WHERE extraction_id = ? AND source_reference = ?", (extraction_id, reference)).fetchall()
            if exact:
                texts.extend(str(row["value_text"] or "") for row in exact)
                continue
            block = _PDF_BLOCK_REFERENCE.fullmatch(reference)
            if block:
                row = connection.execute("SELECT b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ? AND p.page_number = ? AND b.block_index = ?", (extraction_id, int(block.group(1)), int(block.group(2)))).fetchone()
                if row is None:
                    return []
                texts.append(str(row["text"]))
                continue
            sheet_range = _SHEET_RANGE_REFERENCE.fullmatch(reference)
            if sheet_range:
                start_column, end_column = self._column_number(sheet_range.group("start_column")), self._column_number(sheet_range.group("end_column") or sheet_range.group("start_column"))
                start_row, end_row = int(sheet_range.group("start_row")), int(sheet_range.group("end_row") or sheet_range.group("start_row"))
                rows = connection.execute("SELECT value_text FROM extracted_content WHERE extraction_id = ? AND sheet_name = ? AND row_number BETWEEN ? AND ? AND column_number BETWEEN ? AND ? AND is_empty = 0", (extraction_id, sheet_range.group("sheet"), start_row, end_row, start_column, end_column)).fetchall()
                if not rows:
                    return []
                texts.extend(str(row["value_text"] or "") for row in rows)
                continue
            page = _PDF_PAGE_REFERENCE.fullmatch(reference)
            if page:
                row = connection.execute("SELECT value_text FROM extracted_content WHERE extraction_id = ? AND source_reference = ?", (extraction_id, reference)).fetchone()
                if row is None:
                    return []
                texts.append(str(row["value_text"] or ""))
                continue
            return []
        return texts

    def _document_text(self, connection: sqlite3.Connection, extraction_id: str) -> str:
        generic = connection.execute("SELECT value_text FROM extracted_content WHERE extraction_id = ? AND is_empty = 0", (extraction_id,)).fetchall()
        blocks = connection.execute("SELECT b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ?", (extraction_id,)).fetchall()
        return "\n".join([*(str(row["value_text"] or "") for row in generic), *(str(row["text"] or "") for row in blocks)])

    @staticmethod
    def _references(value: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(part.strip() for part in value.split(";") if part.strip()))

    # Parsing helpers ------------------------------------------------------

    def _sheet_metadata(self, sheet: str, grid: Mapping[int, Mapping[int, str]], skip_rows: Iterable[int] = ()) -> Mapping[str, object]:
        """Labelled cells above a table ("Investor | X"); table header rows are not labels."""
        investor: str | None = None
        issue_date: str | None = None
        references: list[str] = []
        skipped = set(skip_rows)
        for row_number, row in grid.items():
            if row_number in skipped:
                continue
            for column, cell in row.items():
                key, adjacent = self._field_key(cell), row.get(column + 1, "")
                if key in {"investor", "investor_name", "investor_group"} and adjacent.strip():
                    investor = adjacent.strip()
                    references.append(self._sheet_row_reference(sheet, row_number, column, column + 1))
                if key in {"issue_date", "document_issue_date"}:
                    parsed = self._parse_date(adjacent)
                    if parsed:
                        issue_date = parsed
                        references.append(self._sheet_row_reference(sheet, row_number, column, column + 1))
        return {"investor": investor, "issue_date": issue_date, "references": tuple(references)}

    def _holding_headers(self, grid: Mapping[int, Mapping[int, str]]) -> Iterable[tuple[int, Mapping[str, int]]]:
        """Header rows naming account, ISIN, quantity and as-of columns (same vocabulary as PDF tables)."""
        for row_number in sorted(grid):
            columns: dict[str, int] = {}
            for column, value in sorted(grid[row_number].items()):
                name = column_name(value)
                if name is not None:
                    columns.setdefault("position_date" if name == "as_of" else name, column)
            if {"account", "isin", "quantity", "position_date"} <= set(columns):
                yield row_number, columns

    @staticmethod
    def _field_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")

    @staticmethod
    def _sheet_row_reference(sheet: str, row: int, first: int, last: int) -> str:
        return f"{sheet}!{ReconciliationService._column_letters(first)}{row}:{ReconciliationService._column_letters(last)}{row}"

    def _candidate_input(self, context: sqlite3.Row, kind: str, reference: str, snippet: str, issue_date: str, origin: str, payload: Mapping[str, object], confidence: float | None, validation_status: str) -> Mapping[str, object]:
        return {"type": kind, "sourceDocumentId": context["source_document_id"], "sourceReference": reference, "evidenceSnippet": snippet, "documentIssueDate": issue_date, "origin": origin, "payload": dict(payload), "extractionId": context["extraction_id"], "parserVersion": context["parser_version"], "confidence": confidence, "validationStatus": validation_status}

    def _ai_candidate_input(self, context: sqlite3.Row, kind: str, rows: Sequence[sqlite3.Row | None], issue_date: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        selected = [row for row in rows if row is not None]
        confidences = [float(row["confidence"]) for row in selected if row["confidence"] is not None]
        return self._candidate_input(context, kind, "; ".join(str(row["source_reference"]) for row in selected), "Validated AI field-level evidence", issue_date, "ai_validated", payload, min(confidences) if confidences else None, "validated_ai")

    def _document_issue_date(self, connection: sqlite3.Connection, context: sqlite3.Row) -> str | None:
        if context["file_format"] in {"xlsx", "xls", "csv"}:
            rows = connection.execute("SELECT sheet_name, row_number, column_number, value_text FROM extracted_content WHERE extraction_id = ? AND is_empty = 0", (context["extraction_id"],)).fetchall()
            by_sheet: dict[str, dict[int, dict[int, str]]] = {}
            for row in rows:
                if row["sheet_name"] is not None and row["row_number"] is not None and row["column_number"] is not None:
                    by_sheet.setdefault(str(row["sheet_name"]), {}).setdefault(int(row["row_number"]), {})[int(row["column_number"])] = str(row["value_text"] or "")
            for sheet, grid in by_sheet.items():
                issue = self._sheet_metadata(sheet, grid).get("issue_date")
                if isinstance(issue, str):
                    return issue
            return None
        issue_date, _block = labelled_value([(0, self._document_text(connection, str(context["extraction_id"])))], "issue_date")
        return issue_date if isinstance(issue_date, str) else None

    @staticmethod
    def _pdf_references(page_number: int, blocks: Sequence[sqlite3.Row]) -> str:
        return "; ".join(f"page {page_number} block {block['block_index']}" for block in blocks[:8])

    @classmethod
    def _parse_date(cls, value: object) -> str | None:
        return parse_date(value)

    @staticmethod
    def _quantity(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        try: number = Decimal(value.replace(",", "").strip())
        except InvalidOperation: return None
        return ReconciliationService._decimal(number) if number > 0 else None

    @staticmethod
    def _percentage(value: object) -> str | None:
        quantity = ReconciliationService._quantity(str(value))
        return quantity if quantity is not None and Decimal(quantity) <= Decimal("100") else None

    @staticmethod
    def _column_letters(number: int) -> str:
        letters = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            letters = chr(65 + remainder) + letters
        return letters

    @staticmethod
    def _column_number(value: str) -> int:
        number = 0
        for letter in value: number = number * 26 + ord(letter) - 64
        return number

    @staticmethod
    def _date(value: object) -> bool:
        return isinstance(value, str) and ReconciliationService._parse_date(value) == value

    @classmethod
    def _payload_supported(cls, kind: str, payload: Mapping[str, object], issue_date: str, evidence: str, resolved_account: bool) -> bool:
        """Every business value must appear in the source text.

        Dates and amounts may be written in any accepted format ("15/02/2025",
        "1.000.000,00"), so they are also matched by parsing the source.
        """
        text = cls._normalise_text(evidence)
        dates = {value for value, _start in find_dates(evidence) if value}
        amounts = find_amounts(evidence)
        values: list[tuple[str, object]] = [("documentIssueDate", issue_date)] if issue_date else []
        for key, value in payload.items():
            if value is None or (kind == "holding_snapshot" and key == "investor" and resolved_account):
                continue
            values.append((key, value))

        def supported(key: str, value: object) -> bool:
            if cls._normalise_text(str(value)) in text:
                return True
            if key in {"documentIssueDate", "positionDate", "effectiveDate"}:
                return str(value) in dates
            if key in {"quantity", "percentage"}:
                try:
                    return Decimal(str(value)) in amounts
                except InvalidOperation:
                    return False
            return False

        return all(supported(key, value) for key, value in values)

    @staticmethod
    def _normalise_text(value: str) -> str:
        return " ".join(value.casefold().replace(",", "").split())

    def _validate(self, kind: str, payload: Mapping[str, object]) -> None:
        fields = {"holding_snapshot": ("investor", "subfund", "account", "isin", "quantity", "positionDate"), "securities_transfer": ("investor", "fromSubfund", "fromAccount", "toSubfund", "toAccount", "isin", "quantity", "effectiveDate"), "claim_assignment": ("claimReference", "fromEntity", "toEntity", "isin", "effectiveDate")}[kind]
        allowed = set(fields) | ({"percentage"} if kind == "claim_assignment" else set())
        if set(payload) - allowed or not set(fields) <= set(payload):
            raise ReconciliationError("INVALID_PAYLOAD", "Candidate business fields are incomplete.")
        optional = {"holding_snapshot": {"investor", "subfund"}, "securities_transfer": {"investor", "fromSubfund", "toSubfund"}, "claim_assignment": set()}[kind]
        for field in fields:
            if field in optional and payload[field] is None: continue
            if not isinstance(payload[field], str) or not payload[field].strip():
                raise ReconciliationError("INVALID_PAYLOAD", "Candidate business fields are incomplete.")
        date_field = "positionDate" if kind == "holding_snapshot" else "effectiveDate"
        if not self._date(payload[date_field]) or len(str(payload["isin"])) != 12:
            raise ReconciliationError("INVALID_PAYLOAD", "Candidate business fields are incomplete.")
        if kind != "claim_assignment" and self._quantity(payload["quantity"]) is None:
            raise ReconciliationError("INVALID_PAYLOAD", "Quantity must be positive.")
        if kind == "claim_assignment" and "percentage" in payload and self._percentage(payload["percentage"]) is None:
            raise ReconciliationError("INVALID_PAYLOAD", "Assignment percentage must be between zero and one hundred.")

    @staticmethod
    def _decimal(value: Decimal) -> str:
        result = format(value.normalize(), "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
