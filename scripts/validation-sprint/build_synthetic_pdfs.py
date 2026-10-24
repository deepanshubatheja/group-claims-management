"""Create fictional source PDFs for an isolated ClaimFlow validation scenario."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz


PAGES = {
    "02_partial_transfer_effective_2025-02-15.pdf": """SYNTHETIC - PARTIAL SECURITIES TRANSFER NOTICE

Issue date: 20 February 2025
Effective date: 15 February 2025
Investor group: Redwood Investors LP

Atlas Custody Bank confirms a partial securities transfer of 200,000 nominal
units of ISIN XS0123456789, Example Credit Notes 6.25% Perpetual, from Horizon
Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B),
account CUST-ALPHA-02.

This is a movement of securities only. This notice does not assign, transfer,
or determine ownership of any legal claim rights.
""",
    "03_claim_right_assignment_effective_2025-02-18.pdf": """SYNTHETIC - CLAIM-RIGHTS ASSIGNMENT

Issue date: 21 February 2025
Effective date: 18 February 2025
Claim reference: CR-2025-ALPHA

Redwood Investors LP assigns 100 percent of its legal claim rights associated
with ISIN XS0123456789 to Northstar Claims SPV, effective 18 February 2025.

This assignment concerns legal claim rights only. It does not move securities,
does not amend custodian account balances, and does not change the dated
holdings of Horizon Credit Fund A, Horizon Credit Fund B, or Meridian Income
Fund C.
""",
    "05_contradictory_custodian_statement_2025-02-28.pdf": """SYNTHETIC - CUSTODIAN POSITION STATEMENT

Issue date: 10 March 2025
Statement as-of date: 28 February 2025
Custodian: Atlas Custody Bank

For account CUST-ALPHA-02, Horizon Credit Fund B (SF-B), the recorded holding
of ISIN XS0123456789 is 900,000 nominal units as of 28 February 2025.

This document conflicts with the later holdings statement stating 1,000,000
nominal units for the same account, security, and date. The difference is
unresolved in this document.
""",
    "06_approved_billing_instruction_2025-03-01.pdf": """SYNTHETIC - APPROVED PRO-RATA BILLING INSTRUCTION

Issue date: 1 March 2025
Approved by: Redwood Investors LP billing committee
Billing period
1 February 2025 to 28 February 2025

Fee pool
USD 120,000.00

Allocation rule
eligible nominal amount / total eligible nominal amount

Eligible participant 1
Horizon Credit Fund A (SF-A): 400,000

Eligible participant 2
Horizon Credit Fund B (SF-B): 1,000,000

Meridian Income Fund C (SF-C) is excluded because it is not participating in the relevant litigation for this billing period.

Currency rounding must reconcile the allocations to the fee pool. This instruction is a calculation authorization only; it is not an invoice and does not determine legal entitlement.
""",
}


def create_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    rect = fitz.Rect(54, 54, 558, 738)
    spare = page.insert_textbox(
        rect, text, fontname="helv", fontsize=11, lineheight=1.35,
        color=(0.05, 0.15, 0.28),
    )
    if spare < 0:
        raise RuntimeError(f"Synthetic content overflowed the page: {path.name}")
    document.set_metadata({"title": path.stem, "author": "ClaimFlow synthetic validation"})
    document.save(path)
    document.close()


def main() -> None:
    output_dir = Path(sys.argv[1]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in PAGES.items():
        create_pdf(output_dir / filename, text)


if __name__ == "__main__":
    main()
