"""ElevenLabs text-to-speech and temporary public audio storage."""

import asyncio
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import Settings

TTS_TIMEOUT_SECONDS = 30
MAX_REPLY_AUDIO_BYTES = 8 * 1024 * 1024
MEDIA_TTL_SECONDS = 24 * 60 * 60
MEDIA_DIRECTORY = Path(tempfile.gettempdir()) / "yauwa-replies"


@dataclass(frozen=True)
class MediaAsset:
    path: Path
    expires_at: float


_media_assets: dict[str, MediaAsset] = {}
_media_lock = asyncio.Lock()


class ElevenLabsTTSException(RuntimeError):
    """A safe, actionable ElevenLabs API failure."""


def _raise_for_tts_error(response: httpx.Response) -> None:
    """Preserve provider status and reason without logging credentials."""
    if not response.is_error:
        return
    code = "unknown_error"
    message = "ElevenLabs rejected the text-to-speech request."
    try:
        detail = response.json().get("detail", {})
        if isinstance(detail, dict):
            code = str(detail.get("code") or code)
            message = str(detail.get("message") or message)
    except (TypeError, ValueError):
        pass
    raise ElevenLabsTTSException(
        f"ElevenLabs TTS failed ({response.status_code}, {code}): {message}"
    )


async def synthesize_speech(text: str, settings: Settings) -> Path:
    """Convert one reply to MP3 and retain it for later Twilio retrieval."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}"
    path: Path | None = None
    try:
        async with asyncio.timeout(TTS_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(timeout=TTS_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    url,
                    params={"output_format": "mp3_44100_128"},
                    headers={
                        "xi-api-key": settings.elevenlabs_api_key.get_secret_value(),
                        "Accept": "audio/mpeg",
                        "Content-Type": "application/json",
                    },
                    json={"text": text, "model_id": settings.elevenlabs_model_id},
                )
                _raise_for_tts_error(response)
                audio = response.content
        if not audio or len(audio) > MAX_REPLY_AUDIO_BYTES:
            raise ValueError("ElevenLabs returned invalid reply audio")
        await asyncio.to_thread(MEDIA_DIRECTORY.mkdir, parents=True, exist_ok=True)
        handle = await asyncio.to_thread(
            tempfile.NamedTemporaryFile,
            mode="w+b",
            prefix="reply-",
            suffix=".mp3",
            dir=MEDIA_DIRECTORY,
            delete=False,
        )
        path = Path(handle.name).resolve()
        try:
            await asyncio.to_thread(handle.write, audio)
        finally:
            await asyncio.to_thread(handle.close)
        return path
    except Exception:
        if path is not None:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        raise


async def register_media(path: Path) -> str:
    """Create an opaque, expiring token for a generated audio file."""
    await cleanup_expired_media()
    token = secrets.token_urlsafe(32)
    async with _media_lock:
        _media_assets[token] = MediaAsset(
            path=path.resolve(), expires_at=time.time() + MEDIA_TTL_SECONDS
        )
    return token


async def get_media(token: str) -> Path | None:
    """Return an unexpired registered path without accepting filesystem input."""
    async with _media_lock:
        asset = _media_assets.get(token)
        if asset is None:
            return None
        if asset.expires_at > time.time() and asset.path.is_file():
            return asset.path
        _media_assets.pop(token, None)
    await asyncio.to_thread(asset.path.unlink, missing_ok=True)
    return None


async def cleanup_expired_media() -> None:
    """Delete expired generated audio files, including leftovers after restarts."""
    now = time.time()
    async with _media_lock:
        expired = [
            (token, asset)
            for token, asset in _media_assets.items()
            if asset.expires_at <= now
        ]
        for token, _ in expired:
            _media_assets.pop(token, None)
    for _, asset in expired:
        try:
            await asyncio.to_thread(asset.path.unlink, missing_ok=True)
        except OSError:
            pass
    if MEDIA_DIRECTORY.is_dir():
        for path in MEDIA_DIRECTORY.glob("reply-*.mp3"):
            try:
                if now - path.stat().st_mtime >= MEDIA_TTL_SECONDS:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                pass
