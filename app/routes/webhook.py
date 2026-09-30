"""Receive signed Twilio WhatsApp webhooks and download their first audio file."""

import asyncio
import logging
import time
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.datastructures import FormData
from twilio.request_validator import (  # pyright: ignore[reportMissingImports]
    RequestValidator,
)

from app.ai.onboarding import (
    onboarding_complete,
    onboard_trader,
    normalize_phone_number,
    send_onboarding_reply,
)
from app.ai.extraction import process_trader_audio
from app.ai.tts import get_media
from app.config import Settings, TwilioSettings, get_settings, get_twilio_settings
from app.db.session import get_engine
from app.schemas.webhook import TwilioWebhookPayload
from sqlmodel import Session

router = APIRouter(prefix="/api/v1", tags=["Twilio"])

MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 3
DOWNLOAD_DEADLINE_SECONDS = 45
REQUEST_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 15
CONNECT_RETRIES = 1
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _load_twilio_settings() -> TwilioSettings:
    """Convert private configuration errors into a safe service response."""
    try:
        return get_twilio_settings()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Twilio webhook configuration is unavailable.",
        ) from exc


def _validate_signature(
    form: FormData,
    signature: str | None,
    settings: TwilioSettings,
) -> None:
    """Reject requests that Twilio did not sign for the configured public URL."""
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    if not signature or not validator.validate(
        str(settings.twilio_webhook_url), form, signature
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Twilio signature.",
        )


def _validate_media_url(media_url: str, account_sid: str) -> None:
    """Accept only Twilio media URLs owned by the configured account."""
    parsed = urlparse(media_url)
    account_path = f"/Accounts/{account_sid}/"
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="MediaUrl0 is not a valid Twilio media URL for this account.",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.twilio.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or account_path not in parsed.path
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="MediaUrl0 is not a valid Twilio media URL for this account.",
        )


def _audio_suffix(content_type: str) -> str:
    """Choose a safe extension from the declared MIME type."""
    mime_type = content_type.partition(";")[0].strip().lower()
    return mimetypes.guess_extension(mime_type, strict=False) or ".audio"


def _same_origin(first_url: str, current_url: str) -> bool:
    """Return whether Basic Auth can safely be sent to the current URL."""
    first = urlparse(first_url)
    current = urlparse(current_url)
    return (first.scheme, first.hostname, first.port or 443) == (
        current.scheme,
        current.hostname,
        current.port or 443,
    )


def _validate_redirect_url(url: str) -> None:
    """Allow only credential-free HTTPS redirect targets."""
    parsed = urlparse(url)
    try:
        parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Twilio returned an unsafe media redirect.",
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Twilio returned an unsafe media redirect.",
        )


async def _remove_file(path: Path | None) -> None:
    """Best-effort removal for incomplete temporary downloads."""
    if path is None:
        return
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        # The original download error is more useful than a cleanup error.
        pass


async def _download_audio(
    media_url: str,
    content_type: str,
    settings: TwilioSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Stream one authenticated Twilio audio attachment into a temporary file."""
    _validate_media_url(media_url, settings.twilio_account_sid)
    auth = httpx.BasicAuth(
        settings.twilio_account_sid,
        settings.twilio_auth_token.get_secret_value(),
    )
    path: Path | None = None
    temp_file = None
    effective_transport = transport or httpx.AsyncHTTPTransport(retries=CONNECT_RETRIES)

    try:
        async with asyncio.timeout(DOWNLOAD_DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(
                    REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
                ),
                transport=effective_transport,
            ) as client:
                current_url = media_url
                for redirect_count in range(MAX_REDIRECTS + 1):
                    request_auth = (
                        auth if _same_origin(media_url, current_url) else None
                    )
                    async with client.stream(
                        "GET", current_url, auth=request_auth
                    ) as response:
                        if response.status_code in REDIRECT_STATUSES:
                            if redirect_count == MAX_REDIRECTS:
                                raise HTTPException(
                                    status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail="Twilio media exceeded the redirect limit.",
                                )
                            location = response.headers.get("location")
                            if not location:
                                raise HTTPException(
                                    status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail="Twilio returned an invalid media redirect.",
                                )
                            current_url = urljoin(str(response.url), location)
                            _validate_redirect_url(current_url)
                            continue

                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise HTTPException(
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="Twilio media download failed.",
                            ) from exc

                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                declared_size = int(content_length)
                            except ValueError:
                                declared_size = 0
                            if declared_size > MAX_AUDIO_BYTES:
                                raise HTTPException(
                                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                    detail="Audio file exceeds the 16 MiB limit.",
                                )

                        temp_file = await asyncio.to_thread(
                            tempfile.NamedTemporaryFile,
                            mode="w+b",
                            prefix="yauwa-",
                            suffix=_audio_suffix(content_type),
                            delete=False,
                        )
                        path = Path(temp_file.name).resolve()
                        bytes_written = 0
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            bytes_written += len(chunk)
                            if bytes_written > MAX_AUDIO_BYTES:
                                raise HTTPException(
                                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                    detail="Audio file exceeds the 16 MiB limit.",
                                )
                            await asyncio.to_thread(temp_file.write, chunk)

                        if bytes_written == 0:
                            raise HTTPException(
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="Twilio returned an empty audio file.",
                            )
                        await asyncio.to_thread(temp_file.close)
                        temp_file = None
                        return str(path)

                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Twilio media download failed.",
                )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Twilio media download timed out.",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Twilio media download timed out.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Twilio media download failed.",
        ) from exc
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save the downloaded audio.",
        ) from exc
    finally:
        if temp_file is not None:
            try:
                await asyncio.to_thread(temp_file.close)
            except OSError:
                pass
        if path is not None and temp_file is not None:
            await _remove_file(path)


logger = logging.getLogger(__name__)
MESSAGE_TTL_SECONDS = 24 * 60 * 60
_seen_messages: dict[str, float] = {}
_seen_messages_lock = asyncio.Lock()
_phone_locks: dict[str, asyncio.Lock] = {}


async def _claim_message(message_sid: str) -> bool:
    """Suppress duplicate webhook deliveries for this running process."""
    now = time.monotonic()
    async with _seen_messages_lock:
        expired = [
            sid
            for sid, seen_at in _seen_messages.items()
            if now - seen_at >= MESSAGE_TTL_SECONDS
        ]
        for sid in expired:
            _seen_messages.pop(sid, None)
        if message_sid in _seen_messages:
            return False
        _seen_messages[message_sid] = now
        return True


async def _process_incoming_message(
    payload: TwilioWebhookPayload,
    twilio_settings: TwilioSettings,
) -> str:
    """Route one accepted message after Twilio has received its acknowledgement."""
    started_at = time.perf_counter()
    phone_number = normalize_phone_number(payload.sender)
    lock = _phone_locks.setdefault(phone_number, asyncio.Lock())
    if lock.locked():
        logger.info("Incoming trader message is waiting for earlier work")
    async with lock:
        logger.info("Incoming trader message background processing started")
        try:
            with Session(get_engine()) as session:
                is_onboarded = onboarding_complete(session, phone_number)
        except Exception:
            logger.exception("Trader lookup failed")
            return "processing failed"

        audio_path: str | None = None
        try:
            settings: Settings = get_settings()
            normalized_content_type = (
                payload.content_type.partition(";")[0].strip().lower()
                if payload.content_type
                else None
            )
            if (
                payload.media_url
                and normalized_content_type
                and normalized_content_type.startswith("audio/")
            ):
                download_started_at = time.perf_counter()
                audio_path = await _download_audio(
                    payload.media_url,
                    normalized_content_type,
                    twilio_settings,
                )
                logger.info(
                    "Incoming audio download completed in %.2fs",
                    time.perf_counter() - download_started_at,
                )
            if is_onboarded:
                print(f"User {phone_number} already onboarded")
                if audio_path and normalized_content_type:
                    await process_trader_audio(
                        phone_number,
                        audio_path,
                        normalized_content_type,
                        settings,
                    )
                else:
                    await send_onboarding_reply(
                        phone_number,
                        "Abeg send voice note about your shop stock or sales "
                        "make I fit hear you well.",
                        settings,
                    )
                result = "extraction processed"
            else:
                result = await onboard_trader(
                    phone_number,
                    audio_path,
                    normalized_content_type,
                    settings,
                )
            logger.info(
                "Incoming trader message processed in %.2fs",
                time.perf_counter() - started_at,
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Incoming trader message failed")
            return "processing failed"
        finally:
            if audio_path:
                await _remove_file(Path(audio_path))


@router.api_route("/media/{token}", methods=["GET", "HEAD"])
async def serve_reply_audio(token: str) -> FileResponse:
    """Serve an unexpired generated MP3 by opaque token."""
    path = await get_media(token)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(path, media_type="audio/mpeg", filename="reply.mp3")


@router.post("/webhook", response_class=Response)
async def receive_twilio_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Validate a Twilio webhook, acknowledge it, then process it in background."""
    settings = _load_twilio_settings()
    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid form-encoded webhook payload.",
        ) from exc

    _validate_signature(form, request.headers.get("X-Twilio-Signature"), settings)
    try:
        payload = TwilioWebhookPayload.model_validate(dict(form))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(include_url=False),
        ) from exc

    if await _claim_message(payload.message_sid):
        logger.info("Accepted Twilio message for background processing")
        background_tasks.add_task(_process_incoming_message, payload, settings)
    else:
        logger.info("Suppressed duplicate Twilio webhook delivery")
    return Response(content="<Response></Response>", media_type="application/xml")
