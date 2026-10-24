from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai_codex import ApprovalMode, Sandbox
from group_claims_backend.services.ai import AIServiceError, QuestionResponseContract
from group_claims_backend.services.codex import CodexManager, CodexProvider


class FakeCodexManager:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str, object]] = []

    def run(self, text: str, instructions: str, schema: object):
        self.calls.append((text, instructions, schema))
        return SimpleNamespace(
            final_response=json.dumps(self.response),
            usage=SimpleNamespace(last=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)),
        )


class CodexProviderTests(unittest.TestCase):
    def test_runtime_uses_ephemeral_read_only_thread_without_approvals(self):
        class FakeRuntime:
            def __init__(self):
                self.start_options = None
                self.run_options = None

            def account(self):
                return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(type="chatgpt")))

            def thread_start(self, **kwargs):
                self.start_options = kwargs
                return self

            def run(self, message, **kwargs):
                self.run_options = (message, kwargs)
                return SimpleNamespace(status=SimpleNamespace(value="completed"))

        with tempfile.TemporaryDirectory() as directory:
            manager = CodexManager(Path(directory))
            runtime = FakeRuntime()
            manager._client = runtime
            manager.run("synthetic excerpt", "do not use tools", {"type": "object"})
            self.assertTrue(runtime.start_options["ephemeral"])
            self.assertEqual(runtime.start_options["sandbox"], Sandbox.read_only)
            self.assertEqual(runtime.start_options["approval_mode"], ApprovalMode.deny_all)
            self.assertEqual(runtime.run_options[1]["sandbox"], Sandbox.read_only)
            self.assertEqual(runtime.run_options[0].content, "synthetic excerpt")

    def test_analysis_returns_existing_validated_contract(self):
        manager = FakeCodexManager({
            "summary": {"text": "A security is named.", "unknown": False, "citations": [
                {"sourceReference": "CSV!A1", "evidenceSnippet": "US0378331005"},
            ]},
            "proposals": [],
        })
        result = CodexProvider(manager).interpret("authorized excerpt")
        self.assertEqual(result.summary.text, "A security is named.")
        self.assertEqual(result.total_tokens, 15)
        self.assertEqual(manager.calls[0][0], "authorized excerpt")

    def test_answer_rejects_unknown_citation_key(self):
        manager = FakeCodexManager({
            "answer": "Unsupported", "unknown": False,
            "citations": [{"sourceReference": "E99"}],
        })
        with self.assertRaises(AIServiceError) as captured:
            CodexProvider(manager).answer_question("Which security?", "E1: Apple", ("E1",))
        self.assertEqual(captured.exception.code, "AI_RESPONSE_CONTRACT_INVALID")

    def test_historical_answer_uses_the_shared_contract_and_section_schema(self):
        manager = FakeCodexManager({
            "unknown": False,
            "sections": [
                {"kind": "holdings", "status": "documented", "text": "Fund A is 600,000.", "citations": [{"sourceReference": "E1"}]},
                {"kind": "claim_assignment", "status": "documented", "text": "Claim assigned.", "citations": [{"sourceReference": "E2"}]},
            ],
        })
        contract = QuestionResponseContract(True, "Keep securities separate from claim rights.", 4)
        result = CodexProvider(manager).answer_question(
            "Reconstruct", "bounded evidence", ("E1", "E2", "E3", "E4"), response_contract=contract,
        )
        self.assertIn("Claim-right assignment", str(result.answer))
        instructions = manager.calls[0][1]
        schema = manager.calls[0][2]
        self.assertIn("HISTORICAL RECONSTRUCTION CONTRACT", instructions)
        self.assertEqual(schema["properties"]["sections"]["items"]["properties"]["citations"]["maxItems"], 3)
        self.assertEqual(result.historical_metadata["responseStatus"], "complete")
        # Codex app-server receives text, instructions, and schema only: no
        # unsupported API max_output_tokens field is forwarded.
        self.assertEqual(len(manager.calls[0]), 3)

    def test_historical_partial_answer_is_compatible_with_codex(self):
        manager = FakeCodexManager({
            "unknown": False,
            "sections": [
                {"kind": "holdings", "status": "documented", "text": "Fund A is 600,000.", "citations": [{"sourceReference": "E1"}]},
                {"kind": "limitation", "status": "unsupported", "text": "Fund C is not established.", "citations": [{"sourceReference": "E2"}]},
            ],
        })
        result = CodexProvider(manager).answer_question(
            "Reconstruct", "bounded evidence", ("E1", "E2"),
            response_contract=QuestionResponseContract(True, "Keep uncertainty explicit.", 2),
        )
        self.assertIn("Limitation (unsupported)", str(result.answer))
        self.assertEqual(result.historical_metadata["responseStatus"], "partial")

    def test_historical_billing_contract_is_compatible_with_codex(self):
        keys = ("E1", "E2", "E3", "E4", "E5")
        manager = FakeCodexManager({
            "unknown": False,
            "sections": [{
                "kind": "billing", "status": "documented", "text": "The allocation is a local calculation.",
                "citations": [{"sourceReference": key} for key in keys],
            }],
        })
        result = CodexProvider(manager).answer_question(
            "Explain the approved allocation", "bounded evidence", keys,
            response_contract=QuestionResponseContract(True, "Use all approved billing inputs.", 5, keys),
        )
        self.assertIn("Billing (documented)", str(result.answer))
        instructions = manager.calls[0][1]
        schema = manager.calls[0][2]
        self.assertIn("fee-pool source alone never proves", instructions)
        self.assertIn("return Billing as unsupported", instructions)
        self.assertEqual(schema["properties"]["sections"]["items"]["properties"]["citations"]["maxItems"], 5)

    def test_incomplete_billing_citations_do_not_discard_supported_codex_findings(self):
        keys = ("E1", "E2", "E3", "E4", "E5")
        manager = FakeCodexManager({
            "unknown": False,
            "sections": [
                {
                    "kind": "contradiction", "status": "unresolved",
                    "text": "The two Fund B observations conflict.",
                    "citations": [{"sourceReference": "E2"}, {"sourceReference": "E3"}],
                },
                {
                    "kind": "billing", "status": "documented",
                    "text": "The fee pool proves the allocation.",
                    "citations": [{"sourceReference": "E1"}],
                },
            ],
        })
        result = CodexProvider(manager).answer_question(
            "Compare holdings and billing", "bounded evidence", keys,
            response_contract=QuestionResponseContract(True, "Use each instruction source.", 5, keys),
        )
        self.assertIn("Contradictory evidence (unresolved)", str(result.answer))
        self.assertIn("Billing (unsupported)", str(result.answer))
        self.assertNotIn("The fee pool proves the allocation.", str(result.answer))
        self.assertFalse(result.unknown)
        self.assertEqual(result.historical_metadata["responseStatus"], "partial")
        self.assertEqual(result.historical_metadata["billingCitationCoverage"], "incomplete")


if __name__ == "__main__":
    unittest.main()
