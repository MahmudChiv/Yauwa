"""Strict schemas and provider configuration stay compatible with existing shapes."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from groq import BadRequestError

from app.services.extraction import EXTRACTION_RESPONSE_SCHEMA, PACKAGE_SIZE_RESPONSE_SCHEMA
from app.providers.generation import NameDecision
from app.providers.groq import complete, strict_schema
from app.core.config import Settings


class GroqContractTests(unittest.TestCase):
    def test_all_schema_objects_are_closed_and_required_without_mutating_input(self):
        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        for schema in (EXTRACTION_RESPONSE_SCHEMA, PACKAGE_SIZE_RESPONSE_SCHEMA,
                       NameDecision.model_json_schema()):
            before = deepcopy(schema)
            result = strict_schema(schema)
            check(result)
            self.assertEqual(schema, before)
            self.assertEqual(set(result["properties"]), set(schema["properties"]))
        result = strict_schema(EXTRACTION_RESPONSE_SCHEMA)
        self.assertEqual(result["properties"]["reply_text"]["type"], ["string", "null"])

    def test_settings_replace_only_non_transcription_models(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(
                _env_file=None, database_url="postgresql://test:test@localhost/test",
                twilio_account_sid="test", twilio_auth_token="test",
                twilio_whatsapp_number="+2348000000000",
                twilio_webhook_url="https://example.test/api/v1/webhook",
                public_base_url="https://example.test", gemini_api_key="gemini-secret",
                gemini_transcription_model="unchanged-transcription",
                groq_api_key="groq-secret", elevenlabs_api_key="test",
                elevenlabs_voice_id="test",
            )
        self.assertEqual(settings.groq_model, "openai/gpt-oss-120b")
        self.assertEqual(settings.groq_extraction_model, "openai/gpt-oss-20b")
        self.assertEqual(settings.gemini_transcription_model, "unchanged-transcription")
        self.assertNotIn("groq-secret", repr(settings))
        self.assertNotIn("gemini_model", Settings.model_fields)
        self.assertNotIn("gemini_extraction_model", Settings.model_fields)


class GroqCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Mock()
        self.settings.groq_api_key.get_secret_value.return_value = "secret"
        self.client = Mock()
        self.client.chat.completions.create = AsyncMock()
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=False)
        self.factory = self.enterContext(
            patch("app.providers.groq.AsyncGroq", return_value=self.client)
        )

    @staticmethod
    def completion(content="{}"): 
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=content, refusal=None),
        )])

    @staticmethod
    def schema_error():
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        response = httpx.Response(400, request=request)
        return BadRequestError(
            "Failed to validate JSON",
            response=response,
            body={"error": {"code": "json_validate_failed"}},
        )

    async def test_schema_validation_failure_retries_once_on_same_model(self):
        self.client.chat.completions.create.side_effect = [
            self.schema_error(), self.completion('{"value":"ok"}')
        ]
        result = await complete(
            self.settings,
            model="extract-model",
            messages=[{"role": "user", "content": "extract"}],
            timeout=5,
            temperature=0,
            schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self.assertEqual(result, '{"value":"ok"}')
        self.assertEqual(self.client.chat.completions.create.await_count, 2)
        self.assertEqual(
            {call.kwargs["model"] for call in self.client.chat.completions.create.await_args_list},
            {"extract-model"},
        )

    async def test_plain_text_generation_does_not_retry_bad_request(self):
        self.client.chat.completions.create.side_effect = self.schema_error()
        with self.assertRaises(BadRequestError):
            await complete(
                self.settings,
                model="reply-model",
                messages=[{"role": "user", "content": "reply"}],
                timeout=5,
                temperature=0.4,
            )
        self.client.chat.completions.create.assert_awaited_once()
