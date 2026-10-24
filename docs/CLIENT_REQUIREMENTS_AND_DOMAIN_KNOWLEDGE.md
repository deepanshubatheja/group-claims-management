# ClaimFlow — Client Requirements and Domain Knowledge

**Updated:** 22 October 2026  
**Status:** Working draft. Some points still need the client's confirmation.

## 1. The problem

A law firm handles a legal claim for a group of investors. Some investors have holdings spread across dozens or even hundreds of funds and subfunds.

The firm receives spreadsheets, bank statements, PDFs and other documents. The records may have different dates and names. Holdings can change. Securities can move between accounts. Legal claim rights can also be transferred, sometimes separately from the securities themselves.

The firm needs a reliable answer to these questions:

- Who is the investor, and which fund or subfund is involved?
- What securities did they hold, how much, and on what date?
- Did the holdings change or move to someone else?
- Who has the legal right to the claim?
- Which person or company should receive the invoice?
- How should legal costs be divided?
- Which original documents support the answer?

The aim is to keep this information organised, notice important changes and make billing easier to check.

## 2. The example given by the client

The client provided sample holdings records with made-up investor and fund names. The security identifiers refer to actual Credit Suisse bonds written down in 2023.

A typical case might involve 15–20 investors. Some may have hundreds of subfunds. Some subfunds can have legal rights and obligations in their own name, while others cannot. This can affect who the lawyer invoices.

The Credit Suisse case is an example, not a limit on what ClaimFlow can handle. Holding a security does not, by itself, prove someone has a valid legal claim.

## 3. Words used in this project

| Term | Simple meaning |
| --- | --- |
| Investor | A person or organisation involved in the group claim. |
| Fund | A pool or structure used to hold investments. |
| Subfund | A part of a larger fund, which may need to be tracked separately. |
| Legal entity | A person or organisation that can hold legal rights and obligations. Not every subfund is its own legal entity. |
| Custodian | A bank or other institution keeping securities for a customer. |
| Account | The account on a statement where securities are recorded. An account number does not prove who owns the claim. |
| Security | A financial investment, such as a bond. |
| ISIN | A code identifying a security. It does not identify the owner. |
| Holding | The amount of a security shown for a person, fund or account on a particular date. |
| Claim right | A legal right to make or share in a claim. It may not follow a security transfer automatically. |
| Assignment | A transfer of legal claim rights from one party to another. |
| Pro-rata billing | Splitting an amount between parties in proportion to an agreed measure. |
| Evidence | The document and the exact part of it that supports a fact. |
| Reconciliation | Comparing records to find what matches, what changed and what does not make sense. |

## 4. Dates must not be mixed up

One document can contain several different dates:

| Date | What it means |
| --- | --- |
| Holding date | The day the reported holdings are true for. |
| Document date | The day the statement or letter was issued. |
| Upload date | The day a lawyer added the file to ClaimFlow. |
| Transfer date | The date the securities transfer took effect, where established. |
| Assignment date | The date a transfer of claim rights took effect, where established. |
| Trade / settlement date | The date of a trade and the date it settled, when relevant. |
| Billing date or period | The date or period that the agreed fee calculation uses. |

**Example:** A statement was issued on 2 February 2025 and says “Holdings as of 31 January 2025.” The lawyer uploads it on 10 March 2025. The holding date is **31 January**, not 2 February or 10 March.

If a date is missing, ClaimFlow should show it as unknown, not copy another date into its place.

## 5. What the client needs

### Documents and history

1. Staff should be able to add individual files or a full folder manually.
2. The original files should be kept unchanged.
3. The system should record when each file was added.
4. It should be possible to find the document, page, row or cell behind an important figure.
5. New evidence should not erase earlier records. Corrections and changes should remain traceable.

### People, funds and accounts

6. Keep investors, funds, subfunds, legal entities and accounts distinct.
7. Do not assume two accounts belong to different investors, or that a subfund can be invoiced in its own name.
8. Keep separate holdings for separate subfunds, even when they belong to the same investor.

### Changes and legal rights

9. Compare holdings from different dates and show changes.
10. Record documented transfers, including the parties, security, amount and effective date.
11. Track assignments of legal claim rights separately from security transfers.
12. A difference between two holdings statements is not enough to prove why the change happened.
13. Flag conflicting names, quantities, dates and other important information. Do not quietly guess which version is right.
14. Staff should only need to deal with meaningful problems, not approve every ordinary extracted line.

### Billing

15. Split fees according to the rules agreed for that case, not a universal formula.
16. Work out the right amount for each party or subfund and identify who should receive the invoice.
17. Show the numbers, calculation method, dates, and any exclusions or adjustments.
18. Make it possible to check the total against the sum of all individual shares.
19. When an important fact or billing rule is unresolved, make that clear rather than presenting a final number as certain.

### Privacy and ease of use

20. The client wants a straightforward desktop app rather than a hosted website.
21. Documents should be stored on the user's computer, with no automatic access to email or online file storage.
22. Optional online AI analysis must be clearly distinguished from local storage. Sending document content externally needs clear permission and agreed handling rules.
23. The first version can be for one user. Shared use by several lawyers is a possible later requirement, not something to assume now.

## 6. Examples of how it should behave

### Example A: Three subfunds

An investor has these holdings on 31 January 2025:

| Subfund | Bond holding |
| --- | ---: |
| Fund A | 100,000 |
| Fund B | 200,000 |
| Fund C | 300,000 |
| **Total** | **600,000** |

ClaimFlow should show both the three separate holdings and the total. It should not automatically send three invoices. That depends on the legal entities and billing instructions.

### Example B: Securities move, but claim rights may not

A record shows bonds moving from Fund A to Fund B on 15 February 2025. Another document shows claim rights being assigned on 18 February 2025.

These are two different events with two different dates. The app must not treat them as one event or assume that moving the bonds also moved the claim rights.

### Example C: Two statements disagree

One document reports 200,000 units and another reports 250,000 units for the same reported position and date.

ClaimFlow should show the disagreement and link to both documents. It should not simply choose the larger or newer figure.

### Example D: Simple proportional billing

Suppose the confirmed agreement says to divide a €1,200 fee according to eligible holdings of 100, 200 and 300 units.

| Party | Share of eligible holdings | Fee |
| --- | --- | ---: |
| A | 100 out of 600 | €200 |
| B | 200 out of 600 | €400 |
| C | 300 out of 600 | €600 |
| **Total** | **600 out of 600** | **€1,200** |

This only shows how proportional splitting works. We still need the client's actual rules for eligible amounts, billing dates, taxes, currency, rounding and who gets invoiced.

### Example E: A new file arrives later

The lawyer uploads a corrected holdings statement in April for a position reported in January.

The January position date should stay January. The April upload should be recorded separately. The original statement and the correction should both remain available.

## 7. Cases we need to support and test

| Case | What should happen |
| --- | --- |
| Add a statement | Save the original and record the upload date. |
| Add a folder | Process the files while keeping each source identifiable. |
| Read holdings | Capture the security, amount, holder/account, correct holding date and source. |
| Compare two dates | Show increases and decreases without guessing the reason. |
| Account number changes | Check whether it is a new account, not automatically a new investor. |
| Security transfer | Show the amount, parties, date and supporting document. |
| Claim-right assignment | Record it separately and link the proof. |
| Missing date or owner | Mark it unknown and show why it matters. |
| Conflicting documents | Show the conflict and both sources. |
| Same file added twice | Avoid counting the same holdings twice. |
| Pro-rata calculation | Show each share, totals, rule used and evidence. |
| Invoice recipient | Use the agreed instructions and legal identity, not a guess based on a fund name. |
| Corrected document | Preserve the old version and show what changed. |

## 8. What is still not settled

These points need to be answered by the client before we can say the billing is correct:

- Which holdings or claim rights count towards fees, and on what date?
- Is the fee based on bond value, eligible claim amount, recovery, or something else?
- Is billing done once, repeatedly, or at certain points in a case?
- How do transfers after the 2023 write-down affect claim rights?
- Who receives the invoice when a subfund is not a separate legal entity?
- What should happen when ownership, dates, amounts or assignments cannot be proved?
- Which currencies, exchange rates, taxes, minimum fees, caps and rounding rules apply?
- What happens to an earlier invoice when new evidence changes the result?
- Who can resolve disputed facts and approve exceptions?
- What are the agreed privacy conditions for optional online AI analysis?

**Do not turn an open question into a rule without confirmation.**

## 9. How to check that it works

For every test, keep the input documents and the expected answer so someone can check the result by hand.

A good test should answer:

1. Did it read the right people, funds, securities and amounts?
2. Did it use the correct dates?
3. Did it keep transfers of securities separate from assignments of claim rights?
4. Did it find missing or conflicting facts?
5. Did it preserve the original evidence and earlier history?
6. Did it calculate the correct shares under the stated rule?
7. Can someone trace each important result back to a document and understand the calculation?

Passing a sample calculation does not prove the legal billing rule is correct. The rule itself must come from the client.

## 10. Keeping this document up to date

This is the main place for the client's problem, business rules, explanations and use cases.

When we learn something new:

1. Add it to the relevant section in plain language.
2. Say whether it is **confirmed by the client**, **our current assumption**, or **still an open question**.
3. Add a short example or test where useful.
4. Record important decisions below, including the date and where the decision came from.
5. Keep coding tools, database design, AI model names and other software details in separate technical documentation.

### Decision record

| Date | Topic | Status |
| --- | --- | --- |
| 22 October 2026 | Initial problem and sample holdings | Based on the client's shared problem statement. |
| 22 October 2026 | Local desktop use and manual uploads | Current project requirements; confirm final privacy wording with client. |
| 22 October 2026 | Separate security transfers and claim assignments | Current business use case. |
| 22 October 2026 | Exact billing rules | Open — client confirmation needed. |

---

This document describes **what needs to happen and why**. It does not describe how the software is built.
