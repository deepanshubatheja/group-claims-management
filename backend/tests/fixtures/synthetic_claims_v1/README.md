# Synthetic claims v1 fixtures

This directory is the local destination for generated fictional regression
sources. They contain no client, production, credential, or user-app-data
content and are intentionally excluded from Git.

- `01` and `04` are dated holdings snapshots.
- `02` is a securities transfer notice.
- `03` is a legal claim-right assignment, separate from the securities transfer.
- `05` intentionally contradicts the later Fund B holding.
- `06` contains the approved fictional pro-rata allocation rule.
- `ClaimFlow_Synthetic_Ground_Truth.xlsx` is the independently prepared expected-results workbook.

Regenerate sources in any temporary directory with:

```bash
backend/.venv/bin/python scripts/validation-sprint/build_synthetic_pdfs.py /tmp/claimflow-synthetic
backend/.venv/bin/python scripts/validation-sprint/build_synthetic_workbooks.py /tmp/claimflow-synthetic
```

The tests generate the source documents in their own isolated temporary
directories. Keep screenshots, inspection NDJSON, rendered workbook images,
runtime databases, and validation reports under ignored `outputs/`.
