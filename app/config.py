"""Central environment configuration, loaded only when requested."""

from functools import lru_cache
from pathlib import Path

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Required configuration for database and provider integrations."""

    model_config = SettingsConfigDict(
        # Resolve from this file so launching outside the repo still finds .env.
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: PostgresDsn
    twilio_account_sid: str
    twilio_auth_token: SecretStr
    twilio_whatsapp_number: str
    gemini_api_key: SecretStr
    elevenlabs_api_key: SecretStr
    elevenlabs_voice_id: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Validate settings on first use; imports and health checks need no keys."""
    return Settings()
