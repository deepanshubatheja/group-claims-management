from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from group_claims_backend.services.ai import (
    AICostControls,
    AIService,
    AIServiceError,
    MockAIProvider,
    OPENAI_API_BASE_URL,
    OpenAIProvider,
    _normalized_openai_api_key,
    validate_answer,
    validate_proposal,
    validate_proposals,
)
from group_claims_backend.storage.service import StorageService


def valid_proposal(**overrides):
    proposal = {
        "proposalType": "holding",
        "fieldName": "isin",
        "value": "US0000000001",
        "unknown": False,
        "confidence": 0.8,
        "sourceDocumentId": "doc",
        "extractionId": None,
        "sourceReference": "Sheet1!A1",
        "evidenceSnippet": "US0000000001",
        "modelVersion": "gpt-4.1-mini",
    }
    proposal.update(overrides)
    return proposal


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.storage = StorageService(str(Path(self.temp.name) / "app")); self.storage.initialize()
        self.previous = {key: os.environ.pop(key, None) for key in ("GROUP_CLAIMS_AI_PROVIDER", "GROUP_CLAIMS_AI_MODEL", "GROUP_CLAIMS_AI_API_KEY", "OPENAI_API_KEY")}
    def tearDown(self):
        for key, value in self.previous.items():
            if value is not None: os.environ[key] = value
        self.temp.cleanup()
    def test_ai_is_disabled_without_configuration_and_consent(self):
        status = AIService(self.storage).status(); self.assertFalse(status["enabled"]); self.assertFalse(status["authorized"]); self.assertFalse(status["configured"])
        self.assertEqual(status["configurationState"], "provider_not_configured")
    def test_openai_configuration_distinguishes_missing_key_and_untested_provider(self):
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        service = AIService(self.storage)
        missing = service.status()
        self.assertEqual(missing["configurationState"], "api_key_missing")
        self.assertFalse(missing["credentialPresent"])
        os.environ["OPENAI_API_KEY"] = "test-key"
        status = service.status()
        self.assertEqual(status["configurationState"], "configured_not_live_tested")
        self.assertTrue(status["configured"])
        self.assertTrue(status["credentialPresent"])

    def test_codex_connection_is_separate_from_cloud_authorization(self):
        class FakeCodex:
            signed_in = False

            def connected(self):
                return self.signed_in

            def check_connection(self):
                return None

        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "codex"
        codex = FakeCodex()
        service = AIService(self.storage, codex_manager=codex)
        self.assertEqual(service.status()["configurationState"], "codex_login_required")
        self.assertEqual(service.check_connection({})["outcome"], "codex_login_required")
        codex.signed_in = True
        status = service.status()
        self.assertTrue(status["configured"])
        self.assertFalse(status["authorized"])
        self.assertEqual(service.check_connection({})["outcome"], "available")

    def test_whitespace_only_key_is_missing_and_surrounding_whitespace_is_not_sent(self):
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = " \n\t "
        self.assertFalse(AIService(self.storage).status()["credentialPresent"])
        self.assertIsNone(_normalized_openai_api_key(" \n\t "))
        self.assertEqual(_normalized_openai_api_key("  synthetic-key\n"), "synthetic-key")
    def test_authorization_is_audited_without_payload(self):
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        service = AIService(self.storage); self.assertTrue(service.set_authorization({"authorized": True, "acknowledged": True})["authorized"])
        assert self.storage.database
        with self.storage.database.connection() as connection: details = connection.execute("SELECT details_json FROM audit_events WHERE event_type = 'ai_authorization_updated'").fetchone()[0]
        self.assertNotIn("secret", details); self.assertNotIn("document", details)
    def test_authorization_does_not_carry_to_another_provider(self):
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        service = AIService(self.storage)
        self.assertTrue(service.set_authorization({"authorized": True, "acknowledged": True})["authorized"])
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "codex"
        self.assertFalse(service.status()["authorized"])
        self.assertFalse(service.set_authorization({"authorized": False, "acknowledged": True})["authorized"])
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"
        self.assertFalse(service.status()["authorized"])
    def test_invalid_authorization_and_proposals_are_rejected(self):
        with self.assertRaises(AIServiceError): AIService(self.storage).set_authorization({"authorized": True})
        with self.assertRaises(AIServiceError): validate_proposal({"proposalType": "holding"})
    def test_unknown_proposal_is_preserved_as_unknown(self):
        proposal = validate_proposal(valid_proposal(value=None, unknown=True, confidence=None))
        self.assertTrue(proposal["unknown"])
    def test_mock_provider_is_local_and_validates_synthetic_output(self):
        provider = MockAIProvider({"proposals": [valid_proposal()]})
        result = provider.interpret("synthetic-only")
        self.assertEqual(result.proposals[0]["fieldName"], "isin")
        self.assertEqual(provider.requests, ["synthetic-only"])

class FakeResponses:
    def __init__(self, response=None, error=None): self.response, self.error, self.calls = response, error, []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return self.response
class FakeModels:
    def __init__(self, error=None): self.error, self.calls = error, 0
    def list(self):
        self.calls += 1
        if self.error: raise self.error
        return type("Models", (), {"data": []})()
class FakeClient:
    def __init__(self, response=None, error=None, model_error=None):
        self.responses = FakeResponses(response, error); self.models = FakeModels(model_error)
class FakeResponse:
    status = "completed"; model = "gpt-4.1-mini"; refusal = None
    def __init__(self, text, usage=None): self.output_text, self.usage = text, usage

class OpenAIProviderTests(unittest.TestCase):
    def payload(self):
        return json.dumps({
            "summary": {
                "text": "The analyzed source identifies a security identifier.",
                "unknown": False,
                "citations": [{"sourceReference": "CSV!A1", "evidenceSnippet": "US0378331005"}],
            },
            "proposals": [valid_proposal(proposalType="security")],
        })
    def test_uses_strict_responses_api_without_tools_or_storage(self):
        client = FakeClient(FakeResponse(self.payload(), type("Usage", (), {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})()))
        provider = OpenAIProvider(api_key="test-key", client=client, sleep=lambda _: None)
        self.assertEqual(provider.availability_state, "configured_not_live_tested")
        result = provider.interpret("synthetic only")
        self.assertEqual(result.total_tokens, 7); call = client.responses.calls[0]
        self.assertFalse(call["store"]); self.assertEqual(call["tools"], []); self.assertTrue(call["text"]["format"]["strict"])
        self.assertEqual(call["text"]["format"]["schema"]["properties"]["proposals"]["maxItems"], 5)
        self.assertEqual(result.summary.text, "The analyzed source identifies a security identifier.")
        self.assertEqual(call["text"]["format"]["schema"]["properties"]["summary"]["properties"]["citations"]["minItems"], 1)
        self.assertEqual(call["model"], "gpt-4.1-mini")
        self.assertEqual(provider.availability_state, "available")

    def test_question_uses_a_strict_cited_schema_without_tools_or_storage(self):
        payload = json.dumps({"answer": "The source supports the identifier.", "unknown": False, "citations": [{"sourceReference": "E1"}]})
        client = FakeClient(FakeResponse(payload, type("Usage", (), {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7})()))
        result = OpenAIProvider(api_key="test-key", client=client, sleep=lambda _: None, max_output_tokens=500).answer_question("What is it?", "[CSV!A1] US0000000001", ("E1",))
        self.assertEqual(result.answer, "The source supports the identifier.")
        self.assertEqual(result.citations[0]["sourceReference"], "E1")
        call = client.responses.calls[0]
        self.assertFalse(call["store"]); self.assertEqual(call["tools"], []); self.assertTrue(call["text"]["format"]["strict"])
        schema = call["text"]["format"]["schema"]["properties"]
        self.assertEqual(schema["answer"]["type"], "string")
        self.assertEqual(schema["citations"]["minItems"], 1)
        self.assertEqual(schema["citations"]["maxItems"], 2)
        self.assertEqual(schema["citations"]["items"]["properties"]["sourceReference"]["enum"], ["E1"])
        self.assertIn("no more than 120 words", call["instructions"])

    def test_question_answer_requires_citations_and_a_valid_envelope(self):
        with self.assertRaises(AIServiceError):
            validate_answer({"answer": "unsupported", "unknown": False, "citations": []})
        malformed = FakeClient(FakeResponse(json.dumps({"answer": "unsupported", "unknown": False, "citations": []})))
        with self.assertRaises(AIServiceError) as raised:
            OpenAIProvider(api_key="test-key", client=malformed, sleep=lambda _: None).answer_question("Question", "context", ("E1",))
        self.assertEqual(raised.exception.code, "AI_RESPONSE_CONTRACT_INVALID")

    def test_question_answer_rejects_more_than_two_citations(self):
        with self.assertRaises(AIServiceError):
            validate_answer({
                "answer": "unsupported",
                "unknown": False,
                "citations": [
                    {"sourceReference": "E1"},
                    {"sourceReference": "E2"},
                    {"sourceReference": "E3"},
                ],
            })

    def test_unknown_answer_requires_evidence_and_is_shown_as_no_conclusion(self):
        answer, unknown, citations = validate_answer({
            "answer": "Not established by the available evidence.",
            "unknown": True,
            "citations": [{"sourceReference": "E1"}],
        })
        self.assertIsNone(answer)
        self.assertTrue(unknown)
        self.assertEqual(citations[0]["sourceReference"], "E1")
    def test_missing_key_and_malformed_or_refused_responses_are_controlled(self):
        with self.assertRaisesRegex(AIServiceError, "not configured"): OpenAIProvider(api_key="").interpret("synthetic")
        with self.assertRaises(AIServiceError): OpenAIProvider(api_key="key", client=FakeClient(FakeResponse("not json")), sleep=lambda _: None).interpret("synthetic")
        refused = FakeResponse(self.payload()); refused.refusal = "no"
        with self.assertRaises(AIServiceError): OpenAIProvider(api_key="key", client=FakeClient(refused), sleep=lambda _: None).interpret("synthetic")
        nested_refusal = FakeResponse(self.payload())
        nested_refusal.output = [type("Message", (), {"content": [type("Refusal", (), {"type": "refusal", "refusal": "no"})()]})()]
        with self.assertRaises(AIServiceError): OpenAIProvider(api_key="key", client=FakeClient(nested_refusal), sleep=lambda _: None).interpret("synthetic")

    def test_response_failures_have_safe_distinct_categories(self):
        provider = OpenAIProvider(api_key="key", client=FakeClient(FakeResponse("not json")), sleep=lambda _: None)
        with self.assertRaises(AIServiceError) as raised:
            provider.interpret("synthetic")
        self.assertEqual(raised.exception.code, "AI_RESPONSE_JSON_INVALID")

        invalid_envelope = FakeResponse(json.dumps({"proposals": "not-a-list"}))
        provider = OpenAIProvider(api_key="key", client=FakeClient(invalid_envelope), sleep=lambda _: None)
        with self.assertRaises(AIServiceError) as raised:
            provider.interpret("synthetic")
        self.assertEqual(raised.exception.code, "AI_RESPONSE_CONTRACT_INVALID")

        fallback = FakeResponse(None)
        fallback.output = [type("Message", (), {"content": [type("Text", (), {"type": "output_text", "text": self.payload()})()]})()]
        result = OpenAIProvider(api_key="key", client=FakeClient(fallback), sleep=lambda _: None).interpret("synthetic")
        self.assertEqual(len(result.proposals), 1)

    def test_individual_schema_invalid_proposals_are_left_for_safe_per_item_rejection(self):
        proposals = validate_proposals({"proposals": [{"proposalType": "security"}]})
        self.assertEqual(len(proposals), 1)

    def test_response_envelope_rejects_more_than_the_bounded_proposal_cap(self):
        with self.assertRaises(AIServiceError):
            validate_proposals({"proposals": [valid_proposal() for _ in range(6)]})

    def test_output_limit_incomplete_response_has_a_controlled_error_code(self):
        incomplete = FakeResponse(self.payload())
        incomplete.status = "incomplete"
        incomplete.incomplete_details = type("Details", (), {"reason": "max_tokens"})()
        with self.assertRaises(AIServiceError) as raised:
            OpenAIProvider(api_key="key", client=FakeClient(incomplete), sleep=lambda _: None).interpret("synthetic")
        self.assertEqual(raised.exception.code, "AI_OUTPUT_LIMIT_REACHED")

    def test_explicit_key_is_normalized_without_exposing_it(self):
        provider = OpenAIProvider(api_key="  synthetic-key\n", client=FakeClient())
        self.assertEqual(provider._api_key, "synthetic-key")
        self.assertNotIn("synthetic-key", provider.availability_state)

    def test_provider_pins_the_official_openai_endpoint(self):
        names = (
            "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
            "GROUP_CLAIMS_OPENAI_ORG_ID", "GROUP_CLAIMS_OPENAI_PROJECT_ID",
        )
        previous = {name: os.environ.get(name) for name in names}
        os.environ["OPENAI_BASE_URL"] = "https://untrusted.example.test/v1"
        os.environ["OPENAI_ORG_ID"] = "org-unrelated"
        os.environ["OPENAI_PROJECT_ID"] = "proj-unrelated"
        try:
            client = OpenAIProvider(api_key="synthetic-key")._create_client()
            self.assertEqual(str(client.base_url).rstrip("/"), OPENAI_API_BASE_URL)
            self.assertIsNone(client.organization)
            self.assertIsNone(client.project)
            os.environ["GROUP_CLAIMS_OPENAI_ORG_ID"] = "org-intended"
            os.environ["GROUP_CLAIMS_OPENAI_PROJECT_ID"] = "proj-intended"
            routed_client = OpenAIProvider(api_key="synthetic-key")._create_client()
            self.assertEqual(routed_client.organization, "org-intended")
            self.assertEqual(routed_client.project, "proj-intended")
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_timeout_rate_limit_and_authentication_are_redacted(self):
        for name, expected in (("APITimeoutError", "AI_TIMEOUT"), ("RateLimitError", "AI_RATE_LIMITED"), ("AuthenticationError", "AI_AUTHENTICATION_FAILED")):
            error = type(name, (Exception,), {})()
            client = FakeClient(error=error)
            with self.assertRaises(AIServiceError) as raised: OpenAIProvider(api_key="secret-value", client=client, sleep=lambda _: None).interpret("synthetic")
            self.assertEqual(raised.exception.code, expected); self.assertNotIn("secret-value", str(raised.exception))
            self.assertEqual(len(client.responses.calls), 1)

    def test_retries_are_opt_in_and_output_is_explicitly_limited(self):
        client = FakeClient(error=type("APITimeoutError", (Exception,), {})())
        provider = OpenAIProvider(
            api_key="test-key", client=client, sleep=lambda _: None, max_attempts=2, max_output_tokens=123
        )
        with self.assertRaises(AIServiceError):
            provider.interpret("synthetic")
        self.assertEqual(len(client.responses.calls), 2)

        successful = FakeClient(FakeResponse(self.payload()))
        OpenAIProvider(api_key="test-key", client=successful, max_output_tokens=123).interpret("synthetic")
        self.assertEqual(successful.responses.calls[0]["max_output_tokens"], 123)

    def test_provider_records_a_controlled_request_failure(self):
        provider = OpenAIProvider(
            api_key="test-key",
            client=FakeClient(FakeResponse("not json")),
            sleep=lambda _: None,
        )
        with self.assertRaises(AIServiceError):
            provider.interpret("synthetic")
        self.assertEqual(provider.availability_state, "request_failed")

    def test_explicit_connection_check_uses_only_models_metadata(self):
        client = FakeClient()
        provider = OpenAIProvider(api_key="test-key", client=client)
        provider.check_connection()
        self.assertEqual(client.models.calls, 1)
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(provider.availability_state, "available")

    def test_connection_check_maps_expected_models_api_failures(self):
        expected_errors = (
            ("AuthenticationError", "AI_AUTHENTICATION_FAILED"),
            ("PermissionDeniedError", "AI_PERMISSION_DENIED"),
            ("RateLimitError", "AI_RATE_LIMITED"),
            ("APITimeoutError", "AI_TIMEOUT"),
            ("APIConnectionError", "AI_NETWORK_UNAVAILABLE"),
            ("UnexpectedSDKError", "AI_PROVIDER_FAILURE"),
        )
        for name, expected in expected_errors:
            client = FakeClient(model_error=type(name, (Exception,), {})())
            with self.assertRaises(AIServiceError) as raised:
                OpenAIProvider(api_key="test-key", client=client).check_connection()
            self.assertEqual(raised.exception.code, expected)
            self.assertEqual(client.responses.calls, [])

    def test_connection_check_converts_client_setup_failure_to_a_controlled_error(self):
        class SetupFailureProvider(OpenAIProvider):
            def _create_client(self):
                raise RuntimeError("implementation detail that must not escape")

        with self.assertRaises(AIServiceError) as raised:
            SetupFailureProvider(api_key="test-key").check_connection()
        self.assertEqual(raised.exception.code, "AI_PROVIDER_FAILURE")
        self.assertNotIn("implementation detail", str(raised.exception))


class AIConnectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = StorageService(str(Path(self.temp.name) / "app")); self.storage.initialize()
        self.previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "GROUP_CLAIMS_AI_PROVIDER")}
        os.environ["OPENAI_API_KEY"] = "synthetic-secret"
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "openai"

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        self.temp.cleanup()

    def test_explicit_check_persists_a_non_sensitive_available_result(self):
        provider = type("ConnectionProvider", (), {"check_connection": lambda self: None})()
        service = AIService(self.storage, connection_provider_factory=lambda: provider)
        result = service.check_connection({})
        self.assertEqual(result["outcome"], "available")
        status = service.status()
        self.assertEqual(status["lastConnectionCheck"]["outcome"], "available")
        self.assertNotIn("synthetic-secret", str(status))
        assert self.storage.database
        with self.storage.database.connection() as connection:
            details = connection.execute("SELECT details_json FROM audit_events WHERE event_type = 'ai_connection_checked'").fetchone()[0]
        self.assertNotIn("synthetic-secret", details)
        self.assertIn('"document_content_sent":false', details)
        os.environ["GROUP_CLAIMS_AI_PROVIDER"] = "codex"
        self.assertIsNone(service.status()["lastConnectionCheck"])

    def test_explicit_check_reports_rejected_authentication_without_disclosing_the_key(self):
        def rejected():
            raise AIServiceError("AI_AUTHENTICATION_FAILED", "secret should not escape")
        provider = type("ConnectionProvider", (), {"check_connection": staticmethod(rejected)})()
        result = AIService(self.storage, connection_provider_factory=lambda: provider).check_connection({})
        self.assertEqual(result["outcome"], "authentication_rejected")

    def test_explicit_check_maps_all_expected_provider_errors_and_serializes_safely(self):
        expected = {
            "AI_AUTHENTICATION_FAILED": "authentication_rejected",
            "AI_PERMISSION_DENIED": "permission_denied",
            "AI_RATE_LIMITED": "rate_limited",
            "AI_TIMEOUT": "timeout",
            "AI_NETWORK_UNAVAILABLE": "network_unavailable",
            "AI_PROVIDER_FAILURE": "provider_error",
        }
        for code, outcome in expected.items():
            def failed(code=code):
                raise AIServiceError(code, "secret and provider internals must not escape")
            provider = type("ConnectionProvider", (), {"check_connection": staticmethod(failed)})()
            result = AIService(self.storage, connection_provider_factory=lambda provider=provider: provider).check_connection({})
            self.assertEqual(result["outcome"], outcome)
            self.assertNotIn("secret", json.dumps(result))

    def test_unexpected_connection_provider_failure_becomes_provider_error_not_protocol_failure(self):
        def broken_factory():
            raise RuntimeError("secret implementation failure")

        service = AIService(self.storage, connection_provider_factory=broken_factory)
        result = service.check_connection({})
        self.assertEqual(result["outcome"], "provider_error")
        self.assertNotIn("secret", json.dumps(result))
        status = service.status()
        self.assertEqual(status["lastConnectionCheck"]["outcome"], "provider_error")

    def test_legacy_generic_connection_audit_is_presented_as_provider_error(self):
        assert self.storage.database
        with self.storage.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (id, event_type, target_type, target_id, actor_type, occurred_at, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("legacy-check", "ai_connection_checked", "ai_provider", "openai", "user", "2026-10-15T00:00:00+00:00", '{\"outcome\":\"request_failed\"}'),
            )
        self.assertEqual(AIService(self.storage).status()["lastConnectionCheck"]["outcome"], "provider_error")


class AICostControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.pop("GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS", None)

    def tearDown(self) -> None:
        os.environ.pop("GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS", None)
        if self._previous is not None:
            os.environ["GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS"] = self._previous

    def test_codex_questions_get_a_larger_bounded_input_limit(self) -> None:
        self.assertEqual(AICostControls.from_environment("gpt-4.1-mini", "openai").max_input_characters, 4_000)
        self.assertEqual(AICostControls.from_environment(None, None).max_input_characters, 4_000)
        self.assertEqual(AICostControls.from_environment(None, "codex").max_input_characters, 12_000)
        # The explicit environment value still wins, within the same bounds.
        os.environ["GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS"] = "6000"
        self.assertEqual(AICostControls.from_environment(None, "codex").max_input_characters, 6_000)
        os.environ["GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS"] = "20000"
        with self.assertRaises(AIServiceError):
            AICostControls.from_environment(None, "codex")
