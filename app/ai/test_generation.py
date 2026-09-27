"""Tests for Gemini transcription followed by Groq extraction and replies."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.ai.generation import (
    NameStatus,
    extract_stated_name,
    generate_onboarding_reply,
)


def _completion(text, finish_reason="stop", refusal=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=text, refusal=refusal),
    )])


class GroqGenerationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Mock()
        self.settings.groq_model = "reply-test"
        self.settings.groq_extraction_model = "extract-test"
        self.settings.groq_api_key.get_secret_value.return_value = "groq-secret"
        self.settings.gemini_transcription_model = "transcription-test"
        self.settings.gemini_api_key.get_secret_value.return_value = "secret"
        self.client = Mock()
        self.client.aio.files.upload = AsyncMock(
            return_value=SimpleNamespace(
                name="files/test",
                uri="https://generativelanguage.googleapis.com/files/test",
                mime_type="audio/ogg",
            )
        )
        self.client.aio.files.delete = AsyncMock()
        self.client.aio.interactions.create = AsyncMock(
            return_value=SimpleNamespace(output_text="My name is Amina")
        )
        self.client.aio.models.generate_content = AsyncMock()
        self.client.aio.aclose = AsyncMock()
        self.groq = Mock()
        self.groq.chat.completions.create = AsyncMock()
        self.groq.__aenter__ = AsyncMock(return_value=self.groq)
        self.groq.__aexit__ = AsyncMock(return_value=False)
        self.factory = self.enterContext(patch("app.ai.groq_client.AsyncGroq", return_value=self.groq))

    async def test_name_extraction_preserves_transcription_and_uses_groq_schema(self) -> None:
        self.groq.chat.completions.create.return_value = _completion(json.dumps({
            "status": "found", "name": "  Amina  ", "name_evidence": "My name is Amina",
        }))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch("app.ai.generation._gemini_client", return_value=self.client):
                result = await extract_stated_name(
                    str(path), "audio/ogg", self.settings
                )

        self.assertEqual(result.status, NameStatus.FOUND)
        self.assertEqual(result.name, "Amina")
        transcription_call = self.client.aio.interactions.create.await_args.kwargs
        self.assertEqual(
            transcription_call["model"], self.settings.gemini_transcription_model
        )
        self.assertEqual(
            transcription_call["input"],
            [
                {
                    "type": "audio",
                    "uri": "https://generativelanguage.googleapis.com/files/test",
                    "mime_type": "audio/ogg",
                }
            ],
        )
        transcription_config = transcription_call["generation_config"][
            "transcription_config"
        ]
        self.assertEqual(transcription_config["language_codes"], ["en-GB"])
        self.assertIn("Yauwa", transcription_config["custom_vocabulary"])
        self.assertIn("how far", transcription_config["custom_vocabulary"])
        self.assertEqual(transcription_config["mode"], {"type": "verbatim"})
        decision_call = self.groq.chat.completions.create.await_args.kwargs
        self.assertEqual(decision_call["model"], self.settings.groq_extraction_model)
        self.assertIn("<transcript>My name is Amina</transcript>", decision_call["messages"][0]["content"])
        schema = decision_call["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertEqual(set(schema["schema"]["required"]), {"status", "name", "name_evidence"})
        self.client.aio.models.generate_content.assert_not_awaited()
        self.groq.chat.completions.create.assert_awaited_once()
        self.groq.__aexit__.assert_awaited_once()
        self.client.aio.files.delete.assert_awaited_once_with(name="files/test")
        self.client.aio.aclose.assert_awaited_once()

    async def test_reply_generation_uses_plain_groq_completion(self) -> None:
        self.groq.chat.completions.create.return_value = _completion(
            text="  Welcome!  Wetin be your name?  "
        )
        with patch("app.ai.generation._gemini_client", return_value=self.client):
            result = await generate_onboarding_reply("ask_name", self.settings)

        self.assertEqual(result, "Welcome! Wetin be your name?")
        call = self.groq.chat.completions.create.await_args.kwargs
        self.assertEqual(call["model"], "reply-test")
        self.assertEqual(call["temperature"], 0.4)
        self.assertNotIn("response_format", call)
        self.assertEqual(call["messages"], [{"role": "user", "content":
            "Reply only in friendly Nigerian Pidgin. Keep it warm, harmless, clear, "
            "and under 35 words. Welcome the trader and ask for their name."}])
        self.factory.assert_called_once_with(api_key="groq-secret", timeout=10, max_retries=0)
        self.groq.chat.completions.create.assert_awaited_once()
        self.groq.__aexit__.assert_awaited_once()
        self.client.aio.models.generate_content.assert_not_awaited()
        self.client.aio.files.upload.assert_not_awaited()

    async def test_name_failure_cleans_both_clients(self):
        for response in (_completion("not json"), _completion('{"status":"invalid"}')):
            with self.subTest(response=response):
                self.groq.chat.completions.create.reset_mock()
                self.groq.__aexit__.reset_mock()
                self.client.aio.files.delete.reset_mock()
                self.client.aio.aclose.reset_mock()
                self.groq.chat.completions.create.return_value = response
                with patch("app.ai.generation._gemini_client", return_value=self.client):
                    with self.assertRaises(ValueError):
                        await extract_stated_name("voice.ogg", "audio/ogg", self.settings)
                self.groq.chat.completions.create.assert_awaited_once()
                self.groq.__aexit__.assert_awaited_once()
                self.client.aio.files.delete.assert_awaited_once()
                self.client.aio.aclose.assert_awaited_once()
                self.client.aio.models.generate_content.assert_not_awaited()

    async def test_reply_failure_closes_client_without_gemini(self):
        for response in (_completion(""), _completion("Abeg", "length"), _completion("No", refusal="No")):
            with self.subTest(response=response):
                self.groq.chat.completions.create.reset_mock()
                self.groq.__aexit__.reset_mock()
                self.groq.chat.completions.create.return_value = response
                with self.assertRaises(ValueError):
                    await generate_onboarding_reply("ask_name", self.settings)
                self.groq.chat.completions.create.assert_awaited_once()
                self.groq.__aexit__.assert_awaited_once()
                self.client.aio.models.generate_content.assert_not_awaited()

    async def test_name_nullable_fields_remain_valid(self):
        self.groq.chat.completions.create.return_value = _completion(
            '{"status":"missing","name":null,"name_evidence":null}'
        )
        with patch("app.ai.generation._gemini_client", return_value=self.client):
            result = await extract_stated_name("voice.ogg", "audio/ogg", self.settings)
        self.assertEqual(result.status, NameStatus.MISSING)
        self.assertIsNone(result.name)
        self.assertIsNone(result.name_evidence)



if __name__ == "__main__":
    unittest.main()

class NameValidationTestCase(unittest.TestCase):
    def test_bot_name_variant_is_not_accepted_as_trader_name(self) -> None:
        from app.ai.generation import NameExtraction, _clean_name

        result = _clean_name(
            NameExtraction(
                transcription="Hello, how far Yauwa",
                status=NameStatus.FOUND,
                name="Yehwa",
                name_evidence="Yehwa",
            )
        )
        self.assertEqual(result.status, NameStatus.MISSING)
        self.assertIsNone(result.name)
