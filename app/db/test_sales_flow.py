"""Sales persistence and async reply tests; no external services are contacted.

SQLite exercises writes and rollback, not PostgreSQL locking or migrations.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.extraction import (
    _pending_stock,
    _save_extracted_sales,
    process_trader_audio,
)
from app.db.ledger import SaleWriteError, record_sales
from app.models.item import Item
from app.models.sale import Sale
from app.models.trader import Trader


def sale(**changes):
    data = dict(item_name="Cabin biscuits", quantity=3, unit_price=100.0,
                total_price=300.0, buyer_name=None)
    data.update(changes)
    return data


def extracted(**changes):
    data = dict(intent="sale", status="ready", transcript="I sell three biscuits",
                stock_items=[], sales=[sale()], reply_text=None)
    data.update(changes)
    return data


class DatabaseFixture:
    def create_database(self):
        # One temporary connection may be used by the async worker thread.
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            first = Trader(phone_number="+2348000000001", name="Amina", language="pidgin")
            second = Trader(phone_number="+2348000000002", name="Musa", language="pidgin")
            session.add_all([first, second])
            session.flush()
            self.trader_id, self.other_id = first.id, second.id
            self.phone = first.phone_number
            for trader_id in (self.trader_id, self.other_id):
                session.add(Item(trader_id=trader_id, item_name="Cabin biscuits",
                                 unit_quantity=10, unit_price=120,
                                 low_stock_threshold=2))
            session.commit()

    def snapshot(self, trader_id=None):
        trader_id = self.trader_id if trader_id is None else trader_id
        with Session(self.engine) as session:
            item = session.exec(select(Item).where(Item.trader_id == trader_id)).first()
            rows = session.exec(select(Sale).where(Sale.trader_id == trader_id)
                                .order_by(Sale.id)).all()
            return item.unit_quantity, [row.model_dump() for row in rows]

    def write(self, entries, trader_id=None):
        with Session(self.engine) as session:
            return record_sales(session, trader_id or self.trader_id, entries)


class SalesPersistenceTestCase(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        self.create_database()

    def tearDown(self):
        self.engine.dispose()

    def test_single_sale_commits_all_fields_and_preserves_catalogue_price(self):
        summary = self.write([sale(buyer_name="Aisha")])
        quantity, rows = self.snapshot()
        self.assertEqual(quantity, 7)
        self.assertEqual(len(rows), 1)
        for key, value in sale(buyer_name="Aisha").items():
            self.assertEqual(rows[0][key], value)
        self.assertEqual(rows[0]["trader_id"], self.trader_id)
        self.assertEqual(summary["sales"][0]["sale_id"], rows[0]["id"])
        with Session(self.engine) as session:
            item = session.exec(select(Item).where(Item.trader_id == self.trader_id)).one()
            self.assertEqual(item.unit_price, 120)

    def test_batch_keeps_buyers_and_prices_separate(self):
        self.write([sale(buyer_name="Aisha"),
                    sale(quantity=2, unit_price=150, total_price=300, buyer_name="Musa")])
        quantity, rows = self.snapshot()
        self.assertEqual(quantity, 5)
        self.assertEqual([row["buyer_name"] for row in rows], ["Aisha", "Musa"])
        self.assertEqual([row["unit_price"] for row in rows], [100, 150])

    def test_other_traders_stock_is_untouched(self):
        self.write([sale()])
        self.assertEqual(self.snapshot(self.other_id), (10, []))

    def test_case_and_surrounding_spaces_match(self):
        result = self.write([sale(item_name="  CABIN BISCUITS  ")])
        self.assertEqual(result["unmatched_items"], [])
        self.assertEqual(self.snapshot()[0], 7)

    def test_plural_and_spacing_flexible_match(self):
        # Singular "Cabin biscuit" matches inventory "Cabin biscuits"
        result1 = self.write([sale(item_name="Cabin biscuit")])
        self.assertEqual(result1["unmatched_items"], [])
        self.assertEqual(self.snapshot()[0], 7)

        # Space-collapsed "cabinbiscuits" matches inventory "Cabin biscuits"
        result2 = self.write([sale(item_name="cabinbiscuits")])
        self.assertEqual(result2["unmatched_items"], [])
        self.assertEqual(self.snapshot()[0], 4)

    def test_unknown_product_is_saved_and_logged_without_stock_change(self):
        with self.assertLogs("app.db.ledger", level="WARNING") as logs:
            result = self.write([sale(item_name="Eggs")])
        self.assertIn("Eggs", logs.output[0])
        self.assertEqual(result["unmatched_items"], ["Eggs"])
        self.assertEqual(self.snapshot()[0], 10)
        self.assertEqual(self.snapshot()[1][0]["item_name"], "Eggs")

    def test_ambiguous_match_rejects_without_writes(self):
        with Session(self.engine) as session:
            session.add(Item(trader_id=self.trader_id, item_name="CABIN BISCUITS",
                             unit_quantity=5, low_stock_threshold=2))
            session.commit()
        with self.assertRaises(SaleWriteError):
            self.write([sale()])
        self.assertEqual(self.snapshot(), (10, []))

    def test_missing_trader_rejects(self):
        with self.assertRaises(SaleWriteError):
            self.write([sale()], trader_id=9999)
        self.assertEqual(self.snapshot(), (10, []))

    def test_invalid_batches_leave_database_unchanged(self):
        for entries in ([], [sale()] * 51, [sale(quantity=0)],
                        [sale(quantity=1.5)], [sale(unit_price=-1)],
                        [sale(total_price=999)], [sale(unit_price=float("nan"))]):
            with self.subTest(entries=entries[:1]):
                with self.assertRaises((SaleWriteError, ValidationError)):
                    self.write(entries)
                self.assertEqual(self.snapshot(), (10, []))

    def test_later_failure_rolls_back_even_an_unknown_item_sale(self):
        with self.assertRaises(SaleWriteError):
            self.write([sale(item_name="Eggs"), sale(),
                        sale(quantity=20, total_price=2000)])
        self.assertEqual(self.snapshot(), (10, []))

    def test_exact_stock_sale_reaches_zero(self):
        self.write([sale(quantity=10, total_price=1000)])
        self.assertEqual(self.snapshot()[0], 0)

    def test_active_transaction_rejected(self):
        with Session(self.engine) as session:
            session.exec(select(Trader)).all()
            with self.assertRaises(SaleWriteError):
                record_sales(session, self.trader_id, [sale()])
        self.assertEqual(self.snapshot(), (10, []))

    def test_phone_lookup_normalizes_whatsapp_prefix(self):
        with patch("app.ai.extraction.get_engine", return_value=self.engine):
            result = _save_extracted_sales("whatsapp:" + self.phone, [sale()])
        self.assertEqual(result["recorded_count"], 1)
        self.assertEqual(self.snapshot()[0], 7)


class SalesReplyTestCase(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.create_database()
        _pending_stock.clear()
        self.settings = Mock()

    def tearDown(self):
        _pending_stock.clear()
        self.engine.dispose()

    async def run_message(self, payload, reply_mock):
        with patch("app.ai.extraction.get_engine", return_value=self.engine), \
             patch("app.ai.extraction.extract_data_from_audio",
                   new=AsyncMock(return_value=payload)), \
             patch("app.ai.extraction.send_onboarding_reply", new=reply_mock):
            return await process_trader_audio(self.phone, "unused.ogg", "audio/ogg",
                                              settings=self.settings)

    async def test_success_reply_occurs_after_committed_stock_and_sale(self):
        async def inspect_reply(phone, text, settings):
            quantity, rows = self.snapshot()
            self.assertEqual(quantity, 7)
            self.assertEqual(len(rows), 1)
            self.assertIn("I don record", text)
            self.assertEqual(phone, self.phone)
        reply = AsyncMock(side_effect=inspect_reply)
        result = await self.run_message(extracted(), reply)
        reply.assert_awaited_once()
        self.assertEqual(result["reply_text"], reply.await_args.args[1])

    async def test_failed_write_sends_no_success_confirmation(self):
        reply = AsyncMock()
        with self.assertLogs("app.ai.extraction", level="ERROR"):
            await self.run_message(extracted(sales=[sale(quantity=11, total_price=1100)]), reply)
        self.assertEqual(self.snapshot(), (10, []))
        self.assertIn("no fit confirm", reply.await_args.args[1])

    async def test_unmatched_item_reply_mentions_stock_not_adjusted(self):
        reply = AsyncMock()
        with self.assertLogs("app.db.ledger", level="WARNING"):
            await self.run_message(extracted(sales=[sale(item_name="Eggs")]), reply)
        self.assertIn("no reduce their stock", reply.await_args.args[1])
        self.assertEqual(self.snapshot()[0], 10)
        self.assertEqual(len(self.snapshot()[1]), 1)

    async def test_clarification_and_off_topic_never_write(self):
        for status, intent in (("needs_clarification", "sale"), ("off_topic", "unknown")):
            with self.subTest(status=status):
                reply = AsyncMock()
                with patch("app.ai.extraction._save_extracted_sales") as save:
                    await self.run_message(extracted(status=status, intent=intent,
                                                     sales=[], reply_text="Abeg repeat am."), reply)
                save.assert_not_called()
                self.assertEqual(reply.await_args.args[1], "Abeg repeat am.")
                self.assertEqual(self.snapshot(), (10, []))

    async def test_stock_intake_does_not_enter_sale_writer(self):
        reply = AsyncMock()
        payload = extracted(intent="stock_intake", sales=[], stock_items=[dict(
            item_name="Eggs", unit_quantity=30, bulk_type=None, bulk_quantity=None)])
        with patch("app.ai.extraction._save_extracted_sales") as save:
            await self.run_message(payload, reply)
        save.assert_not_called()
        self.assertEqual(self.snapshot(), (10, []))

    async def test_extraction_failure_sends_retry_without_database_write(self):
        reply = AsyncMock()
        with patch("app.ai.extraction.extract_data_from_audio",
                   new=AsyncMock(side_effect=RuntimeError("provider down"))), \
             patch("app.ai.extraction._save_extracted_sales") as save, \
             patch("app.ai.extraction.send_onboarding_reply", new=reply), \
             self.assertLogs("app.ai.extraction", level="ERROR"):
            result = await process_trader_audio(self.phone, "unused.ogg", "audio/ogg",
                                                settings=self.settings)
        self.assertIsNone(result)
        save.assert_not_called()
        reply.assert_awaited_once()

    async def test_reply_delivery_failure_does_not_undo_committed_sale(self):
        with self.assertRaises(RuntimeError):
            await self.run_message(extracted(), AsyncMock(side_effect=RuntimeError("delivery")))
        quantity, rows = self.snapshot()
        self.assertEqual(quantity, 7)
        self.assertEqual(len(rows), 1)

    async def test_cancellation_before_write_propagates(self):
        with patch("app.ai.extraction.extract_data_from_audio",
                   new=AsyncMock(side_effect=asyncio.CancelledError)), \
             patch("app.ai.extraction._save_extracted_sales") as save, \
             patch("app.ai.extraction.send_onboarding_reply", new=AsyncMock()) as reply:
            with self.assertRaises(asyncio.CancelledError):
                await process_trader_audio(self.phone, "unused.ogg", "audio/ogg",
                                           settings=self.settings)
        save.assert_not_called()
        reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
