"""Protocol tests using only Python's standard-library unittest framework."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def request(request_id: str, command: str = "health.check", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocolVersion": "1.0",
        "id": request_id,
        "command": command,
        "params": {},
    }
    payload.update(overrides)
    return payload


def run_backend(raw_input: bytes) -> tuple[list[dict[str, object]], str]:
    completed = subprocess.run(
        [sys.executable, "-m", "group_claims_backend"],
        cwd=BACKEND_ROOT,
        input=raw_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    responses = [
        json.loads(line)
        for line in completed.stdout.decode("utf-8", errors="strict").splitlines()
    ]
    return responses, completed.stderr.decode("utf-8", errors="strict")


class ProtocolServerTests(unittest.TestCase):
    def test_valid_health_check(self) -> None:
        responses, _ = run_backend((json.dumps(request("request-1")) + "\n").encode())

        self.assertEqual(len(responses), 1)
        response = responses[0]
        self.assertTrue(response["ok"])
        self.assertEqual(response["id"], "request-1")
        self.assertEqual(response["protocolVersion"], "1.0")
        result = response["result"]
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "ok")

    def test_unknown_command_is_rejected(self) -> None:
        responses, _ = run_backend((json.dumps(request("request-2", "system.exec")) + "\n").encode())

        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["error"]["code"], "unknown_command")

    def test_invalid_json_is_rejected(self) -> None:
        responses, _ = run_backend(b"{not-json}\n")

        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["id"], None)
        self.assertEqual(responses[0]["error"]["code"], "invalid_json")

    def test_missing_or_invalid_request_id_is_rejected(self) -> None:
        missing = {"protocolVersion": "1.0", "command": "health.check", "params": {}}
        invalid = request("contains whitespace")
        responses, _ = run_backend(
            (json.dumps(missing) + "\n" + json.dumps(invalid) + "\n").encode()
        )

        self.assertEqual([response["error"]["code"] for response in responses], [
            "invalid_request_id",
            "invalid_request_id",
        ])

    def test_unsupported_protocol_version_is_rejected(self) -> None:
        responses, _ = run_backend(
            (json.dumps(request("request-3", protocolVersion="9.9")) + "\n").encode()
        )

        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["id"], "request-3")
        self.assertEqual(responses[0]["error"]["code"], "unsupported_protocol_version")

    def test_multiple_sequential_requests(self) -> None:
        payload = "".join(json.dumps(request(f"request-{index}")) + "\n" for index in range(3))
        responses, _ = run_backend(payload.encode())

        self.assertEqual([response["id"] for response in responses], [
            "request-0",
            "request-1",
            "request-2",
        ])
        self.assertTrue(all(response["ok"] for response in responses))

    def test_utf8_request_and_response_handling(self) -> None:
        # The command remains allowlisted; a Unicode request id is rejected safely as malformed.
        payload = request("unicode-✓")
        responses, _ = run_backend((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

        self.assertFalse(responses[0]["ok"])
        self.assertEqual(responses[0]["error"]["code"], "invalid_request_id")

    def test_stdout_contains_only_json_responses(self) -> None:
        responses, stderr = run_backend((json.dumps(request("clean-output")) + "\n").encode())

        self.assertEqual(len(responses), 1)
        self.assertNotIn("health.check", stderr)


if __name__ == "__main__":
    unittest.main()
