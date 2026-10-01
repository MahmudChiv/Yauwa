"""Tests for market restock confirmation, inventory merging, price updates, and queue cleanup."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.item import Item
from app.models.low_stock_item import LowStockItem
from app.models.trader import Trader
from app.services.market import (
    MarketItem,
    get_last_sent_market_list,
    process_restock_confirmation,
    save_sent_market_list,
)


class MarketRestockTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            trader = Trader(id=1, phone_number="+2348000000001", name="Amina", language="pidgin")
            session.add(trader)
            session.flush()

            # Item 1: Cabin biscuits (low stock: 2 remaining out of threshold 5, suggested purchase = 48)
            item1 = Item(
                id=1,
                trader_id=1,
                item_name="Cabin biscuit 100g",
                unit_quantity=2,
                unit_price=100.0,
                low_stock_threshold=5.0,
                last_updated=datetime.now(timezone.utc),
            )
            # Item 2: Bread (low stock: 0 remaining out of threshold 10, suggested purchase = 100)
            item2 = Item(
                id=2,
                trader_id=1,
                item_name="Bread",
                unit_quantity=0,
                unit_price=500.0,
                low_stock_threshold=10.0,
                last_updated=datetime.now(timezone.utc),
            )
            # Item 3: Pure water (normal stock: 50 remaining, threshold 10)
            item3 = Item(
                id=3,
                trader_id=1,
                item_name="Pure water",
                unit_quantity=50,
                unit_price=20.0,
                low_stock_threshold=10.0,
                last_updated=datetime.now(timezone.utc),
            )

            session.add_all([item1, item2, item3])
            session.flush()

            # Add low stock items to queue
            session.add_all([
                LowStockItem(id=1, item_id=1, trader_id=1),
                LowStockItem(id=2, item_id=2, trader_id=1),
            ])
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_save_and_get_last_sent_market_list(self):
        items = [MarketItem(1, "Cabin biscuit 100g", 2, 48)]
        save_sent_market_list(1, items)
        retrieved = get_last_sent_market_list(1)
        self.assertEqual(len(retrieved), 1)
        self.assertEqual(retrieved[0]["item_name"], "Cabin biscuit 100g")
        self.assertEqual(retrieved[0]["suggested_quantity"], 48)

    def test_full_confirmation_without_modifications(self):
        # Save market list sent to trader
        save_sent_market_list(
            1,
            [
                MarketItem(1, "Cabin biscuit 100g", 2, 48),
                MarketItem(2, "Bread", 0, 100),
            ],
        )

        with patch("app.services.market.get_engine", return_value=self.engine):
            res = process_restock_confirmation(
                trader_id=1,
                confirms_market_list=True,
                stock_items=[],
            )

        self.assertEqual(res["status"], "success")

        with Session(self.engine) as session:
            item1 = session.get(Item, 1)
            item2 = session.get(Item, 2)
            # Stock 1: original 2 + 48 restocked = 50
            self.assertEqual(item1.unit_quantity, 50)
            # Stock 2: original 0 + 100 restocked = 100
            self.assertEqual(item2.unit_quantity, 100)

            # LowStockItem queue entries should be cleared since both items > threshold
            queue = session.exec(select(LowStockItem).where(LowStockItem.trader_id == 1)).all()
            self.assertEqual(len(queue), 0)

    def test_confirmation_with_modifications_and_price_change(self):
        save_sent_market_list(
            1,
            [
                MarketItem(1, "Cabin biscuit 100g", 2, 48),
                MarketItem(2, "Bread", 0, 100),
            ],
        )

        # Trader added 5 more Cabin biscuits, removed Bread, updated Cabin biscuit price to 120
        stock_modifications = [
            {
                "item_name": "Cabin biscuit 100g",
                "action": "add",
                "unit_quantity": 5,
                "unit_price": 120.0,
                "bulk_type": None,
                "bulk_quantity": None,
            },
            {
                "item_name": "Bread",
                "action": "remove",
                "unit_quantity": None,
                "unit_price": None,
                "bulk_type": None,
                "bulk_quantity": None,
            },
        ]

        with patch("app.services.market.get_engine", return_value=self.engine):
            res = process_restock_confirmation(
                trader_id=1,
                confirms_market_list=True,
                stock_items=stock_modifications,
            )

        self.assertEqual(res["status"], "success")

        with Session(self.engine) as session:
            item1 = session.get(Item, 1)
            item2 = session.get(Item, 2)

            # Cabin biscuit: 2 original + (48 suggested + 5 added) = 55
            self.assertEqual(item1.unit_quantity, 55)
            self.assertEqual(item1.unit_price, 120.0)

            # Bread: 0 original + 0 restocked = 0 (remains low stock)
            self.assertEqual(item2.unit_quantity, 0)

            # LowStockItem for Cabin biscuit cleared, Bread remains in queue
            queue = session.exec(select(LowStockItem).where(LowStockItem.trader_id == 1)).all()
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0].item_id, 2)

    def test_confirmation_with_new_entire_item(self):
        save_sent_market_list(
            1,
            [
                MarketItem(1, "Cabin biscuit 100g", 2, 48),
            ],
        )

        # Trader bought list plus 20 Peak Milk (a brand new item) at 250 naira
        stock_modifications = [
            {
                "item_name": "Peak Milk",
                "action": None,
                "unit_quantity": 20,
                "unit_price": 250.0,
                "bulk_type": "carton",
                "bulk_quantity": 1,
            }
        ]

        with patch("app.services.market.get_engine", return_value=self.engine):
            res = process_restock_confirmation(
                trader_id=1,
                confirms_market_list=True,
                stock_items=stock_modifications,
            )

        self.assertEqual(res["status"], "success")

        with Session(self.engine) as session:
            item1 = session.get(Item, 1)
            self.assertEqual(item1.unit_quantity, 50)

            # Peak Milk should be created as a new row in Item table
            new_item = session.exec(select(Item).where(Item.trader_id == 1, Item.item_name == "Peak Milk")).one()
            self.assertEqual(new_item.unit_quantity, 20)
            self.assertEqual(new_item.unit_price, 250.0)
            self.assertEqual(new_item.bulk_type, "carton")
            self.assertEqual(new_item.bulk_quantity, 1)
            self.assertEqual(new_item.low_stock_threshold, 2.0)


if __name__ == "__main__":
    unittest.main()
