"""Evidence-backed, confirmation-gated local pro-rata billing calculations.

This service deliberately creates calculation records, never invoices.  It
uses approved rule inputs where supplied and keeps any conflicting holdings
observations visible rather than selecting a winner from import order.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Mapping, Sequence

from ..storage.service import StorageService


_CENT = Decimal("0.01")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_PDF_REFERENCE = re.compile(r"^page\s+\d+\s+block\s+\d+$", re.IGNORECASE)
_ISSUED_INVOICE_STATUSES = ("issued", "partially_paid", "paid")
_CREDIT_ADJUSTMENTS = frozenset({"credit", "credit_note"})
_DEBIT_ADJUSTMENTS = frozenset({"debit", "debit_note"})


class BillingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BillingService:
    """Local billing-rule lifecycle and immutable calculation-run service."""

    def __init__(self, storage: StorageService):
        if storage.database is None:
            raise BillingError("STORAGE_UNAVAILABLE", "Local storage is not ready.")
        self._database = storage.database

    # Rule candidates -----------------------------------------------------

    def generate(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"documentId"} or not isinstance(params["documentId"], str):
            raise BillingError("INVALID_DOCUMENT", "Provide one document id.")
        document_id = params["documentId"]
        with self._database.transaction() as connection:
            sources = self._current_pdf_sources(connection, document_id)
            if not sources:
                return {"documentId": document_id, "generated": 0, "candidates": []}
            source = sources[0]
            parsed = self._parse_instruction("\n".join(str(item["text"]) for item in sources))
            if parsed is None:
                return {"documentId": document_id, "generated": 0, "candidates": []}
            payload = parsed
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            row = connection.execute(
                "SELECT id FROM billing_rule_candidates WHERE source_document_id = ? AND source_reference = ? AND payload_sha256 = ?",
                (document_id, "; ".join(str(item["source_reference"]) for item in sources), digest),
            ).fetchone()
            now = self._now()
            candidate_id = str(row["id"]) if row else str(uuid.uuid4())
            if row is None:
                connection.execute(
                    "INSERT INTO billing_rule_candidates "
                    "(id,lifecycle_status,source_document_id,extraction_id,parser_version,source_reference,evidence_snippet,document_issue_date,origin,payload_json,payload_sha256,created_at,approved_at) "
                    "VALUES (?, 'candidate', ?, ?, ?, ?, ?, ?, 'deterministic', ?, ?, ?, NULL)",
                    (candidate_id, document_id, source["extraction_id"], source["parser_version"], "; ".join(str(item["source_reference"]) for item in sources),
                     self._snippet("\n".join(str(item["text"]) for item in sources)), payload["documentIssueDate"], payload_json, digest, now),
                )
                self._audit(connection, "billing_rule_candidate_staged", "billing_rule_candidate", candidate_id, "system", now, {
                    "source_document_id": document_id, "extraction_id": source["extraction_id"],
                })
        return {"documentId": document_id, "generated": 1, "candidates": [self.candidate({"candidateId": candidate_id})]}

    def candidate(self, params: object) -> Mapping[str, object]:
        candidate_id = self._one_id(params, "candidateId")
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT c.*, d.imported_at FROM billing_rule_candidates c JOIN source_documents d ON d.id = c.source_document_id WHERE c.id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise BillingError("RULE_CANDIDATE_NOT_FOUND", "Billing-rule candidate was not found.")
        return self._candidate_result(row)

    def candidates(self, params: object) -> Mapping[str, object]:
        """List bounded local billing-rule candidates for evidence-led review."""
        if not isinstance(params, dict) or set(params) - {"status"}:
            raise BillingError("INVALID_BILLING_RULE", "Billing-rule filters are invalid.")
        status = params.get("status")
        if status is not None and status not in {"candidate", "approved"}:
            raise BillingError("INVALID_BILLING_RULE", "Billing-rule status is invalid.")
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT c.*, d.imported_at, a.id AS billing_agreement_id "
                "FROM billing_rule_candidates c JOIN source_documents d ON d.id = c.source_document_id "
                "LEFT JOIN billing_agreements a ON a.billing_rule_candidate_id = c.id "
                "AND a.source_document_id = c.source_document_id AND a.approval_status = 'approved' "
                + ("WHERE c.lifecycle_status = ? " if status else "")
                + "ORDER BY CASE c.lifecycle_status WHEN 'candidate' THEN 0 ELSE 1 END, c.created_at, c.id LIMIT 200",
                (status,) if status else (),
            ).fetchall()
        return {"candidates": [self._candidate_result(row) for row in rows], "truncated": len(rows) == 200}

    def approve_rule(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"candidateId", "acknowledged"} or params.get("acknowledged") is not True:
            raise BillingError("CONFIRMATION_REQUIRED", "Explicit billing-rule confirmation is required.")
        candidate_id = params.get("candidateId")
        if not isinstance(candidate_id, str):
            raise BillingError("CONFIRMATION_REQUIRED", "Explicit billing-rule confirmation is required.")
        now = self._now()
        with self._database.transaction() as connection:
            candidate = connection.execute("SELECT * FROM billing_rule_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise BillingError("RULE_CANDIDATE_NOT_FOUND", "Billing-rule candidate was not found.")
            if not self._source_reference_exists(connection, str(candidate["extraction_id"]), str(candidate["source_reference"])):
                raise BillingError("INVALID_EVIDENCE", "Billing-rule evidence is no longer available locally.")
            payload = self._payload(candidate)
            if candidate["lifecycle_status"] == "approved":
                agreement = connection.execute("SELECT id FROM billing_agreements WHERE billing_rule_candidate_id = ?", (candidate_id,)).fetchone()
                if agreement is None:
                    raise BillingError("RULE_INTEGRITY", "The approved billing rule is incomplete.")
                return self.rule_details({"billingAgreementId": str(agreement["id"])})
            agreement_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO billing_agreements "
                "(id,invoice_recipient_id,agreement_reference,effective_from,effective_to,terms_reference,created_at,billing_rule_candidate_id,source_document_id,source_reference,rule_json,approval_status,approved_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?)",
                (agreement_id, f"rule:{candidate_id}", payload["periodStart"], payload["periodEnd"],
                 str(candidate["source_reference"]), now, candidate_id, candidate["source_document_id"], candidate["source_reference"],
                 candidate["payload_json"], now),
            )
            connection.execute("UPDATE billing_rule_candidates SET lifecycle_status = 'approved', approved_at = ? WHERE id = ?", (now, candidate_id))
            self._evidence(connection, str(candidate["source_document_id"]), "billing_agreement", agreement_id,
                           str(candidate["source_reference"]), str(candidate["evidence_snippet"]), now)
            self._audit(connection, "billing_rule_approved", "billing_agreement", agreement_id, "user", now, {"candidate_id": candidate_id})
        return self.rule_details({"billingAgreementId": agreement_id})

    def rule_details(self, params: object) -> Mapping[str, object]:
        agreement_id = self._one_id(params, "billingAgreementId")
        with self._database.connection() as connection:
            row = connection.execute("SELECT * FROM billing_agreements WHERE id = ?", (agreement_id,)).fetchone()
            if row is None:
                raise BillingError("BILLING_RULE_NOT_FOUND", "Approved billing rule was not found.")
            evidence = self._evidence_rows(connection, "billing_agreement", agreement_id)
        if row["approval_status"] != "approved" or not row["rule_json"]:
            raise BillingError("RULE_NOT_APPROVED", "Billing rule is not approved for calculation.")
        return {
            "id": str(row["id"]), "status": str(row["approval_status"]), "rule": self._json(str(row["rule_json"])),
            "sourceDocumentId": row["source_document_id"], "sourceReference": row["source_reference"],
            "approvedAt": row["approved_at"], "invoiceRecipientStatus": "unresolved" if row["invoice_recipient_id"] is None else "specified",
            "evidence": evidence,
        }

    # Calculation ---------------------------------------------------------

    def preview(self, params: object) -> Mapping[str, object]:
        agreement_id, as_of_date = self._calculation_request(params)
        with self._database.connection() as connection:
            return self._calculation(connection, agreement_id, as_of_date)

    def invoicing(self, params: object) -> Mapping[str, object]:
        """Read verified issued-invoice information beside a calculation.

        This method does not create invoices or infer them from documents,
        draft records, calculations, or AI output.
        """
        agreement_id, as_of_date = self._calculation_request(params)
        with self._database.connection() as connection:
            calculation = self._calculation(connection, agreement_id, as_of_date)
        return {
            "billingAgreementId": agreement_id,
            "billingPeriod": calculation["billingPeriod"],
            "currency": calculation["currency"],
            "allocations": calculation["allocations"],
            "invoicing": calculation["invoicing"],
        }

    def calculate(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) not in ({"billingAgreementId", "acknowledged"}, {"billingAgreementId", "asOfDate", "acknowledged"}) or params.get("acknowledged") is not True:
            raise BillingError("CONFIRMATION_REQUIRED", "Explicit acknowledgement is required to save a calculation run.")
        agreement_id, as_of_date = self._calculation_request({key: value for key, value in params.items() if key != "acknowledged"})
        now = self._now()
        with self._database.transaction() as connection:
            calculation = self._calculation(connection, agreement_id, as_of_date)
            fingerprint = self._fingerprint(calculation["inputs"])
            existing = connection.execute(
                "SELECT id FROM billing_calculation_runs WHERE billing_agreement_id = ? AND input_fingerprint = ?",
                (agreement_id, fingerprint),
            ).fetchone()
            if existing is not None:
                return self.run({"runId": str(existing["id"])})
            previous = connection.execute(
                "SELECT id, run_version FROM billing_calculation_runs WHERE billing_agreement_id = ? ORDER BY run_version DESC LIMIT 1",
                (agreement_id,),
            ).fetchone()
            run_id = str(uuid.uuid4())
            version = (int(previous["run_version"]) + 1) if previous else 1
            connection.execute(
                "INSERT INTO billing_calculation_runs "
                "(id,billing_agreement_id,previous_run_id,run_version,status,allocation_date,currency_code,fee_pool_decimal,total_eligible_decimal,rounding_method,input_fingerprint,inputs_json,related_issues_json,created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, agreement_id, previous["id"] if previous else None, version, calculation["status"], calculation["allocationDate"],
                 calculation["currency"], calculation["feePool"], calculation["totalEligibleAmount"], calculation["roundingMethod"], fingerprint,
                 json.dumps(calculation["inputs"], sort_keys=True), json.dumps(calculation["relatedIssues"], sort_keys=True), now),
            )
            for allocation in calculation["allocations"]:
                connection.execute(
                    "INSERT INTO billing_calculation_allocations "
                    "(id,calculation_run_id,participant_key,participant_label,subfund_reference,eligible_basis_decimal,share_decimal,allocated_fee_decimal,currency_code,eligibility_status,eligibility_reason,holdings_record_ids_json,evidence_references_json,invoice_recipient_status,created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved', ?)",
                    (str(uuid.uuid4()), run_id, allocation["participantKey"], allocation["participantLabel"], allocation.get("subfundReference"),
                     allocation.get("eligibleBasis"), allocation.get("share"), allocation.get("allocatedFee"), calculation["currency"],
                     allocation["eligibilityStatus"], allocation.get("eligibilityReason"), json.dumps(allocation["holdingRecordIds"]),
                     json.dumps(allocation["evidence"]), now),
                )
            rule = connection.execute("SELECT source_document_id, source_reference FROM billing_agreements WHERE id = ?", (agreement_id,)).fetchone()
            if rule is not None:
                self._evidence(connection, str(rule["source_document_id"]), "billing_calculation_run", run_id, str(rule["source_reference"]), "Approved billing-rule input", now)
            self._audit(connection, "billing_calculation_saved", "billing_calculation_run", run_id, "user", now, {"agreement_id": agreement_id, "status": calculation["status"], "version": version})
        return self.run({"runId": run_id})

    def runs(self, params: object) -> Mapping[str, object]:
        agreement_id = self._one_id(params, "billingAgreementId")
        with self._database.connection() as connection:
            rows = connection.execute("SELECT id FROM billing_calculation_runs WHERE billing_agreement_id = ? ORDER BY run_version", (agreement_id,)).fetchall()
        return {"billingAgreementId": agreement_id, "runs": [self.run({"runId": str(row["id"])}) for row in rows]}

    def run(self, params: object) -> Mapping[str, object]:
        run_id = self._one_id(params, "runId")
        with self._database.connection() as connection:
            row = connection.execute("SELECT * FROM billing_calculation_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise BillingError("CALCULATION_NOT_FOUND", "Billing calculation run was not found.")
            allocations = connection.execute("SELECT * FROM billing_calculation_allocations WHERE calculation_run_id = ? ORDER BY participant_key", (run_id,)).fetchall()
            evidence = self._evidence_rows(connection, "billing_calculation_run", run_id)
        return self._run_result(row, allocations, evidence)

    def compare_runs(self, params: object) -> Mapping[str, object]:
        if not isinstance(params, dict) or set(params) != {"leftRunId", "rightRunId"}:
            raise BillingError("INVALID_CALCULATION", "Provide two calculation run ids.")
        left, right = self.run({"runId": params["leftRunId"]}), self.run({"runId": params["rightRunId"]})
        if left["billingAgreementId"] != right["billingAgreementId"]:
            raise BillingError("INVALID_CALCULATION", "Only versions of the same billing rule can be compared.")
        left_amounts = {row["participantKey"]: row.get("allocatedFee") for row in left["allocations"]}
        right_amounts = {row["participantKey"]: row.get("allocatedFee") for row in right["allocations"]}
        return {"leftRunId": left["id"], "rightRunId": right["id"], "billingAgreementId": left["billingAgreementId"],
                "changes": [{"participantKey": key, "before": left_amounts.get(key), "after": right_amounts.get(key)}
                            for key in sorted(set(left_amounts) | set(right_amounts)) if left_amounts.get(key) != right_amounts.get(key)]}

    # Internal calculation ------------------------------------------------

    def _calculation(self, connection: sqlite3.Connection, agreement_id: str, as_of_date: str | None) -> Mapping[str, object]:
        agreement = connection.execute("SELECT * FROM billing_agreements WHERE id = ?", (agreement_id,)).fetchone()
        if agreement is None:
            raise BillingError("BILLING_RULE_NOT_FOUND", "Approved billing rule was not found.")
        if agreement["approval_status"] != "approved" or not agreement["rule_json"]:
            raise BillingError("RULE_NOT_APPROVED", "Billing rule is not approved for calculation.")
        rule = self._json(str(agreement["rule_json"]))
        if rule.get("allocationDateRequired") and not (as_of_date or rule.get("allocationDate")):
            raise BillingError("MISSING_ALLOCATION_DATE", "The approved billing rule requires an allocation date.")
        selected_date = as_of_date or rule.get("allocationDate") or rule.get("periodEnd")
        if selected_date is not None:
            self._date(str(selected_date))
        fee_pool = self._decimal(rule.get("feePool"), "The approved fee pool is invalid.")
        currency = rule.get("currency")
        participants = rule.get("participants")
        if fee_pool <= 0 or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency) or not isinstance(participants, list) or not participants:
            raise BillingError("INVALID_BILLING_RULE", "The approved billing rule is incomplete.")
        allocations: list[dict[str, object]] = []
        eligible: list[tuple[dict[str, object], Decimal]] = []
        related_issues: list[Mapping[str, object]] = []
        blocking_reasons: list[str] = []
        for raw in participants:
            if not isinstance(raw, dict):
                raise BillingError("INVALID_BILLING_RULE", "The approved billing participant is invalid.")
            key, label, status = raw.get("key"), raw.get("label"), raw.get("status")
            if not isinstance(key, str) or not isinstance(label, str) or status not in {"eligible", "excluded"}:
                raise BillingError("INVALID_BILLING_RULE", "The approved billing participant is invalid.")
            holding_ids, issues = self._participant_records(connection, str(raw.get("name", label)), str(raw.get("subfundReference", "")), selected_date)
            related_issues.extend(issues)
            allocation: dict[str, object] = {
                "participantKey": key, "participantLabel": label, "subfundReference": raw.get("subfundReference"),
                "eligibilityStatus": status, "eligibilityReason": raw.get("reason"), "holdingRecordIds": holding_ids,
                "currency": currency,
                "evidence": [{"documentId": agreement["source_document_id"], "sourceReference": agreement["source_reference"]}],
            }
            if status == "eligible":
                basis = self._decimal(raw.get("eligibleBasis"), "An approved eligible basis is invalid.")
                if basis <= 0:
                    raise BillingError("ZERO_ELIGIBLE_NOMINAL", "Eligible nominal amounts must be positive.")
                if not holding_ids:
                    blocking_reasons.append(f"No confirmed holdings record is linked to eligible participant {label} at the requested date.")
                allocation["eligibleBasis"] = self._string(basis)
                eligible.append((allocation, basis))
            allocations.append(allocation)
        total = sum((amount for _allocation, amount in eligible), Decimal("0"))
        if total <= 0:
            raise BillingError("ZERO_ELIGIBLE_NOMINAL", "No positive eligible nominal amount is available.")
        pool_cents = int((fee_pool / _CENT).to_integral_exact())
        raw_cents = [(allocation, Decimal(pool_cents) * amount / total) for allocation, amount in eligible]
        cents = {str(allocation["participantKey"]): int(value.to_integral_value(rounding=ROUND_DOWN)) for allocation, value in raw_cents}
        residual = pool_cents - sum(cents.values())
        for allocation, value in sorted(raw_cents, key=lambda item: (-(item[1] - item[1].to_integral_value(rounding=ROUND_DOWN)), str(item[0]["participantKey"])))[:residual]:
            cents[str(allocation["participantKey"])] += 1
        for allocation, amount in eligible:
            allocation["share"] = self._string(amount / total, 8)
            allocation["allocatedFee"] = self._string(Decimal(cents[str(allocation["participantKey"])]) * _CENT, 2)
        for allocation in allocations:
            if allocation["eligibilityStatus"] == "excluded":
                allocation["eligibleBasis"] = None
                allocation["share"] = None
                allocation["allocatedFee"] = None
        unique_issues = {str(item["id"]): item for item in related_issues}
        status = "blocked" if blocking_reasons else ("provisional" if unique_issues else "approved_calculation")
        inputs = {"billingAgreementId": agreement_id, "ruleCandidateId": agreement["billing_rule_candidate_id"], "allocationDate": selected_date,
                  "feePool": self._string(fee_pool, 2), "currency": currency, "participants": allocations,
                  "relatedIssueIds": sorted(unique_issues), "blockingReasons": blocking_reasons}
        invoicing = self._invoicing_summary(connection, agreement_id, allocations, currency)
        for allocation in allocations:
            allocation.update(invoicing["byParticipant"].get(str(allocation["participantKey"]), {}))
        return {"billingAgreementId": agreement_id, "status": status, "allocationDate": selected_date,
                "feePool": self._string(fee_pool, 2), "currency": currency, "totalEligibleAmount": self._string(total),
                "allocatedTotal": self._string(sum((Decimal(str(row["allocatedFee"])) for row in allocations if row.get("allocatedFee")), Decimal("0")), 2),
                "billingPeriod": {"start": agreement["effective_from"], "end": agreement["effective_to"]},
                "roundingMethod": "largest_remainder_cent; fractional remainder descending, participant key ascending", "allocations": allocations,
                "invoicing": invoicing["summary"],
                "relatedIssues": list(unique_issues.values()), "limitations": [
                    "This is a calculation record, not an issued invoice.",
                    "Invoice recipients remain unresolved unless separately documented.",
                    *blocking_reasons,
                    *( ["An approved explicit billing basis was used while the listed holdings discrepancy remains unresolved."] if unique_issues else [] ),
                ], "inputs": inputs}

    def _invoicing_summary(
        self, connection: sqlite3.Connection, agreement_id: str, allocations: Sequence[Mapping[str, object]], currency: str,
    ) -> Mapping[str, object]:
        """Return only provenance-backed issued amounts; never turn a calculation into an invoice."""
        status_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM invoices WHERE billing_agreement_id = ? GROUP BY status",
            (agreement_id,),
        ).fetchall()
        status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
        issued_count = sum(status_counts.get(status, 0) for status in _ISSUED_INVOICE_STATUSES)
        draft_count = status_counts.get("draft", 0)
        rows = connection.execute(
            "SELECT l.id AS line_id, l.invoice_id, l.amount_decimal, l.currency_code, "
            "l.billing_calculation_allocation_id, i.status, i.issued_at, "
            "recipient.display_name AS recipient_name "
            "FROM invoice_line_items l JOIN invoices i ON i.id = l.invoice_id "
            "LEFT JOIN invoice_recipients r ON r.id = i.invoice_recipient_id "
            "LEFT JOIN legal_entities recipient ON recipient.id = r.legal_entity_id "
            "WHERE i.billing_agreement_id = ? AND i.status IN ('issued', 'partially_paid', 'paid') "
            "ORDER BY i.issued_at, i.id, l.id",
            (agreement_id,),
        ).fetchall()
        allocation_ids_by_participant: dict[str, list[str]] = {}
        for allocation_row in connection.execute(
            "SELECT a.id, a.participant_key FROM billing_calculation_allocations a "
            "JOIN billing_calculation_runs r ON r.id = a.calculation_run_id "
            "WHERE r.billing_agreement_id = ?",
            (agreement_id,),
        ):
            allocation_ids_by_participant.setdefault(str(allocation_row["participant_key"]), []).append(
                str(allocation_row["id"])
            )
        known_allocation_ids = {
            allocation_id
            for allocation_ids in allocation_ids_by_participant.values()
            for allocation_id in allocation_ids
        }
        draft_allocation_ids = {
            str(row["billing_calculation_allocation_id"])
            for row in connection.execute(
                "SELECT l.billing_calculation_allocation_id FROM invoice_line_items l "
                "JOIN invoices i ON i.id = l.invoice_id "
                "WHERE i.billing_agreement_id = ? AND i.status = 'draft' "
                "AND l.billing_calculation_allocation_id IS NOT NULL",
                (agreement_id,),
            )
        }
        line_counts = {
            str(row["invoice_id"]): int(row["count"])
            for row in connection.execute(
                "SELECT invoice_id, COUNT(*) AS count FROM invoice_line_items "
                "WHERE invoice_id IN (SELECT id FROM invoices WHERE billing_agreement_id = ? "
                "AND status IN ('issued', 'partially_paid', 'paid')) GROUP BY invoice_id",
                (agreement_id,),
            )
        }
        by_allocation: dict[str, list[sqlite3.Row]] = {}
        unallocated_issued = 0
        for row in rows:
            allocation_id = row["billing_calculation_allocation_id"]
            if allocation_id is None or str(allocation_id) not in known_allocation_ids:
                unallocated_issued += 1
                continue
            by_allocation.setdefault(str(allocation_id), []).append(row)

        result: dict[str, Mapping[str, object]] = {}
        limitations: list[str] = []
        for allocation in allocations:
            key = str(allocation["participantKey"])
            matching_ids = allocation_ids_by_participant.get(key, [])
            participant_rows = [line for allocation_id in matching_ids for line in by_allocation.get(allocation_id, [])]
            result[key] = self._participant_invoice_summary(
                connection, participant_rows, line_counts, issued_count,
                any(allocation_id in draft_allocation_ids for allocation_id in matching_ids), currency, allocation,
            )
            if result[key]["issuedInvoiceStatus"] in {"issued_unmapped", "insufficient_evidence", "currency_mismatch"}:
                limitations.append(f"Issued-invoice comparison for {allocation['participantLabel']} is incomplete.")
        if unallocated_issued:
            limitations.append(f"{unallocated_issued} issued invoice line(s) are not linked to a calculated participant.")
        return {
            "byParticipant": result,
            "summary": {
                "issuedInvoiceCount": issued_count,
                "draftInvoiceCount": draft_count,
                "unmappedIssuedInvoiceLineCount": unallocated_issued,
                "limitations": limitations,
            },
        }

    def _participant_invoice_summary(
        self, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row], line_counts: Mapping[str, int],
        issued_count: int, has_draft: bool, calculation_currency: str, allocation: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not rows:
            if issued_count == 0:
                status = "draft_only" if has_draft else "no_issued_invoice_recorded"
                return {
                    "issuedInvoiceStatus": status, "verifiedInvoicedAmount": None,
                    "allocationInvoiceDifference": None, "outstandingBalance": None,
                    "outstandingStatus": "not_available", "invoiceRecipient": None, "invoiceEvidence": [],
                }
            return {
                "issuedInvoiceStatus": "issued_unmapped", "verifiedInvoicedAmount": None,
                "allocationInvoiceDifference": None, "outstandingBalance": None,
                "outstandingStatus": "not_available", "invoiceRecipient": None, "invoiceEvidence": [],
            }
        evidence: list[Mapping[str, object]] = []
        comparable: list[tuple[sqlite3.Row, Decimal]] = []
        recipient_names: set[str] = set()
        unsupported_currency = False
        for row in rows:
            row_evidence = self._target_evidence(connection, "invoice_line_item", str(row["line_id"]))
            if not row_evidence:
                row_evidence = self._target_evidence(connection, "invoice", str(row["invoice_id"]))
            if not row_evidence:
                continue
            evidence.extend(row_evidence)
            if row["recipient_name"] is not None:
                recipient_names.add(str(row["recipient_name"]))
            if str(row["currency_code"]) != calculation_currency:
                unsupported_currency = True
                continue
            try:
                comparable.append((row, self._decimal(row["amount_decimal"], "A verified invoice amount is invalid.")))
            except BillingError:
                unsupported_currency = True
        if unsupported_currency:
            return {
                "issuedInvoiceStatus": "currency_mismatch" if evidence else "insufficient_evidence",
                "verifiedInvoicedAmount": None, "allocationInvoiceDifference": None,
                "outstandingBalance": None, "outstandingStatus": "not_available",
                "invoiceRecipient": self._recipient_label(recipient_names), "invoiceEvidence": evidence,
            }
        if not comparable:
            return {
                "issuedInvoiceStatus": "currency_mismatch" if evidence else "insufficient_evidence",
                "verifiedInvoicedAmount": None, "allocationInvoiceDifference": None,
                "outstandingBalance": None, "outstandingStatus": "not_available",
                "invoiceRecipient": self._recipient_label(recipient_names), "invoiceEvidence": evidence,
            }
        invoiced = sum((amount for _row, amount in comparable), Decimal("0"))
        allocated = allocation.get("allocatedFee")
        difference = None if allocated is None else self._string(self._decimal(allocated, "Calculated allocation is invalid.") - invoiced, 2)
        outstanding, outstanding_status = self._outstanding_for_lines(connection, comparable, line_counts, calculation_currency)
        return {
            "issuedInvoiceStatus": "verified_issued",
            "verifiedInvoicedAmount": self._string(invoiced, 2),
            "allocationInvoiceDifference": difference,
            "outstandingBalance": outstanding,
            "outstandingStatus": outstanding_status,
            "invoiceRecipient": self._recipient_label(recipient_names),
            "invoiceEvidence": evidence,
        }

    def _outstanding_for_lines(
        self, connection: sqlite3.Connection, lines: Sequence[tuple[sqlite3.Row, Decimal]], line_counts: Mapping[str, int], currency: str,
    ) -> tuple[str | None, str]:
        if any(line_counts.get(str(row["invoice_id"]), 0) != 1 for row, _amount in lines):
            return None, "unresolved_multi_line_invoice"
        total = sum((amount for _row, amount in lines), Decimal("0"))
        for row, _amount in lines:
            invoice_id = str(row["invoice_id"])
            payments = connection.execute("SELECT id, amount_decimal, currency_code FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchall()
            adjustments = connection.execute("SELECT id, adjustment_type, amount_decimal, currency_code FROM financial_adjustments WHERE invoice_id = ?", (invoice_id,)).fetchall()
            # An absent payment row is not evidence that no payment was made.
            # Do not present the invoice amount itself as an outstanding balance
            # until a payment/adjustment reconciliation has been recorded.
            if not payments and not adjustments:
                return None, "payment_information_unavailable"
            for payment in payments:
                if str(payment["currency_code"]) != currency or not self._target_evidence(connection, "payment", str(payment["id"])):
                    return None, "insufficient_payment_evidence"
                total -= self._decimal(payment["amount_decimal"], "A verified payment amount is invalid.")
            for adjustment in adjustments:
                if str(adjustment["currency_code"]) != currency or not self._target_evidence(connection, "financial_adjustment", str(adjustment["id"])):
                    return None, "insufficient_adjustment_evidence"
                kind = str(adjustment["adjustment_type"]).casefold()
                amount = self._decimal(adjustment["amount_decimal"], "A verified adjustment amount is invalid.")
                if kind in _CREDIT_ADJUSTMENTS:
                    total -= amount
                elif kind in _DEBIT_ADJUSTMENTS:
                    total += amount
                else:
                    return None, "unsupported_adjustment_type"
        return self._string(total, 2), "verified"

    @staticmethod
    def _recipient_label(names: set[str]) -> str | None:
        return next(iter(names)) if len(names) == 1 else None

    def _participant_records(self, connection: sqlite3.Connection, name: str, reference: str, as_of_date: object) -> tuple[list[str], list[Mapping[str, object]]]:
        # Match only a documented vehicle name, not a loose inference from a
        # claimant or assignment.  The billing rule's own explicit basis still
        # controls the allocation when a linked holding is disputed.
        rows = connection.execute(
            "SELECT h.id, h.position_date, h.quantity, h.source_document_id, v.name FROM holding_positions h "
            "JOIN accounts a ON a.id = h.account_id JOIN investment_vehicles v ON v.id = a.investment_vehicle_id "
            "WHERE v.name = ? AND (? IS NULL OR h.position_date <= ?) ORDER BY h.position_date DESC, h.created_at DESC",
            (name, as_of_date, as_of_date),
        ).fetchall()
        record_ids = [str(row["id"]) for row in rows[:1]]
        issues: list[Mapping[str, object]] = []
        candidates = connection.execute(
            "SELECT i.id, i.reason, i.source_document_id, c.source_reference, c.payload_json FROM issues i "
            "JOIN reconciliation_candidates c ON c.id = i.affected_id "
            "WHERE i.status = 'open' AND i.affected_type = 'reconciliation_candidate'"
        ).fetchall()
        for item in candidates:
            payload = self._json(str(item["payload_json"]))
            if payload.get("subfund") == name or (reference and reference in str(payload.get("subfund", ""))):
                issues.append({"id": str(item["id"]), "reason": str(item["reason"]), "sourceDocumentId": str(item["source_document_id"]), "sourceReference": str(item["source_reference"])})
        conflicts = connection.execute(
            "SELECT DISTINCT i.id, i.reason, link.source_document_id, link.source_reference, candidate.payload_json "
            "FROM issues i JOIN reconciliation_holding_conflicts conflict ON conflict.id = i.affected_id "
            "JOIN reconciliation_holding_conflict_candidates link ON link.conflict_id = conflict.id "
            "JOIN reconciliation_candidates candidate ON candidate.id = link.candidate_id "
            "WHERE i.status = 'open' AND i.affected_type = 'reconciliation_holding_conflict' "
            "AND conflict.status = 'open'"
        ).fetchall()
        for item in conflicts:
            payload = self._json(str(item["payload_json"]))
            if payload.get("subfund") == name or (reference and reference in str(payload.get("subfund", ""))):
                issues.append({"id": str(item["id"]), "reason": str(item["reason"]), "sourceDocumentId": str(item["source_document_id"]), "sourceReference": str(item["source_reference"])})
        return record_ids, issues

    # Parsing and serialization ------------------------------------------

    def _current_pdf_sources(self, connection: sqlite3.Connection, document_id: str) -> list[sqlite3.Row]:
        extraction = connection.execute(
            "SELECT e.id FROM source_documents d JOIN document_extractions e ON e.source_document_id = d.id "
            "WHERE d.id = ? AND e.status = 'parsed' ORDER BY e.completed_at DESC, e.id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        if extraction is None:
            return []
        return connection.execute(
            "SELECT e.id AS extraction_id, e.parser_version, p.page_number, b.block_index, "
            "'page ' || p.page_number || ' block ' || b.block_index AS source_reference, b.text "
            "FROM source_documents d JOIN document_extractions e ON e.source_document_id = d.id "
            "JOIN pdf_page_extractions p ON p.extraction_id = e.id JOIN pdf_text_blocks b ON b.pdf_page_extraction_id = p.id "
            "WHERE d.id = ? AND e.id = ? AND b.text <> '' ORDER BY p.page_number, b.block_index",
            (document_id, extraction["id"]),
        ).fetchall()

    @staticmethod
    def _parse_instruction(text: str) -> dict[str, object] | None:
        compact = " ".join(text.split())
        if not all(token in compact.casefold() for token in ("approved", "fee pool", "allocation rule", "eligible participant", "eligible nominal amount / total eligible nominal amount")):
            return None
        pool = re.search(r"Fee\s+pool\s+(?:\n\s*)?([A-Z]{3})\s+([0-9][0-9,]*(?:\.\d{1,2})?)", text, re.IGNORECASE)
        period = re.search(r"Billing\s+period\s*\n\s*([^\n]+)", text, re.IGNORECASE)
        participant_rows = re.findall(r"Eligible\s+participant\s+\d+\s*\n\s*([^\n(]+?)\s*\(([^)]+)\)\s*:\s*([0-9][0-9,]*(?:\.\d+)?)", text, re.IGNORECASE)
        if pool is None or period is None or not participant_rows:
            return None
        dates = re.findall(r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b", period.group(1), re.IGNORECASE)
        issue = re.search(r"Issue\s+date\s*:\s*([^\n]+)", text, re.IGNORECASE)
        if len(dates) != 2 or issue is None:
            return None
        try:
            fee_pool = Decimal(pool.group(2).replace(",", ""))
            period_start, period_end, issue_date = (BillingService._date_text(value) for value in (*dates, issue.group(1).strip()))
        except (InvalidOperation, ValueError):
            return None
        if fee_pool <= 0:
            return None
        participants = []
        for index, (name, reference, amount) in enumerate(participant_rows, start=1):
            value = Decimal(amount.replace(",", ""))
            if value <= 0:
                return None
            participants.append({"key": f"{reference.strip()}-{index}", "name": name.strip() + f" ({reference.strip()})", "label": name.strip() + f" ({reference.strip()})", "subfundReference": reference.strip(), "eligibleBasis": BillingService._string(value), "status": "eligible", "reason": "explicit approved billing basis"})
        excluded = re.search(r"([^\n(]+?)\s*\(([^)]+)\)\s+is\s+excluded\s+because\s+([^\n.]+)", text, re.IGNORECASE)
        if excluded:
            participants.append({"key": f"{excluded.group(2).strip()}-excluded", "name": excluded.group(1).strip() + f" ({excluded.group(2).strip()})", "label": excluded.group(1).strip() + f" ({excluded.group(2).strip()})", "subfundReference": excluded.group(2).strip(), "status": "excluded", "reason": excluded.group(3).strip()})
        return {"documentIssueDate": issue_date, "periodStart": period_start, "periodEnd": period_end, "allocationDate": None,
                "allocationDateRequired": False, "allocationDateRule": "explicit approved eligible nominal amounts for the stated billing period",
                "feePool": BillingService._string(fee_pool, 2), "currency": pool.group(1), "methodology": "eligible nominal amount / total eligible nominal amount", "rounding": "currency rounding reconciles allocations to fee pool", "participants": participants}

    @staticmethod
    def _date_text(value: str) -> str:
        return datetime.strptime(value.strip(), "%d %B %Y").date().isoformat()

    @staticmethod
    def _date(value: str) -> None:
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise BillingError("INVALID_DATE", "Dates must use YYYY-MM-DD.") from error

    @staticmethod
    def _decimal(value: object, message: str) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise BillingError("INVALID_BILLING_RULE", message) from error

    @staticmethod
    def _string(value: Decimal, places: int | None = None) -> str:
        if places is not None:
            return format(value.quantize(Decimal(1).scaleb(-places)), f".{places}f")
        rendered = format(value, "f")
        return (rendered.rstrip("0").rstrip(".") if "." in rendered else rendered) or "0"

    def _calculation_request(self, params: object) -> tuple[str, str | None]:
        if not isinstance(params, dict) or set(params) not in ({"billingAgreementId"}, {"billingAgreementId", "asOfDate"}):
            raise BillingError("INVALID_CALCULATION", "Provide an approved billing agreement and optional as-of date.")
        agreement_id = params.get("billingAgreementId")
        day = params.get("asOfDate")
        if not isinstance(agreement_id, str) or not agreement_id or (day is not None and (not isinstance(day, str) or not day)):
            raise BillingError("INVALID_CALCULATION", "Billing calculation fields are invalid.")
        if isinstance(day, str):
            self._date(day)
        return agreement_id, day

    @staticmethod
    def _one_id(params: object, key: str) -> str:
        if not isinstance(params, dict) or set(params) != {key} or not isinstance(params.get(key), str) or not params[key]:
            raise BillingError("INVALID_REQUEST", "Request identifier is invalid.")
        return params[key]

    @staticmethod
    def _json(value: str) -> dict[str, object]:
        try:
            result = json.loads(value)
        except json.JSONDecodeError as error:
            raise BillingError("RULE_INTEGRITY", "Stored billing data is invalid.") from error
        if not isinstance(result, dict):
            raise BillingError("RULE_INTEGRITY", "Stored billing data is invalid.")
        return result

    @staticmethod
    def _snippet(text: str) -> str:
        return " ".join(text.split())[:500]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _fingerprint(inputs: object) -> str:
        return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _payload(row: sqlite3.Row) -> dict[str, object]:
        return BillingService._json(str(row["payload_json"]))

    @staticmethod
    def _source_reference_exists(connection: sqlite3.Connection, extraction_id: str, reference: str) -> bool:
        references = [item.strip() for item in reference.split(";") if item.strip()]
        if not references:
            return False
        for item in references:
            if not _PDF_REFERENCE.fullmatch(item):
                return False
            page, block = (int(value) for value in re.findall(r"\d+", item))
            if connection.execute("SELECT 1 FROM pdf_text_blocks b JOIN pdf_page_extractions p ON p.id = b.pdf_page_extraction_id WHERE p.extraction_id = ? AND p.page_number = ? AND b.block_index = ?", (extraction_id, page, block)).fetchone() is None:
                return False
        return True

    @staticmethod
    def _evidence(connection: sqlite3.Connection, document_id: str, target_type: str, target_id: str, source_location: str, snippet: str, now: str) -> None:
        if connection.execute("SELECT 1 FROM evidence_references WHERE source_document_id = ? AND target_type = ? AND target_id = ? AND source_location = ?", (document_id, target_type, target_id, source_location)).fetchone() is None:
            connection.execute("INSERT INTO evidence_references VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), document_id, target_type, target_id, source_location, snippet, now))

    @staticmethod
    def _evidence_rows(connection: sqlite3.Connection, target_type: str, target_id: str) -> list[Mapping[str, object]]:
        return [{"documentId": row["source_document_id"], "sourceReference": row["source_location"], "snippet": row["description"]} for row in connection.execute("SELECT source_document_id, source_location, description FROM evidence_references WHERE target_type = ? AND target_id = ? ORDER BY id", (target_type, target_id))]

    @staticmethod
    def _target_evidence(connection: sqlite3.Connection, target_type: str, target_id: str) -> list[Mapping[str, object]]:
        return BillingService._evidence_rows(connection, target_type, target_id)

    @staticmethod
    def _audit(connection: sqlite3.Connection, event: str, target_type: str, target_id: str, actor: str, now: str, details: Mapping[str, object]) -> None:
        connection.execute("INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), event, target_type, target_id, actor, now, json.dumps(details, sort_keys=True)))

    def _candidate_result(self, row: sqlite3.Row) -> Mapping[str, object]:
        agreement_id = row["billing_agreement_id"] if "billing_agreement_id" in row.keys() else None
        return {"id": row["id"], "billingAgreementId": agreement_id, "status": row["lifecycle_status"], "sourceDocumentId": row["source_document_id"], "extractionId": row["extraction_id"], "parserVersion": row["parser_version"], "sourceReference": row["source_reference"], "documentIssueDate": row["document_issue_date"], "origin": row["origin"], "payload": self._payload(row), "createdAt": row["created_at"], "approvedAt": row["approved_at"], "documentImportedAt": row["imported_at"]}

    def _run_result(self, row: sqlite3.Row, allocations: Sequence[sqlite3.Row], evidence: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
        return {"id": row["id"], "billingAgreementId": row["billing_agreement_id"], "previousRunId": row["previous_run_id"], "version": row["run_version"], "status": row["status"], "allocationDate": row["allocation_date"], "feePool": row["fee_pool_decimal"], "currency": row["currency_code"], "totalEligibleAmount": row["total_eligible_decimal"], "roundingMethod": row["rounding_method"], "inputs": self._json(str(row["inputs_json"])), "relatedIssues": json.loads(str(row["related_issues_json"])), "createdAt": row["created_at"], "evidence": list(evidence), "allocations": [{"participantKey": item["participant_key"], "participantLabel": item["participant_label"], "subfundReference": item["subfund_reference"], "eligibleBasis": item["eligible_basis_decimal"], "share": item["share_decimal"], "allocatedFee": item["allocated_fee_decimal"], "currency": item["currency_code"], "eligibilityStatus": item["eligibility_status"], "eligibilityReason": item["eligibility_reason"], "holdingRecordIds": json.loads(str(item["holdings_record_ids_json"])), "evidence": json.loads(str(item["evidence_references_json"])), "invoiceRecipientStatus": item["invoice_recipient_status"]} for item in allocations]}
