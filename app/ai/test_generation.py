"""Focused tests for the native async Gemini integration."""

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


class GeminiGenerationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Mock()
        self.settings.gemini_model = "gemini-3.5-flash-lite"
        self.settings.gemini_transcription_model = "gemini-3.5-transcribe"
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

    async def test_name_extraction_uses_async_sdk_and_disables_afc(self) -> None:
        self.client.aio.models.generate_content.return_value = SimpleNamespace(
            parsed={
                "status": "found",
                "name": "  Amina  ",
                "name_evidence": "My name is Amina",
            }, text=None
        )
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
        decision_call = self.client.aio.models.generate_content.await_args.kwargs
        self.assertEqual(decision_call["model"], self.settings.gemini_model)
        config = decision_call["config"]
        self.assertTrue(config.automatic_function_calling.disable)
        self.client.aio.files.delete.assert_awaited_once_with(name="files/test")
        self.client.aio.aclose.assert_awaited_once()

    async def test_reply_generation_uses_async_sdk_and_disables_afc(self) -> None:
        self.client.aio.models.generate_content.return_value = SimpleNamespace(
            text="  Welcome!  Wetin be your name?  "
        )
        with patch("app.ai.generation._gemini_client", return_value=self.client):
            result = await generate_onboarding_reply("ask_name", self.settings)

        self.assertEqual(result, "Welcome! Wetin be your name?")
        config = self.client.aio.models.generate_content.await_args.kwargs["config"]
        self.assertTrue(config.automatic_function_calling.disable)
        self.client.aio.aclose.assert_awaited_once()


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
