"""Newline-delimited JSON protocol used only by Electron and the local backend."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, Final

PROTOCOL_VERSION: Final = "1.0"
MAX_REQUEST_BYTES: Final = 65_536
MAX_RESPONSE_BYTES: Final = 60_000
MAX_REQUEST_ID_LENGTH: Final = 128
_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProtocolError(Exception):
    """A safe, client-facing protocol error."""

    code: str
    message: str
    request_id: str | None = None


def _is_record(value: object) -> bool:
    return isinstance(value, dict)


def _validate_request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_PATTERN.fullmatch(value):
        raise ProtocolError("invalid_request_id", "Request id is missing or invalid.")
    return value


def _error_response(error: ProtocolError) -> dict[str, object]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "id": error.request_id,
        "ok": False,
        "error": {"code": error.code, "message": error.message},
    }


def _success_response(request_id: str, result: Mapping[str, object]) -> dict[str, object]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": dict(result),
    }


def _encode_response(response: Mapping[str, object]) -> bytes:
    return (
        json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


CommandHandler = Callable[[Mapping[str, object]], Mapping[str, object]]


class ProtocolServer:
    """Handles the allowlisted request/response protocol over byte streams."""

    def __init__(self, commands: Mapping[str, CommandHandler]) -> None:
        self._commands = dict(commands)

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        """Read one request line at a time and emit one JSON response per line."""

        while True:
            raw_line = stdin.readline(MAX_REQUEST_BYTES + 1)
            if raw_line == b"":
                return

            if len(raw_line) > MAX_REQUEST_BYTES:
                self._discard_remainder_of_line(stdin, raw_line)
                self._write(stdout, _error_response(ProtocolError(
                    "request_too_large", "Request exceeds the maximum allowed size."
                )))
                continue

            self._write(stdout, self._handle_line(raw_line))

    @staticmethod
    def _discard_remainder_of_line(stdin: BinaryIO, partial_line: bytes) -> None:
        """Drain an oversized request without retaining its contents in memory."""

        if partial_line.endswith(b"\n"):
            return
        while True:
            chunk = stdin.readline(MAX_REQUEST_BYTES + 1)
            if chunk == b"" or chunk.endswith(b"\n"):
                return

    @staticmethod
    def _write(stdout: BinaryIO, response: Mapping[str, object]) -> None:
        encoded = _encode_response(response)
        if len(encoded) > MAX_RESPONSE_BYTES:
            request_id = response.get("id")
            encoded = _encode_response(
                _error_response(
                    ProtocolError(
                        "response_too_large",
                        "The backend response exceeds the safe protocol limit.",
                        request_id if isinstance(request_id, str) else None,
                    )
                )
            )
        stdout.write(encoded)
        stdout.flush()

    def _handle_line(self, raw_line: bytes) -> dict[str, object]:
        try:
            text = raw_line.rstrip(b"\r\n").decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _error_response(ProtocolError("invalid_encoding", "Request must be UTF-8 JSON."))

        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            return _error_response(ProtocolError("invalid_json", "Request must be valid JSON."))

        request_id: str | None = None
        try:
            if not _is_record(payload):
                raise ProtocolError("invalid_request", "Request must be a JSON object.")

            request_id = _validate_request_id(payload.get("id"))
            protocol_version = payload.get("protocolVersion")
            if protocol_version != PROTOCOL_VERSION:
                raise ProtocolError(
                    "unsupported_protocol_version",
                    "Protocol version is not supported.",
                    request_id,
                )

            command = payload.get("command")
            if not isinstance(command, str) or command not in self._commands:
                raise ProtocolError("unknown_command", "Command is not allowed.", request_id)

            params = payload.get("params")
            if not _is_record(params):
                raise ProtocolError("invalid_params", "Command parameters must be an object.", request_id)

            return _success_response(request_id, self._commands[command](params))
        except ProtocolError as error:
            return _error_response(error)
        except Exception as error:  # pragma: no cover - defensive boundary for future services
            code = getattr(error, "code", None)
            if isinstance(code, str) and code.replace("_", "").isalnum():
                return _error_response(ProtocolError(code.lower(), str(error), request_id))
            LOGGER.exception("A backend command failed.")
            return _error_response(
                ProtocolError("internal_error", "Backend could not process the request.", request_id)
            )
