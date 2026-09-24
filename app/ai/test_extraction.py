"""Tests for stock and retail-sales extraction."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from app.ai.extraction import (
    EXTRACTION_RESPONSE_SCHEMA,
    EXTRACTION_RETRY_REPLY,
    PROMPT,
    AudioExtractionError,
    ExtractionResult,
    PendingStock,
    PackageSizeAnswer,
    StockItem,
    PENDING_STOCK_TTL_SECONDS,
    _get_pending_stock,
    _validate_result,
    _pending_stock,
    extract_data_from_audio,
    extract_package_sizes_from_audio,
    process_trader_audio,
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


class GeminiExtractionTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch("app.ai.extraction._resolve_trader_id", return_value=None))
        _pending_stock.clear()
        self.settings = Mock()
        self.settings.gemini_api_key.get_secret_value.return_value = "secret"
        self.settings.gemini_extraction_model = "gemini-test"
        self.client = Mock()
        self.client.aio.files.upload = AsyncMock(
            return_value=SimpleNamespace(name="files/voice")
        )
        self.client.aio.files.delete = AsyncMock()
        self.client.aio.models.generate_content = AsyncMock()
        self.client.aio.aclose = AsyncMock()

    def tearDown(self) -> None:
        _pending_stock.clear()

    async def test_uses_configured_model_and_cleans_upload(self) -> None:
        payload = _result().model_dump(mode="json")
        self.client.aio.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(payload)
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch(
                "app.ai.extraction._gemini_client",
                return_value=self.client,
            ):
                result = await extract_data_from_audio(
                    path,
                    "audio/ogg",
                    settings=self.settings,
                )

        call = self.client.aio.models.generate_content.await_args.kwargs
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["config"].system_instruction, PROMPT)
        self.assertEqual(
            call["config"].response_json_schema,
            EXTRACTION_RESPONSE_SCHEMA,
        )
        self.assertEqual(result["stock_items"][0]["unit_quantity"], 150)
        self.client.aio.files.delete.assert_awaited_once_with(name="files/voice")
        self.client.aio.aclose.assert_awaited_once()

    async def test_provider_failure_still_cleans_upload(self) -> None:
        self.client.aio.models.generate_content.side_effect = RuntimeError("down")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            path.write_bytes(b"voice")
            with patch(
                "app.ai.extraction._gemini_client",
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
                "app.ai.extraction.extract_data_from_audio",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.ai.extraction.send_onboarding_reply",
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
                "app.ai.extraction.extract_data_from_audio",
                new=AsyncMock(side_effect=AudioExtractionError("down")),
            ),
            patch(
                "app.ai.extraction.send_onboarding_reply",
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
        self.client.aio.models.generate_content.return_value = SimpleNamespace(
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
                "app.ai.extraction._gemini_client",
                return_value=self.client,
            ):
                answer = await extract_package_sizes_from_audio(
                    path,
                    "audio/ogg",
                    [StockItem.model_validate(_stock_item())],
                    settings=self.settings,
                )

        call = self.client.aio.models.generate_content.await_args.kwargs
        self.assertIn('"item_index": 0', call["contents"][1])
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
                "app.ai.extraction.extract_data_from_audio",
                new=AsyncMock(return_value=first_note),
            ) as extract,
            patch(
                "app.ai.extraction.extract_package_sizes_from_audio",
                new=AsyncMock(return_value=answer),
            ) as extract_sizes,
            patch(
                "app.ai.extraction.send_onboarding_reply",
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
        self.assertIn("not saved", print_result.call_args.args[0])
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
                "app.ai.extraction.extract_data_from_audio",
                new=AsyncMock(return_value=first_note),
            ),
            patch(
                "app.ai.extraction.extract_package_sizes_from_audio",
                new=AsyncMock(return_value=answer),
            ),
            patch(
                "app.ai.extraction.send_onboarding_reply",
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
        self.client.aio.models.generate_content.return_value = SimpleNamespace(
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
                "app.ai.extraction._gemini_client",
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

    def test_expired_stock_is_not_used_for_later_audio(self) -> None:
        phone = "+2348012345678"
        _pending_stock[phone] = PendingStock(
            items=[StockItem.model_validate(_stock_item())],
            created_at=0,
        )
        with patch(
            "app.ai.extraction.time.monotonic",
            return_value=PENDING_STOCK_TTL_SECONDS + 1,
        ):
            self.assertIsNone(_get_pending_stock(phone))
        self.assertNotIn(phone, _pending_stock)


if __name__ == "__main__":
    unittest.main()
