"""Serve temporary audio replies to Twilio."""

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.providers.tts import get_media

router = APIRouter(prefix="/api/v1", tags=["Twilio"])


@router.api_route("/media/{token}", methods=["GET", "HEAD"])
async def serve_reply_audio(token: str) -> FileResponse:
    """Serve an unexpired generated MP3 by opaque token."""
    path = await get_media(token)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(path, media_type="audio/mpeg", filename="reply.mp3")


