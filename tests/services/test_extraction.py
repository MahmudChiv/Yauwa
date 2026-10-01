"""Tests for stock and retail-sales extraction."""

import json
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.providers.groq import strict_schema
from pydantic import ValidationError

from app.schemas.extraction import ExtractionResult, PackageSizeAnswer, StockItem
from app.services.message_processing import EXTRACTION_RETRY_REPLY, PendingStock, PENDING_STOCK_TTL_SECONDS, _get_pending_stock, _pending_stock, process_trader_audio
from app.services.extraction import (
    EXTRACTION_RESPONSE_SCHEMA,
    PROMPT,
    AudioExtractionError,
    _validate_result,
    extract_data_from_audio,
    extract_package_sizes_from_audio,
)


def _stock_item(**overrides):
    data = {
        "item_name": "Cabin biscuit",
        "unit_quantity": None,
        "bulk_type": "pack",
        "bulk_quantity": 3,
    }
    data.update(overrides)
    return data


def _sale(**overrides):
    data = {
        "item_name": "Cabin biscuit",
        "quantity": 2,
        "unit_price": 100,
        "total_price": None,
        "buyer_name": None,
    }
    data.update(overrides)
    return data


def _result(**overrides):
    data = {
        "intent": "stock_intake",
        "status": "ready",
        "transcript": "I buy three packs of Cabin biscuit.",
        "stock_items": [_stock_item(unit_quantity=150)],
        "sales": [],
        "reply_text": None,
    }
    data.update(overrides)
    return ExtractionResult.model_validate(data)


class ExtractionValidationTestCase(unittest.TestCase):
    def test_bulk_stock_without_pack_size_requests_unit_quantity(self) -> None:
        result = _validate_result(_result(stock_items=[_stock_item()]))

        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("one pack of Cabin biscuit", result.reply_text or "")
        self.assertEqual(result.stock_items[0].bulk_quantity, 3)
        self.assertIsNone(result.stock_items[0].unit_quantity)

    def test_explicit_bulk_totals_are_ready(self) -> None:
        result = _validate_result(
            _result(
                stock_items=[
                    _stock_item(),
                    _stock_item(
                        item_name="Pure water",
                        bulk_type="bag",
                        bulk_quantity=10,
                    ),
                ]
            )
        )

        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("one pack of Cabin biscuit", result.reply_text or "")
        self.assertIn("one bag of Pure water", result.reply_text or "")

        ready = _validate_result(
            _result(
                stock_items=[
                    _stock_item(unit_quantity=150),
                    _stock_item(
                        item_name="Pure water",
                        unit_quantity=200,
                        bulk_type="bag",
                        bulk_quantity=10,
                    ),
                ]
            )
        )
        self.assertEqual(ready.status, "ready")
        self.assertIsNone(ready.reply_text)

    def test_multiple_sales_are_valid_and_totals_are_calculated(self) -> None:
        result = _validate_result(
            _result(
                intent="sale",
                transcript=(
                    "I sold two Cabin biscuits at 100 each and three Coke "
                    "bottles at 250 each."
                ),
                stock_items=[],
                sales=[
                    _sale(),
                    _sale(
                        item_name="Coke bottle",
                        quantity=3,
                        unit_price=250,
                        buyer_name="Musa",
                    ),
                ],
            )
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.sales), 2)
        self.assertEqual(result.sales[0].total_price, 200)
        self.assertEqual(result.sales[1].total_price, 750)
        self.assertEqual(result.sales[1].buyer_name, "Musa")

    def test_inconsistent_sale_total_requires_clarification(self) -> None:
        result = _validate_result(
            _result(
                intent="sale",
                stock_items=[],
                sales=[_sale(total_price=350)],
            )
        )

        self.assertEqual(result.status, "needs_clarification")

    def test_fractional_retail_quantity_is_rejected(self) -> None:
        data = _result(
            intent="sale",
            stock_items=[],
            sales=[_sale()],
        ).model_dump()
        data["sales"][0]["quantity"] = 4.5

        with self.assertRaises(ValidationError):
            ExtractionResult.model_validate(data)

    def test_removed_restock_intent_is_rejected(self) -> None:
        data = _result().model_dump()
        data["intent"] = "restock"

        with self.assertRaises(ValidationError):
            ExtractionResult.model_validate(data)

    def test_unknown_intent_cannot_contain_ledger_entries(self) -> None:
        with self.assertRaisesRegex(AudioExtractionError, "Inconsistent unknown"):
            _validate_result(
                _result(
                    intent="unknown",
                    status="off_topic",
                    reply_text="Abeg send only your shop stock or completed sales.",
                )
            )

    def test_prompt_scopes_bulk_to_stock_and_supports_multiple_sales(self) -> None:
        self.assertIn(
            "A single note may contain many stock items or many sales", PROMPT
        )
        self.assertIn("Support one or many sales in the same note", PROMPT)
        self.assertIn("Ignore every purchase amount or cost price", PROMPT)
        self.assertNotIn("- restock:", PROMPT)

    def test_provider_schema_uses_supported_json_schema_fields(self) -> None:
        allowed = {"type", "properties", "required", "items", "enum"}

        def check_schema(schema: dict) -> None:
            self.assertLessEqual(set(schema), allowed)
            for property_schema in schema.get("properties", {}).values():
                check_schema(property_schema)
            if "items" in schema:
                check_schema(schema["items"])

        check_schema(EXTRACTION_RESPONSE_SCHEMA)
        self.assertEqual(
            set(EXTRACTION_RESPONSE_SCHEMA["properties"]),
            set(ExtractionResult.model_fields),
        )
        self.assertNotIn("make your voice clear", EXTRACTION_RETRY_REPLY)


def _completion(text, finish_reason="stop", refusal=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=text, refusal=refusal),
    )])


class GroqExtractionTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch("app.services.message_processing._resolve_trader_id", return_value=None))
        _pending_stock.clear()
        self.settings = Mock()
        self.settings.gemini_api_key.get_secret_value.return_value = "secret"
        self.settings.groq_extraction_model = "groq-test"
        self.settings.gemini_transcription_model = "transcription-test"
        self.settings.groq_api_key.get_secret_value.return_value = "groq-secret"
        self.client = Mock()
        self.client.aio.files.upload = AsyncMock(
            return_value=SimpleNamespace(name="files/voice", uri="https://example.test/audio", mime_type="audio/ogg")
        )
        self.client.aio.files.delete = AsyncMock()
        self.client.aio.models.generate_content = AsyncMock()
        self.client.aio.interactions.create = AsyncMock(
            return_value=SimpleNamespace(output_text="Three packs of biscuits, fifty each")
        )
        self.groq = Mock()
        self.groq.chat.completions.create = AsyncMock()
        self.groq.__aenter__ = AsyncMock(return_value=self.groq)
        self.groq.__aexit__ = AsyncMock(return_value=False)
        self.enterContext(patch("app.providers.groq.AsyncGroq", return_value=self.groq))
        self.client.aio.aclose = AsyncMock()

    def tearDown(self) -> None:
        _pending_stock.clear()

    async def test_uses_configured_model_and_cleans_upload(self) -> None:
        payload = _result().model_dump(mode="json")
        self.groq.chat.completions.create.return_value = _completion(
            text=json.dumps(payload)
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch(
                "app.services.extraction._gemini_client",
                return_value=self.client,
            ):
                result = await extract_data_from_audio(
                    path,
                    "audio/ogg",
                    settings=self.settings,
                    preferred_language="pidgin",
                )

        call = self.groq.chat.completions.create.await_args.kwargs
        self.assertEqual(call["model"], "groq-test")
        self.assertEqual(call["messages"][0]["content"], PROMPT)
        self.assertEqual(
            call["response_format"]["json_schema"]["schema"],
            strict_schema(EXTRACTION_RESPONSE_SCHEMA),
        )
        self.assertTrue(call["response_format"]["json_schema"]["strict"])
        self.assertEqual(call["response_format"]["type"], "json_schema")
        self.assertIn("<transcript>Three packs", call["messages"][1]["content"])
        self.assertIn("stored preferred language is \"pidgin\"", call["messages"][1]["content"])
        self.assertIn("'don' transcribed as English 'don't'", call["messages"][1]["content"])
        self.client.aio.models.generate_content.assert_not_awaited()
        self.groq.chat.completions.create.assert_awaited_once()
        self.groq.__aexit__.assert_awaited_once()
        self.assertEqual(result["stock_items"][0]["unit_quantity"], 150)
        self.client.aio.files.delete.assert_awaited_once_with(name="files/voice")
        self.client.aio.aclose.assert_awaited_once()

    async def test_provider_failure_still_cleans_upload(self) -> None:
        self.groq.chat.completions.create.side_effect = RuntimeError("down")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch(
                "app.services.extraction._gemini_client",
                return_value=self.client,
            ):
                with self.assertRaisesRegex(AudioExtractionError, "Could not process"):
                    await extract_data_from_audio(
                        path,
                        "audio/ogg",
                        settings=self.settings,
                    )

        self.client.aio.files.delete.assert_awaited_once_with(name="files/voice")
        self.client.aio.aclose.assert_awaited_once()

    async def test_processor_prints_and_speaks_clarification(self) -> None:
        result = _validate_result(_result(stock_items=[_stock_item()])).model_dump()
        with (
            patch(
                "app.services.message_processing.extract_data_from_audio",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.message_processing.send_onboarding_reply",
                new=AsyncMock(),
            ) as send_reply,
            patch("builtins.print") as print_result,
        ):
            processed = await process_trader_audio(
                "+2348012345678",
                "/tmp/voice.ogg",
                "audio/ogg",
                self.settings,
            )

        self.assertEqual(processed, result)
        print_result.assert_called_once()
        send_reply.assert_awaited_once_with(
            "+2348012345678",
            result["reply_text"],
            self.settings,
        )

    async def test_processor_sends_retry_when_extraction_fails(self) -> None:
        with (
            patch(
                "app.services.message_processing.extract_data_from_audio",
                new=AsyncMock(side_effect=AudioExtractionError("down")),
            ),
            patch(
                "app.services.message_processing.send_onboarding_reply",
                new=AsyncMock(),
            ) as send_reply,
        ):
            result = await process_trader_audio(
                "+2348012345678",
                "/tmp/voice.ogg",
                "audio/ogg",
                self.settings,
            )

        self.assertIsNone(result)
        send_reply.assert_awaited_once()

    async def test_package_size_request_uses_pending_indices(self) -> None:
        self.groq.chat.completions.create.return_value = _completion(
            text=json.dumps(
                {
                    "status": "answer",
                    "transcript": "One pack has fifty biscuits.",
                    "sizes": [
                        {
                            "item_index": 0,
                            "units_per_bulk": 50,
                            "corrected_item_name": None,
                        }
                    ],
                }
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch(
                "app.services.extraction._gemini_client",
                return_value=self.client,
            ):
                answer = await extract_package_sizes_from_audio(
                    path,
                    "audio/ogg",
                    [StockItem.model_validate(_stock_item())],
                    settings=self.settings,
                )

        call = self.groq.chat.completions.create.await_args.kwargs
        self.assertIn('"item_index": 0', call["messages"][1]["content"])
        self.assertEqual(answer.sizes[0].units_per_bulk, 50)
        self.client.aio.files.delete.assert_awaited_once_with(name="files/voice")

    async def test_two_notes_print_original_bulk_counts_and_calculated_units(
        self,
    ) -> None:
        first_note = _validate_result(
            _result(
                status="needs_clarification",
                stock_items=[
                    _stock_item(
                        item_name="biscuits", bulk_type="cartons", bulk_quantity=4
                    ),
                    _stock_item(
                        item_name="pure water", bulk_type="bags", bulk_quantity=5
                    ),
                    _stock_item(item_name="Danmick", bulk_type="roll", bulk_quantity=6),
                    _stock_item(
                        item_name="Yali chewing gum",
                        bulk_type="packs",
                        bulk_quantity=2,
                    ),
                    _stock_item(
                        item_name="bread",
                        unit_quantity=30,
                        bulk_type=None,
                        bulk_quantity=None,
                    ),
                ],
            )
        ).model_dump()
        answer = PackageSizeAnswer.model_validate(
            {
                "status": "answer",
                "transcript": "One carton 50, bag 20, Danono roll 10, gum pack 20",
                "sizes": [
                    {"item_index": 0, "units_per_bulk": 50},
                    {"item_index": 1, "units_per_bulk": 20},
                    {
                        "item_index": 2,
                        "units_per_bulk": 10,
                        "corrected_item_name": "Danono milk",
                    },
                    {"item_index": 3, "units_per_bulk": 20},
                ],
            }
        )
        with (
            patch(
                "app.services.message_processing.extract_data_from_audio",
                new=AsyncMock(return_value=first_note),
            ) as extract,
            patch(
                "app.services.message_processing.extract_package_sizes_from_audio",
                new=AsyncMock(return_value=answer),
            ) as extract_sizes,
            patch(
                "app.services.message_processing.send_onboarding_reply",
                new=AsyncMock(),
            ) as send_reply,
            patch("builtins.print") as print_result,
        ):
            await process_trader_audio(
                "+2348012345678", "first.ogg", "audio/ogg", self.settings
            )
            rows = await process_trader_audio(
                "+2348012345678", "second.ogg", "audio/ogg", self.settings
            )

        self.assertEqual(
            [item["unit_quantity"] for item in rows["items"]],
            [200, 100, 60, 40, 30],
        )
        self.assertEqual(
            [item["bulk_quantity"] for item in rows["items"]],
            [4, 5, 6, 2, None],
        )
        self.assertEqual(rows["items"][0]["bulk_type"], "carton")
        self.assertEqual(rows["items"][2]["item_name"], "Danono milk")
        self.assertIsNone(rows["items"][0]["unit_price"])
        self.assertIn("now saved via save_stock_items", print_result.call_args.args[0])
        self.assertEqual(extract.await_count, 1)
        extract_sizes.assert_awaited_once()
        self.assertEqual(send_reply.await_count, 2)
        self.assertNotIn("+2348012345678", _pending_stock)

    async def test_partial_size_answer_keeps_unanswered_items_pending(self) -> None:
        first_note = _validate_result(
            _result(
                stock_items=[
                    _stock_item(),
                    _stock_item(
                        item_name="Pure water", bulk_type="bag", bulk_quantity=5
                    ),
                ]
            )
        ).model_dump()
        answer = PackageSizeAnswer.model_validate(
            {
                "status": "answer",
                "transcript": "One pack has fifty",
                "sizes": [{"item_index": 0, "units_per_bulk": 50}],
            }
        )
        with (
            patch(
                "app.services.message_processing.extract_data_from_audio",
                new=AsyncMock(return_value=first_note),
            ),
            patch(
                "app.services.message_processing.extract_package_sizes_from_audio",
                new=AsyncMock(return_value=answer),
            ),
            patch(
                "app.services.message_processing.send_onboarding_reply",
                new=AsyncMock(),
            ) as send_reply,
            patch("builtins.print"),
        ):
            await process_trader_audio(
                "+2348012345678", "first.ogg", "audio/ogg", self.settings
            )
            result = await process_trader_audio(
                "+2348012345678", "second.ogg", "audio/ogg", self.settings
            )

        self.assertIsNone(result)
        self.assertEqual(_pending_stock["+2348012345678"].items[0].unit_quantity, 150)
        self.assertIsNone(_pending_stock["+2348012345678"].items[1].unit_quantity)
        self.assertIn("one bag of Pure water", send_reply.await_args.args[1])

    async def test_unmatched_package_index_does_not_change_pending_stock(
        self,
    ) -> None:
        self.groq.chat.completions.create.return_value = _completion(
            text=json.dumps(
                {
                    "status": "answer",
                    "transcript": "One bag contains twenty.",
                    "sizes": [
                        {
                            "item_index": 3,
                            "units_per_bulk": 20,
                            "corrected_item_name": None,
                        }
                    ],
                }
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch(
                "app.services.extraction._gemini_client",
                return_value=self.client,
            ):
                with self.assertRaisesRegex(
                    AudioExtractionError, "did not match pending stock"
                ):
                    await extract_package_sizes_from_audio(
                        path,
                        "audio/ogg",
                        [StockItem.model_validate(_stock_item())],
                        settings=self.settings,
                    )

        self.client.aio.files.delete.assert_awaited_once()

    async def test_bad_groq_responses_fail_and_clean_up_without_fallback(self):
        invalid = _result().model_dump(mode="json")
        invalid["stock_items"][0]["unit_quantity"] = -1
        cases = [
            _completion(""), _completion("   "), _completion("{}", "length"),
            _completion("{}", refusal="No"), SimpleNamespace(choices=[]),
            _completion("not json"), _completion("{}"),
            _completion(json.dumps(invalid)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            for response in cases:
                with self.subTest(response=response):
                    self.groq.chat.completions.create.reset_mock()
                    self.groq.__aexit__.reset_mock()
                    self.client.aio.files.delete.reset_mock()
                    self.client.aio.aclose.reset_mock()
                    self.groq.chat.completions.create.return_value = response
                    with patch("app.services.extraction._gemini_client", return_value=self.client):
                        with self.assertRaises(AudioExtractionError):
                            await extract_data_from_audio(path, "audio/ogg", settings=self.settings)
                    self.groq.chat.completions.create.assert_awaited_once()
                    self.groq.__aexit__.assert_awaited_once()
                    self.client.aio.models.generate_content.assert_not_awaited()
                    self.client.aio.files.delete.assert_awaited_once()
                    self.client.aio.aclose.assert_awaited_once()

    async def test_groq_deadline_cleans_up_without_retry(self):
        import asyncio

        async def slow_completion(**kwargs):
            await asyncio.sleep(1)

        self.groq.chat.completions.create.side_effect = slow_completion
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with (
                patch("app.services.extraction._gemini_client", return_value=self.client),
                patch("app.services.extraction.EXTRACTION_TIMEOUT_SECONDS", 0.01),
            ):
                with self.assertRaises(AudioExtractionError):
                    await extract_data_from_audio(path, "audio/ogg", settings=self.settings)
        self.groq.chat.completions.create.assert_awaited_once()
        self.groq.__aexit__.assert_awaited_once()
        self.client.aio.files.delete.assert_awaited_once()
        self.client.aio.aclose.assert_awaited_once()

    async def test_transcription_failure_never_calls_groq(self):
        self.client.aio.interactions.create.side_effect = ValueError("transcription failed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch("app.services.extraction._gemini_client", return_value=self.client):
                with self.assertRaises(AudioExtractionError):
                    await extract_data_from_audio(path, "audio/ogg", settings=self.settings)
        self.groq.chat.completions.create.assert_not_awaited()
        self.client.aio.files.delete.assert_awaited_once()
        self.client.aio.aclose.assert_awaited_once()

    async def test_model_override_and_inventory_context_reach_groq(self):
        self.groq.chat.completions.create.return_value = _completion(
            _result(intent="sale", stock_items=[], sales=[_sale()]).model_dump_json()
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch("app.services.extraction._gemini_client", return_value=self.client):
                result = await extract_data_from_audio(
                    path, "audio/ogg", settings=self.settings,
                    model="override-test", known_items=["Cabin biscuit"],
                )
        call = self.groq.chat.completions.create.await_args.kwargs
        self.assertEqual(call["model"], "override-test")
        self.assertIn('["Cabin biscuit"]', call["messages"][1]["content"])
        self.assertIsNone(result["sales"][0]["buyer_name"])
        self.assertEqual(result["sales"][0]["total_price"], 200)

    def test_expired_stock_is_not_used_for_later_audio(self) -> None:
        phone = "+2348012345678"
        _pending_stock[phone] = PendingStock(
            items=[StockItem.model_validate(_stock_item())],
            created_at=0,
        )
        with patch(
            "app.services.message_processing.time.monotonic",
            return_value=PENDING_STOCK_TTL_SECONDS + 1,
        ):
            self.assertIsNone(_get_pending_stock(phone))
        self.assertNotIn(phone, _pending_stock)


if __name__ == "__main__":
    unittest.main()
