"""Isolated, in-memory prerecorded demo. No AI, TTS, or ledger operations."""

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.ai.onboarding import _send_twilio_message, normalize_phone_number
from app.config import DemoSettings

logger = logging.getLogger(__name__)
DEMO_DIRECTORY = Path(__file__).resolve().parent.parent / "demo"
SEND_DELAY_SECONDS = 1.0
router = APIRouter(prefix="/demo", tags=["Demo"])


class DemoPlayer:
    def __init__(self, settings: DemoSettings, directory: Path = DEMO_DIRECTORY):
        self.settings = settings
        self.directory = directory
        self.steps: list[list[str]] = []
        self.index = 0
        self.failure: dict | None = None
        self.configuration_error: str | None = None
        self.lock = asyncio.Lock()
        if settings.demo_mode:
            try:
                self._load()
            except (ValueError, OSError, TypeError, KeyError):
                self.configuration_error = "Invalid demo configuration or script; check demo settings and script.json."
                logger.error(self.configuration_error)
            state = self.status()
            if not state["ready"]:
                logger.error("Demo is not ready: %s", state)

    def _load(self) -> None:
        s = self.settings
        if not re.fullmatch(r"\+[1-9]\d{7,14}", normalize_phone_number(s.demo_phone_number)):
            raise ValueError("Demo phone must be E.164")
        if not (s.twilio_account_sid and s.twilio_auth_token.get_secret_value() and s.twilio_whatsapp_number):
            raise ValueError("Missing demo delivery settings")
        public, webhook = urlsplit(s.public_base_url), urlsplit(s.twilio_webhook_url)
        if (public.scheme != "https" or not public.hostname or public.username
                or public.password or public.query or public.fragment
                or (public.hostname, public.port or 443) != (webhook.hostname, webhook.port or 443)
                or webhook.scheme != "https"):
            raise ValueError("Demo media and webhook must use the same public HTTPS origin")
        data = json.loads((self.directory / "script.json").read_text())
        if not isinstance(data, list) or not data:
            raise ValueError("Script must contain steps")
        steps = []
        for entry in data:
            if not isinstance(entry, dict) or type(entry.get("step")) is not int:
                raise ValueError("Each step needs an integer label")
            if ("file" in entry) == ("files" in entry):
                raise ValueError("Use exactly one of file or files")
            files = [entry["file"]] if "file" in entry else entry["files"]
            texts = [entry["text"]] if "file" in entry else entry["text"]
            if (not isinstance(files, list) or not files or not isinstance(texts, list)
                    or len(files) != len(texts)
                    or any(not isinstance(t, str) or not t.strip() for t in texts)):
                raise ValueError("Each file needs matching text")
            if any(not isinstance(f, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.mp3", f) for f in files):
                raise ValueError("Only MP3 basenames allowed")
            steps.append(files)
        self.steps = steps

    def audio_path(self, filename: str) -> Path | None:
        if filename not in {f for step in self.steps for f in step}:
            return None
        root = (self.directory / "audio").resolve()
        path = root / filename
        try:
            if path.resolve().parent == root and path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            pass
        return None

    def matches(self, sender: str) -> bool:
        return bool(self.settings.demo_mode and self.settings.demo_phone_number
                    and normalize_phone_number(sender) == normalize_phone_number(self.settings.demo_phone_number))

    def status(self) -> dict:
        missing = sorted({f for step in self.steps for f in step if self.audio_path(f) is None})
        return {
            "ready": bool(self.settings.demo_mode and self.steps and not missing
                          and not self.configuration_error and not self.failure),
            "current_step": self.index,
            "completed": bool(self.steps and self.index >= len(self.steps)),
            "missing_files": missing,
            "configuration_error": self.configuration_error,
            "delivery_failure": self.failure,
        }

    async def reset(self) -> dict:
        async with self.lock:
            self.index = 0
            self.failure = None
            logger.info("Demo reset to step 0")
            return self.status()

    async def play(self) -> None:
        async with self.lock:
            state = self.status()
            if not state["ready"] or state["completed"]:
                logger.info("Demo playback skipped: %s", state)
                return
            filename = ""
            try:
                for offset, filename in enumerate(self.steps[self.index]):
                    if offset:
                        await asyncio.sleep(SEND_DELAY_SECONDS)
                    url = f"{self.settings.public_base_url.rstrip('/')}/demo/audio/{filename}"
                    sid = await asyncio.to_thread(
                        _send_twilio_message, self.settings, self.settings.demo_phone_number,
                        media_url=url,
                    )
                    logger.info("Demo step %s file %s accepted by Twilio: %s", self.index, filename, sid)
            except Exception as exc:
                self.failure = {"step": self.index, "file": filename, "error": type(exc).__name__}
                logger.error("Demo delivery failed; reset required: %s", self.failure)
                return
            self.index += 1
            logger.info("Demo advanced to step %s", self.index)


_player: DemoPlayer | None = None


def initialize_demo() -> DemoPlayer:
    global _player
    _player = DemoPlayer(DemoSettings())
    return _player


def get_demo_player() -> DemoPlayer:
    return _player if _player is not None else initialize_demo()


def _enabled_player() -> DemoPlayer:
    player = get_demo_player()
    if not player.settings.demo_mode:
        raise HTTPException(status_code=404)
    return player


@router.get("/status")
async def demo_status():
    return _enabled_player().status()


@router.get("/reset")
async def demo_reset():
    return await _enabled_player().reset()


@router.api_route("/audio/{filename}", methods=["GET", "HEAD"])
async def demo_audio(filename: str):
    path = _enabled_player().audio_path(filename)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/mpeg")
