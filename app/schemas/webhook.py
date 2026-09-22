"""Schemas for validated Twilio webhook input and development responses."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TwilioWebhookPayload(BaseModel):
    """The Twilio fields used by the first-stage WhatsApp audio pipeline."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sender: str = Field(alias="From", min_length=1)
    media_url: str | None = Field(default=None, alias="MediaUrl0")
    content_type: str | None = Field(default=None, alias="MediaContentType0")

    @field_validator("sender", "media_url", "content_type", mode="before")
    @classmethod
    def strip_and_normalize_strings(cls, value: object) -> object:
        """Trim Twilio form values and treat blank optional values as absent."""
        if isinstance(value, str):
            return value.strip() or None
        return value


class WebhookResponse(BaseModel):
    """Development response describing the accepted incoming message."""

    sender: str
    media_url: str | None
    content_type: str | None
    file_path: str | None
