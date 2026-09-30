"""Twilio WhatsApp addresses, delivery, and media URL validation."""

from urllib.parse import urlsplit

from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.core.config import Settings


def normalize_phone_number(phone_number: str) -> str:
    """Normalize the Twilio WhatsApp sender into the stored E.164 value."""
    return phone_number.strip().removeprefix("whatsapp:").strip()


def _twilio_address(phone_number: str) -> str:
    phone = normalize_phone_number(phone_number)
    return f"whatsapp:{phone}"


def _send_twilio_message(
    settings: Settings,
    phone_number: str,
    *,
    body: str | None = None,
    media_url: str | None = None,
) -> str:
    """Send one outbound WhatsApp message through Twilio and return its SID."""
    client = Client(
        settings.twilio_account_sid,
        settings.twilio_auth_token.get_secret_value(),
        http_client=TwilioHttpClient(timeout=15),
    )
    kwargs: dict[str, object] = {
        "from_": _twilio_address(settings.twilio_whatsapp_number),
        "to": _twilio_address(phone_number),
    }
    if body is not None:
        kwargs["body"] = body
    if media_url is not None:
        kwargs["media_url"] = [media_url]
    message = client.messages.create(**kwargs)
    return str(message.sid)


def _origin(url: object) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(str(url))
    port = parsed.port or (443 if parsed.scheme == "https" else None)
    return parsed.scheme, parsed.hostname, port


def _validate_public_media_origin(settings: Settings) -> None:
    """Require webhook and media URLs to resolve to the same running app."""
    if _origin(settings.public_base_url) != _origin(settings.twilio_webhook_url):
        raise ValueError(
            "PUBLIC_BASE_URL must use the same origin as TWILIO_WEBHOOK_URL"
        )


