"""Offline regression coverage for the isolated hackathon walkthrough."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from twilio.request_validator import RequestValidator

from app.config import DemoSettings, TwilioSettings
from app.demo import DEMO_DIRECTORY, DemoPlayer
from app.routes.webhook import _seen_messages
from main import app

PHONE = "+2348164247735"
URL = "https://example.test/api/v1/webhook"


class DemoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        shutil.copy(DEMO_DIRECTORY / "script.json", self.directory / "script.json")
        (self.directory / "audio").mkdir()
        self.settings = DemoSettings(
            _env_file=None, demo_mode=True, demo_phone_number=PHONE,
            twilio_account_sid="AC-test", twilio_auth_token="test-token",
            twilio_whatsapp_number="+14155550123", twilio_webhook_url=URL,
            public_base_url="https://example.test",
        )
        self.script = json.loads((self.directory / "script.json").read_text())
        self.files = [f for s in self.script for f in s.get("files", [s.get("file")])]
        for filename in self.files:
            (self.directory / "audio" / filename).write_bytes(b"ID3-test-audio")
        self.player = DemoPlayer(self.settings, self.directory)
        self.player_patch = patch("app.demo._player", self.player)
        self.player_patch.start()
        self.addCleanup(self.player_patch.stop)
        self.twilio_patch = patch("app.routes.webhook.get_twilio_settings", return_value=TwilioSettings(
            _env_file=None, twilio_account_sid="AC-test", twilio_auth_token="test-token", twilio_webhook_url=URL,
        ))
        self.twilio_patch.start()
        self.addCleanup(self.twilio_patch.stop)
        _seen_messages.clear()
        self.addCleanup(_seen_messages.clear)

    async def request(self, path, fields=None, valid=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            if fields is None:
                return await client.get(path)
            signature = RequestValidator("test-token").compute_signature(URL, fields) if valid else "bad"
            return await client.post(path, data=fields, headers={"X-Twilio-Signature": signature})

    async def post(self, sid="SM1", sender=PHONE, valid=True):
        return await self.request("/api/v1/webhook", {
            "From": "whatsapp:" + sender, "MessageSid": sid, "Body": "content is ignored",
            "MediaUrl0": "https://not-downloaded.test/audio", "MediaContentType0": "audio/ogg",
        }, valid=valid)

    async def test_entire_script_bypasses_real_pipeline_and_suppresses_duplicates(self):
        with patch("app.demo._send_twilio_message", return_value="SM-out") as send, \
                patch("app.demo.asyncio.sleep", new_callable=AsyncMock) as sleep, \
                patch("app.routes.webhook._process_incoming_message", new_callable=AsyncMock) as process, \
                patch("app.routes.webhook._download_audio", new_callable=AsyncMock) as download, \
                patch("app.routes.webhook.get_engine") as database, \
                patch("app.routes.webhook.get_settings") as providers:
            for i in range(6):
                self.assertEqual((await self.post(f"SM{i}")).status_code, 200)
                self.assertEqual((await self.post(f"SM{i}")).status_code, 200)
            self.assertEqual((await self.post("SM-completed")).status_code, 200)
            self.assertEqual([c.kwargs["media_url"].rsplit("/", 1)[-1] for c in send.call_args_list], self.files)
            self.assertEqual(send.call_count, 12)
            self.assertEqual(sleep.await_count, 6)
            self.assertTrue(all(c.args == (1.0,) for c in sleep.call_args_list))
            process.assert_not_called()
            download.assert_not_called()
            database.assert_not_called()
            providers.assert_not_called()
        self.assertTrue(self.player.status()["completed"])

    async def test_other_number_and_disabled_flag_use_real_route(self):
        with patch("app.routes.webhook._process_incoming_message", new_callable=AsyncMock) as process, \
                patch("app.demo._send_twilio_message") as send:
            self.assertEqual((await self.post(sender=PHONE + "1")).status_code, 200)
            self.settings.demo_mode = False
            self.assertEqual((await self.post("SM2")).status_code, 200)
            self.assertEqual(process.await_count, 2)
            send.assert_not_called()
        for path in ["/demo/status", "/demo/reset", "/demo/audio/00_welcome.mp3"]:
            self.assertEqual((await self.request(path)).status_code, 404)

    async def test_signature_rejected_before_demo(self):
        with patch("app.demo._send_twilio_message") as send:
            self.assertEqual((await self.post(valid=False)).status_code, 403)
            send.assert_not_called()
        self.assertEqual(self.player.index, 0)

    async def test_missing_audio_blocks_without_consuming_message(self):
        path = self.directory / "audio" / self.files[0]
        path.unlink()
        self.assertEqual((await self.post()).status_code, 503)
        self.assertNotIn("SM1", _seen_messages)
        self.assertEqual((await self.request("/demo/status")).json()["missing_files"], [self.files[0]])
        path.write_bytes(b"audio")
        with patch("app.demo._send_twilio_message", return_value="SM-out"):
            self.assertEqual((await self.post()).status_code, 200)
        self.assertEqual(self.player.index, 1)

    async def test_partial_failure_pauses_until_reset(self):
        self.player.index = 2
        with patch("app.demo._send_twilio_message", side_effect=["SM-ok", TimeoutError("private details")]) as send, \
                patch("app.demo.asyncio.sleep", new_callable=AsyncMock):
            await self.player.play()
            await self.player.play()
            self.assertEqual(send.call_count, 2)
        self.assertEqual(self.player.index, 2)
        state = (await self.request("/demo/status")).json()
        self.assertEqual(state["delivery_failure"], {"step": 2, "file": self.files[3], "error": "TimeoutError"})
        self.assertFalse(state["ready"])
        reset = (await self.request("/demo/reset")).json()
        self.assertEqual(reset["current_step"], 0)
        self.assertIsNone(reset["delivery_failure"])
        self.assertTrue(reset["ready"])

    async def test_concurrent_playback_is_serialized(self):
        self.player.index = 2
        with patch("app.demo._send_twilio_message", return_value="SM-out") as send, \
                patch("app.demo.asyncio.sleep", new_callable=AsyncMock):
            await asyncio.gather(self.player.play(), self.player.play())
        self.assertEqual(self.player.index, 4)
        self.assertEqual([c.kwargs["media_url"].rsplit("/", 1)[-1] for c in send.call_args_list], self.files[2:6])

    async def test_media_is_allowlisted_nonempty_and_confined(self):
        response = await self.request("/demo/audio/00_welcome.mp3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.content, b"ID3-test-audio")
        self.assertEqual((await self.request("/demo/audio/unlisted.mp3")).status_code, 404)
        path = self.directory / "audio" / self.files[0]
        path.write_bytes(b"")
        self.assertIsNone(self.player.audio_path(self.files[0]))
        path.unlink()
        path.symlink_to(self.directory / "script.json")
        self.assertEqual((await self.request("/demo/audio/00_welcome.mp3")).status_code, 404)
        self.assertIsNone(self.player.audio_path("../script.json"))

    async def test_script_validation_and_array_order(self):
        for data in ["{", "{}", "[]", json.dumps([{"step": 0, "files": ["a.mp3"], "text": []}]),
                     json.dumps([{"step": 0, "file": "../a.mp3", "text": "x"}]),
                     json.dumps([{"step": 0, "file": "a.mp3", "files": [], "text": "x"}])]:
            with self.subTest(data=data):
                (self.directory / "script.json").write_text(data)
                player = DemoPlayer(self.settings, self.directory)
                self.assertFalse(player.status()["ready"])
                self.assertIsNotNone(player.configuration_error)
        (self.directory / "script.json").write_text(json.dumps(list(reversed(self.script))))
        player = DemoPlayer(self.settings, self.directory)
        self.assertEqual(player.steps[0], ["05_restock_confirmed.mp3"])
        self.assertEqual(player.index, 0)

    async def test_disabled_demo_needs_no_files_or_credentials(self):
        settings = DemoSettings(_env_file=None)
        player = DemoPlayer(settings, self.directory / "nonexistent")
        self.assertFalse(player.matches(PHONE))
        self.assertIsNone(player.configuration_error)
        self.assertFalse(player.status()["ready"])

    async def test_lifespan_and_health_without_provider_credentials(self):
        with patch("app.demo.DemoSettings", return_value=DemoSettings(_env_file=None)), \
                patch("main.cleanup_expired_media", new_callable=AsyncMock):
            async with app.router.lifespan_context(app):
                self.assertEqual((await self.request("/health")).json(), {"status": "healthy"})
                self.assertEqual((await self.request("/demo/status")).status_code, 404)

    async def test_missing_assets_do_not_block_other_callers_or_health(self):
        (self.directory / "audio" / self.files[0]).unlink()
        with patch("app.routes.webhook._process_incoming_message", new_callable=AsyncMock) as process:
            self.assertEqual((await self.post(sender="+2348012345678")).status_code, 200)
            process.assert_awaited_once()
        self.assertEqual((await self.request("/health")).status_code, 200)
