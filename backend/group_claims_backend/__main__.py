"""Entrypoint for the private stdin/stdout backend protocol."""

from .application import main


if __name__ == "__main__":
    raise SystemExit(main())
