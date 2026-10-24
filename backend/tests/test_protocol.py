"""Protocol tests using only Python's standard-library unittest framework."""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from group_claims_backend.protocol import MAX_REQUEST_BYTES, ProtocolServer
from group_claims_backend.storage.migrations import MIGRATIONS

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


def run_backend(
    raw_input: bytes, storage_root: Path | None = None, environment: dict[str, str] | None = None,
) -> tuple[list[dict[str, object]], str]:
    command = [sys.executable, "-m", "group_claims_backend"]
    if storage_root is not None:
        command.extend(["--storage-root", str(storage_root)])
    completed = subprocess.run(
        command,
        cwd=BACKEND_ROOT,
        input=raw_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
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

    def test_unicode_question_sized_at_5000_code_points_fits_the_unchanged_ndjson_guard(self) -> None:
        question = "😀" * 5_000
        server = ProtocolServer({"test.echo": lambda params: {"characters": len(str(params["question"]))}})
        stdout = io.BytesIO()
        server.serve(
            io.BytesIO((json.dumps(request("unicode-question", "test.echo", params={"question": question}), ensure_ascii=False) + "\n").encode("utf-8")),
            stdout,
        )
        response = json.loads(stdout.getvalue().decode("utf-8"))
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["characters"], 5_000)

    def test_oversized_ndjson_request_is_rejected_and_the_next_request_is_still_processed(self) -> None:
        oversized = json.dumps(request("too-large", "test.echo", params={"question": "x" * MAX_REQUEST_BYTES})) + "\n"
        follow_up = json.dumps(request("after-too-large", "test.echo", params={"question": "safe"})) + "\n"
        server = ProtocolServer({"test.echo": lambda params: {"question": params["question"]}})
        stdout = io.BytesIO()
        server.serve(io.BytesIO((oversized + follow_up).encode("utf-8")), stdout)
        responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(responses[0]["error"]["code"], "request_too_large")
        self.assertTrue(responses[1]["ok"])
        self.assertEqual(responses[1]["result"]["question"], "safe")

    def test_stdout_contains_only_json_responses(self) -> None:
        responses, stderr = run_backend((json.dumps(request("clean-output")) + "\n").encode())

        self.assertEqual(len(responses), 1)
        self.assertNotIn("health.check", stderr)

    def test_pdf_import_keeps_stdout_json_only(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "native.pdf"
            pdf = pymupdf.open()
            pdf.new_page().insert_text((72, 72), "Native PDF text")
            pdf.save(source)
            pdf.close()

            payload = "".join(json.dumps(item) + "\n" for item in (
                request("pdf-import", "documents.import", params={"paths": [str(source)]}),
                request("after-pdf-import"),
            ))
            responses, _ = run_backend(payload.encode(), root / "app-data")

        self.assertEqual([response["id"] for response in responses], ["pdf-import", "after-pdf-import"])
        self.assertTrue(responses[0]["ok"])
        self.assertTrue(responses[0]["result"]["documents"][0]["ok"])
        self.assertEqual(responses[0]["result"]["documents"][0]["document"]["processing_status"], "parsed")
        self.assertTrue(responses[1]["ok"])

    def test_storage_status_initializes_supplied_temporary_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "app-data"
            responses, _ = run_backend(
                (json.dumps(request("storage-1", "storage.status")) + "\n").encode(),
                root,
            )

            self.assertTrue(responses[0]["ok"])
            result = responses[0]["result"]
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["databaseStatus"], "connected")
            self.assertEqual(result["schemaVersion"], MIGRATIONS[-1].version)
            self.assertTrue(result["directoriesAvailable"])
            self.assertTrue((root / "database" / "claims.sqlite").is_file())

    def test_storage_failure_is_reported_without_falsely_reporting_ready(self) -> None:
        responses, _ = run_backend(
            (json.dumps(request("storage-error", "storage.status")) + "\n").encode(),
        )

        self.assertTrue(responses[0]["ok"])
        result = responses[0]["result"]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["databaseStatus"], "unavailable")
        self.assertFalse(result["directoriesAvailable"])

    def test_ai_connection_check_without_a_key_is_a_typed_safe_result(self) -> None:
        environment = dict(os.environ)
        environment.pop("OPENAI_API_KEY", None)
        environment["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        with tempfile.TemporaryDirectory() as temporary_directory:
            responses, stderr = run_backend(
                (json.dumps(request("ai-no-key", "ai.check_connection")) + "\n").encode(),
                Path(temporary_directory) / "app-data",
                environment,
            )
        self.assertTrue(responses[0]["ok"])
        self.assertEqual(responses[0]["result"]["outcome"], "api_key_missing")
        self.assertNotIn("OPENAI_API_KEY", stderr)

    def test_reconciliation_read_commands_are_allowlisted_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            payload = "".join(json.dumps(item) + "\n" for item in (
                request("timeline", "reconciliation.holdings_timeline"),
                request("transfers", "reconciliation.transfer_history"),
                request("assignments", "reconciliation.claim_assignment_history"),
                request("discrepancies", "reconciliation.open_discrepancies"),
                request("candidates", "reconciliation.candidates"),
                request("as-of", "reconciliation.holdings_as_of", params={"asOfDate": "2025-01-31"}),
                request("result", "reconciliation.result", params={"asOfDate": "2025-01-31"}),
            ))
            responses, _ = run_backend(payload.encode(), Path(temporary_directory) / "app-data")
        self.assertTrue(all(response["ok"] for response in responses))
        self.assertEqual(responses[0]["result"]["events"], [])
        self.assertEqual(responses[1]["result"]["events"], [])
        self.assertEqual(responses[2]["result"]["events"], [])
        self.assertEqual(responses[3]["result"]["issues"], [])
        self.assertEqual(responses[4]["result"]["candidates"], [])
        self.assertEqual(responses[5]["result"]["positions"], [])
        self.assertEqual(responses[6]["result"]["asOfDate"], "2025-01-31")

    def test_billing_read_commands_are_allowlisted_without_exposing_sqlite(self) -> None:
        identifier = "00000000-0000-4000-8000-000000000001"
        payload = "".join(json.dumps(item) + "\n" for item in (
            request("billing-candidates", "billing.candidates"),
            request("billing-rule", "billing.rule", params={"billingAgreementId": identifier}),
            request("billing-preview", "billing.preview", params={"billingAgreementId": identifier}),
            request("billing-runs", "billing.runs", params={"billingAgreementId": identifier}),
            request("billing-run", "billing.run", params={"runId": identifier}),
            request("billing-compare", "billing.compare_runs", params={"leftRunId": identifier, "rightRunId": identifier}),
        ))
        with tempfile.TemporaryDirectory() as temporary_directory:
            responses, _ = run_backend(payload.encode(), Path(temporary_directory) / "app-data")
        self.assertEqual(len(responses), 6)
        self.assertTrue(responses[0]["ok"])
        self.assertEqual(responses[0]["result"]["candidates"], [])
        self.assertTrue(responses[3]["ok"])
        self.assertTrue(all(response["ok"] or response["error"]["code"] != "unknown_command" for response in responses))

    def test_oversized_response_is_replaced_by_a_controlled_error(self) -> None:
        server = ProtocolServer({"test.large": lambda _params: {"value": "x" * 70_000}})
        stdout = io.BytesIO()
        server.serve(io.BytesIO((json.dumps(request("large-1", "test.large")) + "\n").encode()), stdout)

        response = json.loads(stdout.getvalue().decode())
        self.assertFalse(response["ok"])
        self.assertEqual(response["id"], "large-1")
        self.assertEqual(response["error"]["code"], "response_too_large")

if __name__ == "__main__":
    unittest.main()
