#!/usr/bin/env python3
"""Explicit local maintenance command for pre-v15 reconciliation candidates.

It has no default data path and never imports documents, confirms candidates,
or changes confirmed business records.  The operator must provide the exact
local app-data root and acknowledge the operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from group_claims_backend.services.reconciliation import ReconciliationService
from group_claims_backend.storage.service import StorageService


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist unresolved conflicts among existing local holding candidates."
    )
    parser.add_argument("--data-root", required=True, help="Exact ClaimFlow local app-data root to repair.")
    parser.add_argument(
        "--acknowledged",
        action="store_true",
        help="Confirm that only conflict grouping/issue records should be added.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if not arguments.acknowledged:
        raise SystemExit("Refusing to run without --acknowledged.")
    root = Path(arguments.data_root).expanduser()
    if not root.is_absolute():
        raise SystemExit("--data-root must be an absolute path.")
    storage = StorageService(str(root))
    if storage.initialize().status != "ready":
        raise SystemExit("The specified local workspace could not be opened.")
    result = ReconciliationService(storage).backfill_provisional_holding_conflicts(
        {"acknowledged": True}
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
