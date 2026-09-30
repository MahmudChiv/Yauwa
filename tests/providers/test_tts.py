"""Tests for safe, actionable ElevenLabs failures."""

import unittest

import httpx

from app.providers.tts import ElevenLabsTTSException, _raise_for_tts_error


class ElevenLabsErrorTestCase(unittest.TestCase):
    def test_paid_plan_error_keeps_provider_reason(self) -> None:
        request = httpx.Request("POST", "https://api.elevenlabs.io/v1/text-to-speech/id")
        response = httpx.Response(
            402,
            request=request,
            json={
                "detail": {
                    "code": "paid_plan_required",
                    "message": "Free users cannot use library voices via the API.",
                }
            },
        )
        with self.assertRaisesRegex(
            ElevenLabsTTSException,
            "paid_plan_required.*Free users cannot use library voices",
        ):
            _raise_for_tts_error(response)


if __name__ == "__main__":
    unittest.main()
