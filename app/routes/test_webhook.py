"""Focused tests for the signed Twilio webhook and temporary audio downloads."""

import asyncio
import os
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from twilio.request_validator import RequestValidator  # type: ignore[reportMissingImports]

from app.config import TwilioSettings, get_twilio_settings
from app.routes.webhook import (
    MAX_AUDIO_BYTES,
    _download_audio,
    _process_incoming_message,
    _seen_messages,
)
from app.schemas.webhook import TwilioWebhookPayload
from main import app

ACCOUNT_SID = "AC11111111111111111111111111111111"
AUTH_TOKEN = "test-auth-token"
WEBHOOK_URL = "https://example.test/api/v1/webhook"
MEDIA_URL = (
    "https://api.twilio.com/2010-04-01/Accounts/"
    f"{ACCOUNT_SID}/Messages/MM222/Media/ME333"
)


class ChunkStream(httpx.AsyncByteStream):
    """Yield controlled chunks without adding a Content-Length header."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class WebhookTestCase(unittest.IsolatedAsyncioTestCase):
    """Exercise authentication, acknowledgement, and background dispatch."""

    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "TWILIO_ACCOUNT_SID": ACCOUNT_SID,
                "TWILIO_AUTH_TOKEN": AUTH_TOKEN,
                "TWILIO_WEBHOOK_URL": WEBHOOK_URL,
            },
            clear=True,
        )
        self.environment.start()
        get_twilio_settings.cache_clear()
        _seen_messages.clear()

    def tearDown(self) -> None:
        get_twilio_settings.cache_clear()
        _seen_messages.clear()
        self.environment.stop()

    @staticmethod
    def signed_headers(fields: dict[str, str]) -> dict[str, str]:
        signature = RequestValidator(AUTH_TOKEN).compute_signature(WEBHOOK_URL, fields)
        return {"X-Twilio-Signature": signature}

    async def post(self, fields: dict[str, str], signature_fields=None) -> httpx.Response:
        fields = {"MessageSid": "SM-test-message", **fields}
        headers = self.signed_headers(signature_fields or fields)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/api/v1/webhook", data=fields, headers=headers)

    async def test_acknowledges_and_dispatches_in_background(self) -> None:
        fields = {"From": "whatsapp:+2348012345678", "Body": "How far?"}
        with patch(
            "app.routes.webhook._process_incoming_message",
            new=AsyncMock(return_value="onboarding pending"),
        ) as process:
            response = await self.post(fields)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "<Response></Response>")
        self.assertTrue(response.headers["content-type"].startswith("application/xml"))
        process.assert_awaited_once()

    async def test_duplicate_message_sid_is_dispatched_once(self) -> None:
        fields = {"From": "whatsapp:+2348012345678"}
        with patch(
            "app.routes.webhook._process_incoming_message",
            new=AsyncMock(return_value="onboarding pending"),
        ) as process:
            first = await self.post(fields)
            second = await self.post(fields)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        process.assert_awaited_once()

    async def test_signature_covers_extra_form_fields(self) -> None:
        fields = {"From": "whatsapp:+2348012345678", "Body": "signed too"}
        response = await self.post(fields, {"From": fields["From"]})
        self.assertEqual(response.status_code, 403)

    async def test_missing_or_invalid_signature_is_forbidden(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/webhook",
                data={"From": "whatsapp:+2348012345678", "MessageSid": "SM1"},
                headers={"X-Twilio-Signature": "not-valid"},
            )
        self.assertEqual(response.status_code, 403)

    async def test_missing_twilio_configuration_is_unavailable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            try:
                TwilioSettings(_env_file=None)
            except ValidationError as configuration_error:
                error = configuration_error
            else:
                self.fail("Empty Twilio settings unexpectedly validated")

        with patch("app.routes.webhook.get_twilio_settings", side_effect=error):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/webhook", data={"From": "+234"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(AUTH_TOKEN, response.text)

    async def test_payload_errors_are_rejected(self) -> None:
        response = await self.post({"Body": "hello"})
        self.assertEqual(response.status_code, 422)

    async def test_non_audio_media_is_accepted_for_voice_prompt(self) -> None:
        fields = {
            "From": "whatsapp:+2348012345678",
            "MediaUrl0": MEDIA_URL,
            "MediaContentType0": "image/jpeg",
        }
        with patch(
            "app.routes.webhook._process_incoming_message",
            new=AsyncMock(return_value="onboarding pending"),
        ) as process:
            response = await self.post(fields)
        self.assertEqual(response.status_code, 200)
        process.assert_awaited_once()


class TraderRoutingTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.twilio_settings = TwilioSettings(
            twilio_account_sid=ACCOUNT_SID,
            twilio_auth_token=AUTH_TOKEN,
            twilio_webhook_url=WEBHOOK_URL,
        )
        self.payload = TwilioWebhookPayload.model_validate(
            {
                "From": "whatsapp:+2348012345678",
                "MessageSid": "SM-routing-test",
                "MediaUrl0": MEDIA_URL,
                "MediaContentType0": "audio/ogg",
            }
        )
        self.settings = Mock()

    async def test_onboarded_audio_reaches_extraction_and_is_cleaned(self) -> None:
        with (
            patch("app.routes.webhook.Session"),
            patch("app.routes.webhook.get_engine"),
            patch("app.routes.webhook.onboarding_complete", return_value=True),
            patch("app.routes.webhook.get_settings", return_value=self.settings),
            patch(
                "app.routes.webhook._download_audio",
                new=AsyncMock(return_value="/tmp/routing-test.ogg"),
            ) as download,
            patch(
                "app.routes.webhook.process_trader_audio",
                new=AsyncMock(return_value={"intent": "stock_intake"}),
            ) as extract,
            patch(
                "app.routes.webhook.onboard_trader",
                new=AsyncMock(),
            ) as onboard,
            patch(
                "app.routes.webhook._remove_file",
                new=AsyncMock(),
            ) as cleanup,
            patch("builtins.print"),
        ):
            result = await _process_incoming_message(
                self.payload, self.twilio_settings
            )

        self.assertEqual(result, "extraction processed")
        download.assert_awaited_once_with(MEDIA_URL, "audio/ogg", self.twilio_settings)
        extract.assert_awaited_once_with(
            "+2348012345678",
            "/tmp/routing-test.ogg",
            "audio/ogg",
            self.settings,
        )
        onboard.assert_not_awaited()
        cleanup.assert_awaited_once_with(Path("/tmp/routing-test.ogg"))

    async def test_incomplete_trader_still_uses_onboarding(self) -> None:
        with (
            patch("app.routes.webhook.Session"),
            patch("app.routes.webhook.get_engine"),
            patch("app.routes.webhook.onboarding_complete", return_value=False),
            patch("app.routes.webhook.get_settings", return_value=self.settings),
            patch(
                "app.routes.webhook._download_audio",
                new=AsyncMock(return_value="/tmp/routing-test.ogg"),
            ),
            patch(
                "app.routes.webhook.process_trader_audio",
                new=AsyncMock(),
            ) as extract,
            patch(
                "app.routes.webhook.onboard_trader",
                new=AsyncMock(return_value="onboarding pending"),
            ) as onboard,
            patch("app.routes.webhook._remove_file", new=AsyncMock()),
        ):
            result = await _process_incoming_message(
                self.payload, self.twilio_settings
            )

        self.assertEqual(result, "onboarding pending")
        onboard.assert_awaited_once_with(
            "+2348012345678",
            "/tmp/routing-test.ogg",
            "audio/ogg",
            self.settings,
        )
        extract.assert_not_awaited()

    async def test_onboarded_text_requests_voice_note(self) -> None:
        payload = self.payload.model_copy(
            update={"media_url": None, "content_type": None}
        )
        with (
            patch("app.routes.webhook.Session"),
            patch("app.routes.webhook.get_engine"),
            patch("app.routes.webhook.onboarding_complete", return_value=True),
            patch("app.routes.webhook.get_settings", return_value=self.settings),
            patch("app.routes.webhook.process_trader_audio", new=AsyncMock()) as extract,
            patch(
                "app.routes.webhook.send_onboarding_reply",
                new=AsyncMock(),
            ) as reply,
            patch("builtins.print"),
        ):
            result = await _process_incoming_message(payload, self.twilio_settings)

        self.assertEqual(result, "extraction processed")
        extract.assert_not_awaited()
        self.assertIn("voice note", reply.await_args.args[1])

class AudioDownloadTestCase(unittest.IsolatedAsyncioTestCase):
    """Exercise streaming, authentication, limits, and temporary-file cleanup."""

    def setUp(self) -> None:
        self.settings = TwilioSettings(
            twilio_account_sid=ACCOUNT_SID,
            twilio_auth_token=AUTH_TOKEN,
            twilio_webhook_url=WEBHOOK_URL,
        )
        self.temp_directory = tempfile.TemporaryDirectory(prefix="yauwa-tests-")
        original_named_temporary_file = tempfile.NamedTemporaryFile
        factory = partial(
            original_named_temporary_file,
            dir=self.temp_directory.name,
        )
        self.temp_patch = patch(
            "app.routes.webhook.tempfile.NamedTemporaryFile",
            side_effect=factory,
        )
        self.temp_patch.start()

    def tearDown(self) -> None:
        self.temp_patch.stop()
        self.temp_directory.cleanup()

    async def test_authenticated_redirect_drops_auth_across_origins(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "api.twilio.com":
                return httpx.Response(
                    302,
                    headers={"Location": "https://cdn.example.test/audio"},
                    request=request,
                )
            return httpx.Response(200, content=b"voice", request=request)

        path = await _download_audio(
            MEDIA_URL,
            "audio/ogg",
            self.settings,
            transport=httpx.MockTransport(handler),
        )
        try:
            self.assertEqual(Path(path).read_bytes(), b"voice")
            self.assertTrue(requests[0].headers.get("Authorization", "").startswith("Basic "))
            self.assertNotIn("Authorization", requests[1].headers)
        finally:
            Path(path).unlink(missing_ok=True)

    async def test_oversized_partial_file_is_removed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=ChunkStream(b"first", b"x" * MAX_AUDIO_BYTES),
                request=request,
            )

        with self.assertRaises(HTTPException) as caught:
            await _download_audio(
                MEDIA_URL,
                "audio/ogg",
                self.settings,
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(list(Path(self.temp_directory.name).iterdir()), [])

    async def test_empty_failed_and_timed_out_downloads(self) -> None:
        cases = [
            (httpx.Response(200, content=b""), 502),
            (httpx.Response(500, content=b"private upstream body"), 502),
        ]
        for upstream_response, expected_status in cases:
            async def handler(request, response=upstream_response):
                response.request = request
                return response

            with self.assertRaises(HTTPException) as caught:
                await _download_audio(
                    MEDIA_URL,
                    "audio/ogg",
                    self.settings,
                    transport=httpx.MockTransport(handler),
                )
            self.assertEqual(caught.exception.status_code, expected_status)
            self.assertNotIn("private upstream body", caught.exception.detail)

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("late", request=request)

        with self.assertRaises(HTTPException) as caught:
            await _download_audio(
                MEDIA_URL,
                "audio/ogg",
                self.settings,
                transport=httpx.MockTransport(timeout_handler),
            )
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(list(Path(self.temp_directory.name).iterdir()), [])

    async def test_local_file_creation_failure_returns_500(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"voice", request=request)

        with (
            patch(
                "app.routes.webhook.tempfile.NamedTemporaryFile",
                side_effect=OSError("disk unavailable"),
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            await _download_audio(
                MEDIA_URL,
                "audio/ogg",
                self.settings,
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(caught.exception.status_code, 500)
        self.assertNotIn("disk unavailable", caught.exception.detail)

    async def test_concurrent_downloads_get_unique_paths(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"voice", request=request)

        transport = httpx.MockTransport(handler)
        paths = await asyncio.gather(
            _download_audio(MEDIA_URL, "audio/ogg", self.settings, transport=transport),
            _download_audio(MEDIA_URL, "audio/ogg", self.settings, transport=transport),
        )
        self.assertEqual(len(set(paths)), 2)
        for path in paths:
            Path(path).unlink(missing_ok=True)

    async def test_rejects_media_url_for_another_account(self) -> None:
        wrong_url = MEDIA_URL.replace(ACCOUNT_SID, "AC99999999999999999999999999999999")
        with self.assertRaises(HTTPException) as caught:
            await _download_audio(wrong_url, "audio/ogg", self.settings)
        self.assertEqual(caught.exception.status_code, 422)


class SchemaAndSettingsTestCase(unittest.TestCase):
    """Verify aliases, normalization, and focused settings isolation."""

    def test_schema_aliases_normalize_empty_values(self) -> None:
        payload = TwilioWebhookPayload.model_validate(
            {
                "From": " whatsapp:+2348012345678 ",
                "MessageSid": " SM-schema ",
                "MediaUrl0": " ",
                "MediaContentType0": "",
                "Body": "ignored",
            }
        )
        self.assertEqual(payload.sender, "whatsapp:+2348012345678")
        self.assertEqual(payload.message_sid, "SM-schema")
        self.assertIsNone(payload.media_url)
        self.assertIsNone(payload.content_type)

    def test_twilio_settings_do_not_require_other_services(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWILIO_ACCOUNT_SID": ACCOUNT_SID,
                "TWILIO_AUTH_TOKEN": AUTH_TOKEN,
                "TWILIO_WEBHOOK_URL": WEBHOOK_URL,
            },
            clear=True,
        ):
            get_twilio_settings.cache_clear()
            settings = get_twilio_settings()
            self.assertEqual(settings.twilio_account_sid, ACCOUNT_SID)
            self.assertEqual(str(settings.twilio_webhook_url), WEBHOOK_URL)
            get_twilio_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
