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
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence

from ..storage.service import StorageService


_CANDIDATE_TYPES = frozenset({"holding_snapshot", "securities_transfer", "claim_assignment"})
_ORIGINS = frozenset({"deterministic", "ai_validated"})
_VALIDATION_STATUSES = frozenset({"supported", "validated_ai", "review_required"})
_PDF_BLOCK_REFERENCE = re.compile(r"^page\s+(\d+)\s+block\s+(\d+)$", re.IGNORECASE)
_PDF_PAGE_REFERENCE = re.compile(r"^page\s+(\d+)$", re.IGNORECASE)
_SHEET_RANGE_REFERENCE = re.compile(
    r"^(?P<sheet>.+)!(?P<start_column>[A-Z]+)(?P<start_row>[1-9][0-9]*)"
    r"(?::(?P<end_column>[A-Z]+)(?P<end_row>[1-9][0-9]*))?$"
)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TEXT_DATE = re.compile(r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\b", re.IGNORECASE)


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
        staged: list[Mapping[str, object]] = []
        skipped: list[str] = []
        for candidate in generated:
            try:
                staged.append(self.stage(candidate))
            except ReconciliationError as error:
                # A partially understood source remains local and reviewable;
                # importing a document must not fail because it has no safe
                # business-record interpretation.
                skipped.append(error.code)
        return {
            "documentId": document_id,
            "extractionId": context["extraction_id"],
            "parserVersion": context["parser_version"],
            "generated": len(generated),
            "staged": staged,
            "skipped": sorted(skipped),
        }

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
            or not self._date(params["documentIssueDate"])
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
                and len(connection.execute("SELECT id FROM accounts WHERE external_reference = ?", (payload["account"],)).fetchall()) == 1
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
                + ("WHERE c.lifecycle_status = ? " if status else "")
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
        with self._database.transaction() as connection:
            row = self._candidate_row(connection, str(params["candidateId"]))
            if row["lifecycle_status"] == "confirmed":
                return {"id": row["id"], "type": row["candidate_type"], "status": "confirmed"}
            if row["lifecycle_status"] == "rejected":
                raise ReconciliationError("CANDIDATE_REJECTED", "A rejected candidate cannot be confirmed.")
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
            metadata = self._sheet_metadata(sheet, grid)
            issue_date, investor = metadata.get("issue_date"), metadata.get("investor")
            if not isinstance(issue_date, str) or not isinstance(investor, str):
                continue
            for header_row, columns in self._holding_headers(grid):
                for row_number in sorted(number for number in grid if number > header_row):
                    values = {name: grid[row_number].get(column, "").strip() for name, column in columns.items()}
                    if not any(values.values()):
                        continue
                    if not all(values.get(name) for name in ("account", "subfund", "isin", "quantity", "position_date")):
                        continue
                    position_date, quantity = self._parse_date(values["position_date"]), self._quantity(values["quantity"])
                    isin = values["isin"].upper().replace(" ", "")
                    if position_date is None or quantity is None or len(isin) != 12:
                        continue
                    refs = [*metadata["references"], self._sheet_row_reference(sheet, row_number, min(columns.values()), max(columns.values()))]
                    candidates.append(self._candidate_input(context, "holding_snapshot", "; ".join(dict.fromkeys(refs)), f"Structured holdings row {sheet} {row_number}", issue_date, "deterministic", {
                        "investor": investor, "subfund": values["subfund"], "account": values["account"], "isin": isin, "quantity": quantity, "positionDate": position_date,
                    }, 1.0, "supported"))
        return candidates

    def _pdf_candidates(self, connection: sqlite3.Connection, context: sqlite3.Row) -> list[Mapping[str, object]]:
        rows = connection.execute(
            "SELECT p.page_number, b.block_index, b.text FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ? ORDER BY p.page_number, b.block_index",
            (context["extraction_id"],),
        ).fetchall()
        by_page: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            by_page.setdefault(int(row["page_number"]), []).append(row)
        candidates: list[Mapping[str, object]] = []
        for page_number, blocks in by_page.items():
            text, compact = " ".join(str(block["text"]) for block in blocks), ""
            compact = " ".join(text.split())
            issue_date = self._labelled_date(text, "issue date")
            if issue_date is None:
                continue
            references = self._pdf_references(page_number, blocks)
            transfer = re.search(r"(?:securities\s+)?transfer\s+of\s+([\d,]+(?:\.\d+)?)\s+nominal(?:\s+units)?\s+of\s+ISIN\s+([A-Z0-9]{12}).*?from\s+(.+?),\s*account\s+([A-Z0-9-]+),\s*to\s+(.+?),\s*account\s+([A-Z0-9-]+)", compact, re.IGNORECASE)
            investor = re.search(r"Investor\s+group\s*:\s*(.+?)(?=\s+(?:[A-Z][A-Za-z]+\s+Custody|confirms|$))", compact, re.IGNORECASE)
            effective_date = self._labelled_date(compact, "effective date")
            if transfer and investor and effective_date:
                quantity = self._quantity(transfer.group(1))
                if quantity is not None:
                    candidates.append(self._candidate_input(context, "securities_transfer", references, "Explicit securities transfer notice", issue_date, "deterministic", {
                        "investor": investor.group(1).strip(), "fromSubfund": transfer.group(3).strip(), "fromAccount": transfer.group(4).strip(), "toSubfund": transfer.group(5).strip(), "toAccount": transfer.group(6).strip(), "isin": transfer.group(2).upper(), "quantity": quantity, "effectiveDate": effective_date,
                    }, 1.0, "supported"))
            assignment = re.search(r"Claim\s+reference\s*:\s*([A-Z0-9-]+).*?(.+?)\s+assigns\s+([\d.]+)\s+percent.*?ISIN\s+([A-Z0-9]{12})\s+to\s+(.+?),\s+effective", compact, re.IGNORECASE)
            if assignment and effective_date:
                percentage = self._percentage(assignment.group(3))
                if percentage is not None:
                    candidates.append(self._candidate_input(context, "claim_assignment", references, "Explicit claim-right assignment notice", issue_date, "deterministic", {
                        "claimReference": assignment.group(1).strip(), "fromEntity": assignment.group(2).strip(), "toEntity": assignment.group(5).strip(), "isin": assignment.group(4).upper(), "effectiveDate": effective_date, "percentage": percentage,
                    }, 1.0, "supported"))
            holding = re.search(r"For\s+account\s+([A-Z0-9-]+),\s*(.+?),\s*the\s+recorded\s+holding\s+of\s+ISIN\s+([A-Z0-9]{12})\s+is\s+([\d,]+(?:\.\d+)?)\s+nominal\s+units\s+as\s+of\s+(.+?)(?:\.|$)", compact, re.IGNORECASE)
            if holding:
                position_date, quantity = self._parse_date(holding.group(5)), self._quantity(holding.group(4))
                if position_date and quantity:
                    # This notice does not establish an investor. It remains
                    # unknown unless a unique confirmed account resolves it.
                    candidates.append(self._candidate_input(context, "holding_snapshot", references, "Custodian position statement", issue_date, "deterministic", {
                        "investor": None, "subfund": holding.group(2).strip(), "account": holding.group(1).strip(), "isin": holding.group(3).upper(), "quantity": quantity, "positionDate": position_date,
                    }, 1.0, "supported"))
        return candidates

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
        account = self._account(connection, payload.get("investor"), str(payload["subfund"]), str(payload["account"]), now)
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
        source = self._account(connection, payload.get("investor"), str(payload["fromSubfund"]), str(payload["fromAccount"]), now)
        destination = self._account(connection, payload.get("investor"), str(payload["toSubfund"]), str(payload["toAccount"]), now)
        security, quantity = self._security(connection, str(payload["isin"]), now), Decimal(str(payload["quantity"]))
        prior = connection.execute("SELECT quantity, position_date FROM holding_positions WHERE account_id = ? AND security_id = ? AND position_date <= ? ORDER BY position_date DESC, created_at DESC LIMIT 1", (source, security, payload["effectiveDate"])).fetchone()
        if prior is None:
            raise ReconciliationError("IMPOSSIBLE_TRANSFER", "Transfer has no confirmed source balance.")
        available = Decimal(str(prior["quantity"]))
        for transfer in connection.execute("SELECT from_account_id, to_account_id, quantity FROM securities_transfers WHERE security_id = ? AND effective_date > ? AND effective_date <= ? ORDER BY effective_date, id", (security, prior["position_date"], payload["effectiveDate"])):
            if transfer["from_account_id"] == source:
                available -= Decimal(str(transfer["quantity"]))
            if transfer["to_account_id"] == source:
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
        from_entity, to_entity = self._entity(connection, str(payload["fromEntity"]), now), self._entity(connection, str(payload["toEntity"]), now)
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
        if self._candidate_has_open_holding_conflict(connection, str(row["id"])):
            return "conflicting_dated_holding"
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
        accounts = connection.execute("SELECT id FROM accounts WHERE external_reference = ?", (payload.get("account"),)).fetchall()
        security = connection.execute("SELECT id FROM securities WHERE isin = ?", (payload.get("isin"),)).fetchone()
        if len(accounts) != 1 or security is None:
            return False
        quantities = connection.execute("SELECT quantity FROM holding_positions WHERE account_id = ? AND security_id = ? AND position_date = ?", (accounts[0]["id"], security["id"], payload.get("positionDate"))).fetchall()
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
        account, isin, position_date = identity
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

        key_material = json.dumps({"account": account, "isin": isin, "positionDate": position_date}, sort_keys=True, separators=(",", ":"))
        conflict_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
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
        return (str(account).strip(), str(isin).upper().replace(" ", ""), str(position_date))

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
        accounts = connection.execute("SELECT id FROM accounts WHERE external_reference = ?", (payload.get("fromAccount"),)).fetchall()
        security = connection.execute("SELECT id FROM securities WHERE isin = ?", (payload.get("isin"),)).fetchone()
        if len(accounts) != 1 or security is None:
            return False
        return connection.execute("SELECT 1 FROM holding_positions WHERE account_id = ? AND security_id = ? AND position_date <= ? LIMIT 1", (accounts[0]["id"], security["id"], payload.get("effectiveDate"))).fetchone() is not None

    def _mark_review_required(self, connection: sqlite3.Connection, row: sqlite3.Row, reason: str, now: str) -> None:
        connection.execute("UPDATE reconciliation_candidates SET validation_status = 'review_required' WHERE id = ?", (row["id"],))
        # A legal assignment or AI-origin observation is review-required by
        # policy, not an inconsistency. Persist an issue only when the source
        # set itself is contradictory or operationally impossible.
        if reason == "conflicting_dated_holding" and self._candidate_has_open_holding_conflict(connection, str(row["id"])):
            return
        if reason in {"conflicting_dated_holding", "transfer_source_balance_unavailable"}:
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

    def _account(self, connection: sqlite3.Connection, investor: object, subfund: str, reference: str, now: str) -> str:
        matching = connection.execute(
            "SELECT a.id, fund.name AS subfund, entity.display_name AS investor "
            "FROM accounts a JOIN investors i ON i.id = a.investor_id "
            "JOIN legal_entities entity ON entity.id = i.legal_entity_id "
            "LEFT JOIN investment_vehicles fund ON fund.id = a.investment_vehicle_id "
            "WHERE a.external_reference = ?",
            (reference,),
        ).fetchall()
        if len(matching) > 1:
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "Account identity is ambiguous.")
        if matching:
            existing = matching[0]
            if existing["subfund"] != subfund or (
                isinstance(investor, str) and investor.strip() and existing["investor"] != investor
            ):
                raise ReconciliationError("AMBIGUOUS_IDENTITY", "The account identifier conflicts with its established investor or subfund.")
            return str(matching[0]["id"])
        if not isinstance(investor, str) or not investor.strip():
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "The investor is not established for this account.")
        entity_id = self._entity(connection, investor, now)
        investor_row = connection.execute("SELECT id FROM investors WHERE legal_entity_id = ?", (entity_id,)).fetchone()
        investor_id = str(investor_row["id"]) if investor_row else str(uuid.uuid4())
        if investor_row is None:
            connection.execute("INSERT INTO investors VALUES (?, ?, NULL, ?)", (investor_id, entity_id, now))
        vehicle = connection.execute("SELECT id FROM investment_vehicles WHERE name = ? AND vehicle_type = 'subfund'", (subfund,)).fetchone()
        vehicle_id = str(vehicle["id"]) if vehicle else str(uuid.uuid4())
        if vehicle is None:
            connection.execute("INSERT INTO investment_vehicles (id, name, legal_entity_id, has_separate_legal_personality, created_at, vehicle_type) VALUES (?, ?, NULL, 0, ?, 'subfund')", (vehicle_id, subfund, now))
        account = connection.execute("SELECT id FROM accounts WHERE investor_id = ? AND external_reference = ?", (investor_id, reference)).fetchone()
        if account:
            return str(account["id"])
        account_id = str(uuid.uuid4())
        connection.execute("INSERT INTO accounts VALUES (?, ?, NULL, ?, ?, ?)", (account_id, investor_id, vehicle_id, reference, now))
        return account_id

    @staticmethod
    def _entity(connection: sqlite3.Connection, name: str, now: str) -> str:
        rows = connection.execute("SELECT id FROM legal_entities WHERE display_name = ?", (name,)).fetchall()
        if len(rows) > 1:
            raise ReconciliationError("AMBIGUOUS_IDENTITY", "Entity identity is ambiguous.")
        if rows:
            return str(rows[0]["id"])
        entity_id = str(uuid.uuid4())
        connection.execute("INSERT INTO legal_entities VALUES (?, ?, NULL, 'organization', NULL, ?)", (entity_id, name, now))
        return entity_id

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
        return {"id": row["id"], "type": row["candidate_type"], "status": row["lifecycle_status"], "origin": row["origin"], "sourceDocumentId": row["source_document_id"], "documentIssueDate": row["document_issue_date"], "payload": json.loads(row["payload_json"]), "sourceReference": row["source_reference"], "evidenceSnippet": row["evidence_snippet"], "extractionId": row["extraction_id"], "parserVersion": row["parser_version"], "confidence": row["confidence"], "validationStatus": row["validation_status"], "documentImportedAt": row["imported_at"], "createdAt": row["created_at"], "confirmedAt": row["confirmed_at"]}

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
            groups.setdefault((account, isin, position_date), []).append((candidate, payload, nominal))

        conflicts: dict[str, Mapping[str, object]] = {}
        for (account, isin, position_date), observations in groups.items():
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

    def _sheet_metadata(self, sheet: str, grid: Mapping[int, Mapping[int, str]]) -> Mapping[str, object]:
        investor: str | None = None
        issue_date: str | None = None
        references: list[str] = []
        for row_number, row in grid.items():
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
        for row_number, row in grid.items():
            columns: dict[str, int] = {}
            for column, value in row.items():
                normalized = self._field_key(value)
                if normalized == "account": columns.setdefault("account", column)
                elif normalized in {"subfund", "fund", "sub_fund"}: columns.setdefault("subfund", column)
                elif normalized in {"security_isin", "isin"}: columns.setdefault("isin", column)
                elif normalized in {"nominal_amount", "quantity", "holding", "holding_amount"}: columns.setdefault("quantity", column)
                elif normalized in {"as_of_date", "asof_date", "position_date", "statement_date"}: columns.setdefault("position_date", column)
            if {"account", "subfund", "isin", "quantity", "position_date"} <= set(columns):
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
        return self._labelled_date(self._document_text(connection, str(context["extraction_id"])), "issue date")

    @staticmethod
    def _pdf_references(page_number: int, blocks: Sequence[sqlite3.Row]) -> str:
        return "; ".join(f"page {page_number} block {block['block_index']}" for block in blocks[:8])

    @classmethod
    def _labelled_date(cls, text: str, label: str) -> str | None:
        match = re.search(rf"{re.escape(label)}\s*:\s*([^\n.]+)", text, re.IGNORECASE)
        return cls._parse_date(match.group(1)) if match else None

    @classmethod
    def _parse_date(cls, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        match = _ISO_DATE.search(value)
        if match:
            try: return date.fromisoformat(match.group(1)).isoformat()
            except ValueError: return None
        text = _TEXT_DATE.search(value)
        if text:
            try: return datetime.strptime(text.group(1).title(), "%d %B %Y").date().isoformat()
            except ValueError: return None
        return None

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
        text, values = cls._normalise_text(evidence), [issue_date]
        for key, value in payload.items():
            if value is None or (kind == "holding_snapshot" and key == "investor" and resolved_account):
                continue
            values.append(value)
        return all(cls._normalise_text(str(value)) in text or cls._date_in_text(str(value), text) for value in values)

    @staticmethod
    def _normalise_text(value: str) -> str:
        return " ".join(value.casefold().replace(",", "").split())

    @classmethod
    def _date_in_text(cls, value: str, text: str) -> bool:
        if not cls._date(value): return False
        parsed = date.fromisoformat(value)
        return f"{parsed.day} {parsed.strftime('%B')} {parsed.year}".casefold() in text

    def _validate(self, kind: str, payload: Mapping[str, object]) -> None:
        fields = {"holding_snapshot": ("investor", "subfund", "account", "isin", "quantity", "positionDate"), "securities_transfer": ("investor", "fromSubfund", "fromAccount", "toSubfund", "toAccount", "isin", "quantity", "effectiveDate"), "claim_assignment": ("claimReference", "fromEntity", "toEntity", "isin", "effectiveDate")}[kind]
        allowed = set(fields) | ({"percentage"} if kind == "claim_assignment" else set())
        if set(payload) - allowed or not set(fields) <= set(payload):
            raise ReconciliationError("INVALID_PAYLOAD", "Candidate business fields are incomplete.")
        for field in fields:
            if kind == "holding_snapshot" and field == "investor" and payload[field] is None: continue
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
