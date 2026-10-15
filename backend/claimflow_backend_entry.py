"""PyInstaller entry point for ClaimFlow's private NDJSON backend."""

from group_claims_backend.__main__ import main, parse_arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    raise SystemExit(main(storage_root=arguments.storage_root))
