"""Tests for the approved Item and Sale model contracts."""

import unittest

from app.models import Item, Sale


class InventoryModelTestCase(unittest.TestCase):
    def test_item_uses_nullable_whole_unit_and_bulk_fields(self) -> None:
        item = Item(
            trader_id=1,
            item_name="Pure water",
            unit_quantity=None,
            bulk_type="bag",
            bulk_quantity=10,
            unit_price=None,
            low_stock_threshold=20,
        )

        self.assertIsNone(item.unit_quantity)
        self.assertEqual(item.bulk_type, "bag")
        self.assertEqual(item.bulk_quantity, 10)
        self.assertIsNone(item.unit_price)

        columns = Item.__table__.columns
        self.assertTrue(columns["unit_quantity"].nullable)
        self.assertTrue(columns["bulk_type"].nullable)
        self.assertTrue(columns["bulk_quantity"].nullable)
        self.assertTrue(columns["unit_price"].nullable)

    def test_sale_keeps_only_whole_unit_retail_fields(self) -> None:
        sale = Sale(
            trader_id=1,
            item_name="Cabin biscuit",
            quantity=2,
            unit_price=100,
            total_price=200,
            buyer_name="Musa",
        )

        self.assertEqual(sale.quantity, 2)
        self.assertIs(Sale.__table__.columns["quantity"].type.python_type, int)
        self.assertEqual(
            set(Sale.__table__.columns.keys()),
            {
                "id",
                "trader_id",
                "item_name",
                "quantity",
                "unit_price",
                "total_price",
                "buyer_name",
                "timestamp",
            },
        )


if __name__ == "__main__":
    unittest.main()
