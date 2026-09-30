"""Offline arithmetic and transaction regressions; SQLite does not test PG locks."""

import unittest
from decimal import Decimal

from sqlmodel import Session, SQLModel, create_engine, select

from app.schemas.extraction import ExtractionResult
from app.services.extraction import _validate_result
from app.db.ledger import SaleWriteError, record_sales
from app.db.money import money, sale_total
from app.models.item import Item
from app.models.low_stock_item import LowStockItem
from app.models.sale import Sale
from app.models.trader import Trader


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            trader = Trader(phone_number="+2348000000000", name="Test", language="pidgin")
            session.add(trader)
            session.flush()
            self.trader_id = trader.id
            session.add(Item(trader_id=trader.id, item_name="Biscuits",
                             unit_quantity=10, low_stock_threshold=2))
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def sale(self, quantity=3, price=0.1, total=0.30000000000000004, name="Biscuits"):
        return dict(item_name=name, quantity=quantity, unit_price=price,
                    total_price=total, buyer_name=None)

    def test_extraction_to_database_decimal_total(self):
        data = self.sale(total=None)
        result = _validate_result(ExtractionResult(
            intent="sale", status="ready", transcript="Sold three biscuits",
            stock_items=[], sales=[data], reply_text=None))
        self.assertEqual(result.sales[0].total_price, 0.3)
        with Session(self.engine) as session:
            summary = record_sales(session, self.trader_id,
                                   [row.model_dump() for row in result.sales])
        self.assertEqual(summary["sales"][0]["remaining_quantity"], 7)

    def test_float_artifact_accepted(self):
        with Session(self.engine) as session:
            self.assertEqual(record_sales(session, self.trader_id, [self.sale()])[
                "total_sales_amount"], 0.3)

    def test_incorrect_total_rejected(self):
        with Session(self.engine) as session:
            with self.assertRaises(SaleWriteError):
                record_sales(session, self.trader_id, [self.sale(total=0.31)])

    def test_repeated_item_in_batch(self):
        with Session(self.engine) as session:
            result = record_sales(session, self.trader_id, [self.sale(), self.sale()])
        self.assertEqual(result["recorded_count"], 2)
        self.assertEqual(result["sales"][-1]["remaining_quantity"], 4)

    def test_later_failure_rolls_back_whole_batch(self):
        with Session(self.engine) as session:
            with self.assertRaises(SaleWriteError):
                record_sales(session, self.trader_id,
                             [self.sale(), self.sale(quantity=20, total=2)])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(Item)).one().unit_quantity, 10)
            self.assertEqual(session.exec(select(Sale)).all(), [])

    def test_unknown_item_records_sale(self):
        with Session(self.engine) as session:
            result = record_sales(session, self.trader_id, [self.sale(name="Eggs")])
        self.assertEqual(result["unmatched_items"], ["Eggs"])
        self.assertFalse(result["sales"][0]["stock_adjusted"])

    def test_unknown_stock_rejected(self):
        with Session(self.engine) as session:
            item = session.exec(select(Item)).one()
            item.unit_quantity = None
            session.add(item)
            session.commit()
        with Session(self.engine) as session:
            with self.assertRaises(SaleWriteError):
                record_sales(session, self.trader_id, [self.sale()])

    def test_kobo_rounding(self):
        self.assertEqual(money(1.005), Decimal("1.01"))
        self.assertEqual(sale_total(1.005, 3), Decimal("3.03"))

    def test_low_stock_alert_triggers_on_subsequent_sales(self):
        # Stock start: 10, low_stock_threshold: 2
        # Sale 1: sell 8 units -> remaining 2 units (at threshold). Alert needed.
        with Session(self.engine) as session:
            result1 = record_sales(session, self.trader_id, [self.sale(quantity=8, price=100.0, total=800.0)])
            self.assertTrue(result1["sales"][0]["alert_needed"])
            self.assertEqual(result1["sales"][0]["remaining_quantity"], 2)
            low_count = len(session.exec(select(LowStockItem)).all())
            self.assertEqual(low_count, 1)

        # Sale 2: sell 1 more unit -> remaining 1 unit (below threshold, already in LowStockItem). Alert STILL needed.
        with Session(self.engine) as session:
            result2 = record_sales(session, self.trader_id, [self.sale(quantity=1, price=100.0, total=100.0)])
            self.assertTrue(result2["sales"][0]["alert_needed"])
            self.assertEqual(result2["sales"][0]["remaining_quantity"], 1)
            low_count = len(session.exec(select(LowStockItem)).all())
            self.assertEqual(low_count, 1)

        # Sale 3: sell remaining 1 unit -> remaining 0 units (stock finished completely). Alert STILL needed.
        with Session(self.engine) as session:
            result3 = record_sales(session, self.trader_id, [self.sale(quantity=1, price=100.0, total=100.0)])
            self.assertTrue(result3["sales"][0]["alert_needed"])
            self.assertEqual(result3["sales"][0]["remaining_quantity"], 0)


if __name__ == "__main__":
    unittest.main()
