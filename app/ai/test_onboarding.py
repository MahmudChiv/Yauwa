"""Tests for trader lookup and the voice onboarding state transition."""

import importlib.util
import tempfile
from importlib.machinery import ModuleSpec
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.pool import StaticPool

import httpx
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.generation import NameExtraction, NameStatus
from app.ai.tts import register_media
from app.ai.onboarding import (
    _send_twilio_message,
    ensure_pending_trader,
    get_trader_by_phone,
    onboard_trader,
    onboarding_complete,
    send_onboarding_reply,
    trader_exists,
)
from app.models.trader import Trader
from app.routes.webhook import _process_incoming_message
from app.schemas.webhook import TwilioWebhookPayload
from main import app


class TraderLookupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_exact_match_requires_both_nonblank_fields(self) -> None:
        with Session(self.engine) as session:
            session.add(
                Trader(phone_number="+2348012345678", name="Amina", language="pidgin")
            )
            session.commit()
            self.assertTrue(
                trader_exists(session, " whatsapp:+2348012345678 ", " Amina ")
            )
            self.assertFalse(trader_exists(session, "+2348012345678", "Amira"))
            self.assertFalse(trader_exists(session, "+2348012345678", " "))

    def test_pending_and_completed_routing(self) -> None:
        with Session(self.engine) as session:
            pending = ensure_pending_trader(session, "+2348012345678")
            self.assertIsNone(pending.name)
            self.assertFalse(onboarding_complete(session, pending.phone_number))
            pending.name = "   "
            session.add(pending)
            session.commit()
            self.assertFalse(onboarding_complete(session, pending.phone_number))
            pending.name = "Amina"
            session.add(pending)
            session.commit()
            self.assertTrue(onboarding_complete(session, pending.phone_number))


class TwilioDeliveryTestCase(unittest.TestCase):
    def test_sender_and_recipient_are_whatsapp_addresses(self) -> None:
        settings = Mock()
        settings.twilio_account_sid = "AC-test"
        settings.twilio_auth_token.get_secret_value.return_value = "secret"
        settings.twilio_whatsapp_number = "+14155238886"
        client = Mock()
        with (
            patch("app.ai.onboarding.TwilioHttpClient"),
            patch("app.ai.onboarding.Client", return_value=client),
        ):
            _send_twilio_message(settings, "+2348000000001", body="Abeg try again")
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["from_"], "whatsapp:+14155238886")
        self.assertEqual(kwargs["to"], "whatsapp:+2348000000001")


class OnboardingFlowTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.engine_patch = patch(
            "app.ai.onboarding.get_engine", return_value=self.engine
        )
        self.engine_patch.start()
        self.settings = object()

    def tearDown(self) -> None:
        self.engine_patch.stop()
        self.engine.dispose()

    async def test_name_is_committed_before_confirmation(self) -> None:
        async def assert_saved(*args, **kwargs):
            with Session(self.engine) as session:
                trader = get_trader_by_phone(session, "+2348012345678")
                self.assertEqual(trader.name, "Amina")

        with (
            patch(
                "app.ai.onboarding.extract_stated_name",
                new=AsyncMock(
                    return_value=NameExtraction(
                        transcription="My name is Amina",
                        status=NameStatus.FOUND,
                        name=" Amina ",
                        name_evidence="My name is Amina",
                    )
                ),
            ),
            patch(
                "app.ai.onboarding.send_onboarding_reply",
                new=AsyncMock(side_effect=assert_saved),
            ) as send,
        ):
            result = await onboard_trader(
                "+2348012345678", "/tmp/input.ogg", "audio/ogg", self.settings
            )
        self.assertEqual(result, "onboarding complete")
        send.assert_awaited_once()

    async def test_missing_then_found_name_uses_same_pending_row(self) -> None:
        extraction = AsyncMock(
            side_effect=[
                NameExtraction(transcription="Hello Yauwa", status=NameStatus.MISSING),
                NameExtraction(
                    transcription="Bola",
                    status=NameStatus.FOUND,
                    name="Bola",
                    name_evidence="Bola",
                ),
            ]
        )
        with (
            patch("app.ai.onboarding.extract_stated_name", new=extraction),
            patch("app.ai.onboarding.send_onboarding_reply", new=AsyncMock()) as send,
        ):
            first = await onboard_trader(
                "+2348000000001", "/tmp/one.ogg", "audio/ogg", self.settings
            )
            second = await onboard_trader(
                "+2348000000001", "/tmp/two.ogg", "audio/ogg", self.settings
            )
        self.assertEqual(first, "onboarding pending")
        self.assertEqual(second, "onboarding complete")
        self.assertEqual(send.await_count, 2)
        with Session(self.engine) as session:
            traders = session.exec(select(Trader)).all()
            self.assertEqual(len(traders), 1)
            self.assertEqual(traders[0].name, "Bola")

    async def test_persisted_bot_name_is_cleared_and_onboarding_resumes(self) -> None:
        with Session(self.engine) as session:
            session.add(
                Trader(
                    phone_number="+2348000000005",
                    name="Yehwa",
                    language="pidgin",
                )
            )
            session.commit()
        with patch("app.ai.onboarding.send_onboarding_reply", new=AsyncMock()):
            result = await onboard_trader("+2348000000005", None, None, self.settings)
        self.assertEqual(result, "onboarding pending")
        with Session(self.engine) as session:
            trader = get_trader_by_phone(session, "+2348000000005")
            self.assertIsNone(trader.name)

    async def test_extraction_failure_generates_retry(self) -> None:
        with (
            patch(
                "app.ai.onboarding.extract_stated_name",
                new=AsyncMock(side_effect=RuntimeError("provider down")),
            ),
            patch("app.ai.onboarding.send_onboarding_reply", new=AsyncMock()),
        ):
            result = await onboard_trader(
                "+2348000000002", "/tmp/input.ogg", "audio/ogg", self.settings
            )
        self.assertEqual(result, "onboarding pending")

    async def test_text_fallback_when_tts_fails(self) -> None:
        settings = Mock()
        settings.public_base_url = "https://example.test"
        settings.twilio_webhook_url = "https://example.test/api/v1/webhook"
        send = unittest.mock.Mock()
        with (
            patch(
                "app.ai.onboarding.synthesize_speech",
                new=AsyncMock(side_effect=RuntimeError("tts down")),
            ),
            patch("app.ai.onboarding._send_twilio_message", new=send),
        ):
            await send_onboarding_reply("+2348000000003", "Abeg try again", settings)
        self.assertEqual(send.call_args.kwargs["body"], "Abeg try again")

    async def test_text_fallback_when_media_origin_is_misconfigured(self) -> None:
        settings = Mock()
        settings.public_base_url = "https://production.example"
        settings.twilio_webhook_url = "https://local.ngrok.dev/api/v1/webhook"
        synthesize = AsyncMock()
        send = unittest.mock.Mock(return_value="SM-text")
        with (
            patch("app.ai.onboarding.synthesize_speech", new=synthesize),
            patch("app.ai.onboarding._send_twilio_message", new=send),
        ):
            await send_onboarding_reply("+2348000000003", "Abeg try again", settings)

        synthesize.assert_not_awaited()
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["body"], "Abeg try again")

    async def test_completed_trader_skips_download_and_ai(self) -> None:
        with Session(self.engine) as session:
            session.add(
                Trader(phone_number="+2348000000004", name="Zainab", language="pidgin")
            )
            session.commit()
        payload = TwilioWebhookPayload.model_validate(
            {
                "From": "whatsapp:+2348000000004",
                "MessageSid": "SM-complete",
                "MediaUrl0": "https://api.twilio.com/audio",
                "MediaContentType0": "audio/ogg",
            }
        )
        with (
            patch("app.routes.webhook.get_engine", return_value=self.engine),
            patch("app.routes.webhook._download_audio", new=AsyncMock()) as download,
        ):
            result = await _process_incoming_message(payload, object())
        self.assertEqual(result, "user exist already")
        download.assert_not_awaited()


class TemporaryMediaTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_registered_media_supports_get_and_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reply.mp3"
            path.write_bytes(b"voice")
            token = await register_media(path)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                get_response = await client.get(f"/api/v1/media/{token}")
                head_response = await client.head(f"/api/v1/media/{token}")
            self.assertEqual(get_response.status_code, 200)
            self.assertEqual(get_response.content, b"voice")
            self.assertEqual(head_response.status_code, 200)
            self.assertEqual(head_response.content, b"")

    async def test_expired_media_is_removed(self) -> None:
        from app.ai import tts

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reply.mp3"
            path.write_bytes(b"voice")
            with patch.object(tts, "MEDIA_TTL_SECONDS", -1):
                token = await tts.register_media(path)
            self.assertIsNone(await tts.get_media(token))
            self.assertFalse(path.exists())


class MigrationTestCase(unittest.TestCase):
    def test_downgrade_refuses_pending_traders(self) -> None:
        path = Path("migrations/versions/748f62d467af_allow_pending_trader_name.py")
        spec = importlib.util.spec_from_file_location("pending_name_migration", path)
        self.assertIsInstance(spec, ModuleSpec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        binding = Mock()
        binding.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(module.op, "get_bind", return_value=binding),
            self.assertRaisesRegex(RuntimeError, "NULL names"),
        ):
            module.downgrade()


if __name__ == "__main__":
    unittest.main()
