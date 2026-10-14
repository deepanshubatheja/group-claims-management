"""Application entry point and command dispatch for the local backend."""

from __future__ import annotations

import logging
import sys
from typing import BinaryIO

from .protocol import ProtocolServer
from .services.health import get_health_status


def configure_logging() -> None:
    """Configure operational logging without writing protocol data to stdout."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def main(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Serve the restricted local protocol until stdin is closed."""

    configure_logging()
    server = ProtocolServer(health_check=get_health_status)
    server.serve(
        stdin if stdin is not None else sys.stdin.buffer,
        stdout if stdout is not None else sys.stdout.buffer,
    )
    return 0
