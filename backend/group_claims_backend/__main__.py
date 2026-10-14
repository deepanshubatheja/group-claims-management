"""Entrypoint for the private stdin/stdout backend protocol."""

import argparse
from collections.abc import Sequence

from .application import main


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group Claims local backend")
    parser.add_argument(
        "--storage-root",
        help="Trusted absolute user-data path supplied by Electron during startup.",
    )
    return parser.parse_args(arguments)


if __name__ == "__main__":
    arguments = parse_arguments()
    raise SystemExit(main(storage_root=arguments.storage_root))
