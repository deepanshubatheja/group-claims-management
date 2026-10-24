# ClaimFlow synthetic test matter

Every file in this folder is fictional test data. Do not treat it as a client record, legal notice, or approved invoice.

## Import and test

1. Import the two holdings workbooks and the three PDF notices.
2. Select all five documents in Matter Review and run consolidated analysis with explicit cloud-AI consent.
3. Ask the suggested questions below.

## Expected facts

- Between 28 March and 30 April 2023, Horizon Credit Fund A / AC-1001 falls from 1,000,000 to 600,000 nominal units.
- Horizon Credit Fund B / AC-2001 rises from 600,000 to 1,000,000 nominal units.
- The assignment notice separately records 400,000 related legal claim rights assigned from Fund A to Fund B effective 15 April 2023.
- Meridian Income Fund C / AC-3001 is withdrawn from litigation effective 20 April 2023, while its holding remains 400,000.
- The synthetic billing instruction approves USD 120,000.00, using 30 April eligible nominal holdings. Fund A receives USD 45,000.00 and Fund B receives USD 75,000.00. Meridian is excluded.

## Questions to test

- Which accounts and subfunds changed between the two dated holdings schedules?
- Is there explicit evidence of a legal-right assignment?
- Which subfund is no longer participating in the litigation?
- What inputs are approved for the pro-rata allocation?
- Using the approved instruction, what are the illustrative allocations and why is Meridian excluded?

ClaimFlow should keep the holding movement, rights assignment, litigation status, and billing inputs distinct. The prototype should present results as provisional and must not issue an invoice automatically.
