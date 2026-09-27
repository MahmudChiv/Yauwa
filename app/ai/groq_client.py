"""Single-request Groq completions for extraction and trader replies."""

import asyncio
import logging
from copy import deepcopy
from typing import Any

from groq import AsyncGroq, BadRequestError

from app.config import Settings

logger = logging.getLogger(__name__)
JSON_SCHEMA_ATTEMPTS = 2


def strict_schema(schema: dict) -> dict:
    """Require all existing fields without changing their nullable types."""
    result = deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


async def complete(
    settings: Settings,
    *,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    temperature: float,
    schema: dict | None = None,
    schema_name: str = "extraction",
) -> str:
    """Return complete content without switching providers or models."""
    options = {}
    if schema is not None:
        options["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": strict_schema(schema),
            },
        }
    async with AsyncGroq(
        api_key=settings.groq_api_key.get_secret_value(),
        timeout=timeout,
        max_retries=0,
    ) as client:
        attempts = JSON_SCHEMA_ATTEMPTS if schema is not None else 1
        async with asyncio.timeout(timeout):
            for attempt in range(attempts):
                try:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        **options,
                    )
                    break
                except BadRequestError as exc:
                    body = exc.body if isinstance(exc.body, dict) else {}
                    error = body.get("error", body)
                    code = error.get("code") if isinstance(error, dict) else None
                    if code != "json_validate_failed" or attempt == attempts - 1:
                        raise
                    logger.warning(
                        "Groq rejected a schema-constrained generation; retrying once"
                    )
        if not response.choices:
            raise ValueError("Groq returned no completion choices")
        choice = response.choices[0]
        if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
            raise ValueError("Groq returned an incomplete or refused completion")
        content = choice.message.content
        if not content or not content.strip():
            raise ValueError("Groq returned an empty completion")
        return content
