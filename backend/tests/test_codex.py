from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai_codex import ApprovalMode, Sandbox
from group_claims_backend.services.ai import AIServiceError
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


if __name__ == "__main__":
    unittest.main()
