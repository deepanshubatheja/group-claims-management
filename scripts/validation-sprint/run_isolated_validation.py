"""Run the synthetic scenario against a disposable ClaimFlow data root."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.billing import BillingService
from group_claims_backend.services.reconciliation import ReconciliationService
from group_claims_backend.storage.service import StorageService


NAMES = (
    "01_opening_holdings_2025-01-31.xlsx",
    "02_partial_transfer_effective_2025-02-15.pdf",
    "03_claim_right_assignment_effective_2025-02-18.pdf",
    "04_later_holdings_2025-02-28.xlsx",
    "05_contradictory_custodian_statement_2025-02-28.pdf",
    "06_approved_billing_instruction_2025-03-01.pdf",
)


def main() -> None:
    source_dir = Path(sys.argv[1]).resolve()
    isolated_root = Path(sys.argv[2]).resolve()
    report_path = Path(sys.argv[3]).resolve()
    if isolated_root.exists():
        raise RuntimeError("Refusing to reuse an existing isolated-root path.")
    isolated_root.mkdir(parents=True)
    copied_sources = isolated_root / "authorized-sources"
    copied_sources.mkdir()
    for name in NAMES:
        shutil.copy2(source_dir / name, copied_sources / name)

    storage = StorageService(str(isolated_root / "app-data"))
    status = storage.initialize()
    if status.status != "ready":
        raise RuntimeError(f"Isolated storage failed: {status.status}")
    service = DocumentImportService(storage)
    response = service.import_authorized_paths([str(copied_sources / name) for name in NAMES])
    documents = response["documents"]
    duplicate = service.import_authorized_paths([str(copied_sources / NAMES[0])])["documents"][0]
    reconciliation = ReconciliationService(storage)
    billing_service = BillingService(storage)
    assert storage.database is not None and storage.paths is not None
    with storage.database.connection() as connection:
        candidate_rows = connection.execute(
            "SELECT id, candidate_type FROM reconciliation_candidates ORDER BY created_at, id"
        ).fetchall()
    reviewed_batch = reconciliation.apply_policy({
        "candidateIds": [str(row["id"]) for row in candidate_rows],
        "policy": "reviewed_batch",
        "acknowledged": True,
    })
    for row in candidate_rows:
        if row["candidate_type"] == "claim_assignment":
            reconciliation.confirm({"candidateId": str(row["id"]), "acknowledged": True})
    with storage.database.connection() as connection:
        sources = connection.execute(
            "SELECT id, original_filename, managed_relative_path, content_sha256, processing_status FROM source_documents ORDER BY original_filename"
        ).fetchall()
        xlsx_refs = connection.execute(
            "SELECT source_reference FROM extracted_content WHERE source_reference LIKE 'Holdings!%'"
        ).fetchall()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "reconciliation_candidates", "holding_positions", "securities_transfers",
                "claim_right_assignments", "issues", "evidence_references", "audit_events",
            )
        }
    hashes_match = all(
        hashlib.sha256((copied_sources / str(row["original_filename"])).read_bytes()).hexdigest() == str(row["content_sha256"])
        == hashlib.sha256((storage.paths.root / str(row["managed_relative_path"])).read_bytes()).hexdigest()
        for row in sources
    )
    with storage.database.connection() as connection:
        billing_candidate = connection.execute("SELECT id FROM billing_rule_candidates").fetchone()
    if billing_candidate is None:
        raise RuntimeError("The approved synthetic billing instruction did not create a billing-rule candidate.")
    billing_rule = billing_service.approve_rule({"candidateId": str(billing_candidate["id"]), "acknowledged": True})
    billing = billing_service.calculate({"billingAgreementId": billing_rule["id"], "acknowledged": True})
    billing_rows = {str(item["subfundReference"]): item for item in billing["allocations"]}
    billing_ok = (
        billing["status"] == "provisional"
        and billing["feePool"] == "120000.00"
        and billing["totalEligibleAmount"] == "1400000"
        and billing_rows["SF-A"]["allocatedFee"] == "34285.71"
        and billing_rows["SF-B"]["allocatedFee"] == "85714.29"
        and billing_rows["SF-C"]["eligibilityStatus"] == "excluded"
        and len(billing["relatedIssues"]) == 1
    )
    opening = reconciliation.holdings_as_of({"asOfDate": "2025-01-31"})
    transfer_date = reconciliation.holdings_as_of({"asOfDate": "2025-02-15"})
    closing = reconciliation.reconciliation_result({"asOfDate": "2025-02-28"})
    reopening = StorageService(str(isolated_root / "app-data"))
    restart_status = reopening.initialize().status

    def quantities(result: dict[str, object]) -> dict[str, str]:
        return {
            str(item["subfund"]): str(item["quantity"])
            for item in result["positions"]  # type: ignore[index]
        }

    opening_quantities = quantities(opening)
    transfer_quantities = quantities(transfer_date)
    closing_quantities = quantities(closing["holdings"])  # type: ignore[arg-type]
    discrepancies = closing["openDiscrepancies"]["issues"]  # type: ignore[index]
    report = {
        "isolatedRoot": str(isolated_root),
        "imports": {"expected": 6, "actual": len(sources), "parsed": sum(row["processing_status"] == "parsed" for row in sources), "pass": len(sources) == 6 and all(item["ok"] for item in documents)},
        "duplicateDetection": {"expected": True, "actual": bool(duplicate.get("duplicate")), "pass": bool(duplicate.get("duplicate"))},
        "vaultHashes": {"expected": True, "actual": hashes_match, "pass": hashes_match},
        "cellReferences": {"expected": True, "actual": bool(xlsx_refs), "pass": bool(xlsx_refs), "examples": [row["source_reference"] for row in xlsx_refs[:3]]},
        "reconciliation": {
            "candidates": counts["reconciliation_candidates"],
            "reviewedBatch": reviewed_batch,
            "expected": {
                "holdings": 5,
                "securitiesTransfers": 1,
                "claimAssignments": 1,
                "openIssues": 1,
                "evidenceReferences": 7,
            },
            "actual": {
                "holdings": counts["holding_positions"],
                "securitiesTransfers": counts["securities_transfers"],
                "claimAssignments": counts["claim_right_assignments"],
                "openIssues": counts["issues"],
                "evidenceReferences": counts["evidence_references"],
                "auditEvents": counts["audit_events"],
            },
            "pass": (
                counts["holding_positions"] == 5
                and counts["securities_transfers"] == 1
                and counts["claim_right_assignments"] == 1
                and counts["issues"] == 1
                and counts["evidence_references"] == 7
            ),
            "limitation": "The batch requires explicit acknowledgement. Both conflicting Fund B observations remain provisional; Fund B is reconstructed from the opening snapshot and transfer. The legal claim-right assignment is retained for individual confirmation and does not change securities holdings.",
        },
        "historicalReconciliation": {
            "opening": opening_quantities,
            "transferDate": transfer_quantities,
            "closing": closing_quantities,
            "states": sorted({str(item["reconstructionState"]) for item in transfer_date["positions"]}),  # type: ignore[index]
            "transfers": len(closing["securitiesTransfers"]["events"]),  # type: ignore[index]
            "claimAssignments": len(closing["claimRightAssignments"]["events"]),  # type: ignore[index]
            "openDiscrepancies": len(discrepancies),
            "contradictionDifference": discrepancies[0]["differenceAmounts"] if discrepancies else [],
            "restartStatus": restart_status,
            "pass": (
                opening_quantities == {"Horizon Credit Fund A (SF-A)": "600000", "Horizon Credit Fund B (SF-B)": "800000", "Meridian Income Fund C (SF-C)": "500000"}
                and transfer_quantities == {"Horizon Credit Fund A (SF-A)": "400000", "Horizon Credit Fund B (SF-B)": "1000000", "Meridian Income Fund C (SF-C)": "500000"}
                and closing_quantities == transfer_quantities
                and len(discrepancies) == 1
                and discrepancies[0]["differenceAmounts"] == ["100000"]
                and restart_status == "ready"
            ),
        },
        "billing": {
            "expectedTotal": "120000.00", "actual": billing,
            "pass": billing_ok,
            "limitation": "The saved calculation is not an invoice. It remains provisional because the Fund B 900,000 observation is unresolved, while the confirmed rule expressly authorizes its 1,000,000 allocation basis.",
        },
        "liveAi": {"status": "not-run", "reason": "No authorized test credential was supplied for this isolated validation run."},
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
