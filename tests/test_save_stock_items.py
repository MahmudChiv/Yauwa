"""Integration tests for the save_stock_items inventory service."""

from contextlib import contextmanager
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from sqlmodel import Session, delete, select

from app.db.session import get_test_engine
from app.models.item import Item
from app.models.trader import Trader
from app.services.inventory import save_stock_items

TEST_PHONE = "+2349999999999"


@contextmanager
def _test_session():
    with Session(get_test_engine()) as session:
        yield session


class SaveStockItemsTestCase(unittest.TestCase):
    """Verify stock items persist to the database with expected thresholds and links."""

    def setUp(self) -> None:
        """Ensure a clean slate before each test run."""
        self._cleanup()

    def tearDown(self) -> None:
        """Remove test trader and inserted items after the test completes."""
        self._cleanup()

    def _cleanup(self) -> None:
        """Delete test rows associated with TEST_PHONE."""
        engine = get_test_engine()
        with Session(engine) as session:
            trader = session.exec(
                select(Trader).where(Trader.phone_number == TEST_PHONE)
            ).first()
            if trader and trader.id:
                session.exec(delete(Item).where(Item.trader_id == trader.id))
                session.exec(delete(Trader).where(Trader.id == trader.id))
                session.commit()

    def test_save_stock_items_end_to_end(self) -> None:
        """Create a trader, save items via service, and verify DB row contents."""
        engine = get_test_engine()

        # 1. Create a test trader
        with Session(engine) as session:
            trader = Trader(
                phone_number=TEST_PHONE,
                name="Test Trader",
                language="pidgin",
            )
            session.add(trader)
            session.commit()
            session.refresh(trader)
            trader_id = trader.id
            self.assertIsNotNone(trader_id)

        # 2. Prepare 3 test stock items (2 with unit_quantity set, 1 with None)
        test_items = [
            {
                "item_name": "Indomie noodles",
                "unit_quantity": 40,
                "bulk_type": "carton",
                "bulk_quantity": 1,
            },
            {
                "item_name": "Peak milk",
                "unit_quantity": 25,
                "bulk_type": "roll",
                "bulk_quantity": 2,
            },
            {
                "item_name": "Pure water",
                "unit_quantity": None,
                "bulk_type": "bag",
                "bulk_quantity": 10,
            },
        ]

        # 3. Call the save_stock_items service routed to the test database
        with patch("app.services.inventory.get_session", _test_session):
            inserted_count = save_stock_items(trader_id, test_items)
        self.assertEqual(inserted_count, 3)

        # 4. Read the rows back from the database and verify fields
        now = datetime.now(timezone.utc)
        with Session(engine) as session:
            rows = session.exec(
                select(Item)
                .where(Item.trader_id == trader_id)
                .order_by(Item.item_name)
            ).all()

            # Assert count is 3
            self.assertEqual(len(rows), 3)

            # Map by item_name for clear field-by-field assertions
            row_map = {item.item_name: item for item in rows}

            # Check Indomie (40 units -> threshold 4.0)
            indomie = row_map["Indomie noodles"]
            self.assertEqual(indomie.trader_id, trader_id)
            self.assertEqual(indomie.unit_quantity, 40)
            self.assertEqual(indomie.bulk_type, "carton")
            self.assertEqual(indomie.bulk_quantity, 1)
            self.assertAlmostEqual(indomie.low_stock_threshold, 4.0)
            self.assertIsNone(indomie.unit_price)
            self.assertLess(abs((now - indomie.last_updated).total_seconds()), 60)

            # Check Peak milk (25 units -> threshold 2.5)
            peak_milk = row_map["Peak milk"]
            self.assertEqual(peak_milk.trader_id, trader_id)
            self.assertEqual(peak_milk.unit_quantity, 25)
            self.assertEqual(peak_milk.bulk_type, "roll")
            self.assertEqual(peak_milk.bulk_quantity, 2)
            self.assertAlmostEqual(peak_milk.low_stock_threshold, 2.5)
            self.assertIsNone(peak_milk.unit_price)
            self.assertLess(abs((now - peak_milk.last_updated).total_seconds()), 60)

            # Check Pure water (None units -> threshold 0.0)
            water = row_map["Pure water"]
            self.assertEqual(water.trader_id, trader_id)
            self.assertIsNone(water.unit_quantity)
            self.assertEqual(water.bulk_type, "bag")
            self.assertEqual(water.bulk_quantity, 10)
            self.assertEqual(water.low_stock_threshold, 0.0)
            self.assertIsNone(water.unit_price)
            self.assertLess(abs((now - water.last_updated).total_seconds()), 60)


if __name__ == "__main__":
    unittest.main()
