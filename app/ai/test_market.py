"""Market request routing, read-only selection and bounded per-item audio."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.ai.extraction import (
    AudioExtractionError, ExtractionResult, PackageSizeAnswer, PendingStock,
    StockItem, _pending_stock, _validate_result, process_trader_audio,
)
from app.ai.market import audio_duration, item_replies, send_market_list, send_short_reply
from app.models.item import Item
from app.models.low_stock_item import LowStockItem
from app.models.trader import Trader
from app.services.market import MarketItem, get_market_items, suggested_purchase


def result(**changes):
    data = dict(intent='market_list', status='ready', transcript='Which goods should I buy?',
                stock_items=[], sales=[], reply_text=None)
    data.update(changes)
    return ExtractionResult.model_validate(data)


class MarketQueryTests(unittest.TestCase):
    def test_threshold_rule_and_unknowns(self):
        self.assertEqual(suggested_purchase(3, 2), 28)
        self.assertEqual(suggested_purchase(0.30000000000000004, 0), 3)
        self.assertEqual(suggested_purchase(3, 40), 0)
        for threshold, remaining in [(0, 0), (3, None), (float('nan'), 0),
                                     (float('inf'), 0), (-1, 0), (0.35, 0)]:
            with self.subTest(threshold=threshold, remaining=remaining):
                self.assertIsNone(suggested_purchase(threshold, remaining))

    def test_read_only_queue_filters_recovered_and_other_traders(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([Trader(id=1, phone_number='1', name='A', language='pidgin'),
                             Trader(id=2, phone_number='2', name='B', language='pidgin')])
            session.add_all([
                Item(id=1, trader_id=1, item_name='Bread', unit_quantity=3, low_stock_threshold=3),
                Item(id=2, trader_id=1, item_name='Recovered', unit_quantity=10, low_stock_threshold=3),
                Item(id=3, trader_id=2, item_name='Other trader', unit_quantity=0, low_stock_threshold=3),
                Item(id=4, trader_id=1, item_name='Unknown', unit_quantity=None, low_stock_threshold=3),
                Item(id=5, trader_id=1, item_name='Not queued', unit_quantity=0, low_stock_threshold=3),
                Item(id=6, trader_id=1, item_name='Depleted', unit_quantity=0, low_stock_threshold=3),
                Item(id=7, trader_id=2, item_name='Wrong ownership', unit_quantity=0, low_stock_threshold=3),
            ])
            session.flush()
            for item_id, trader_id in [(1, 1), (2, 1), (3, 2), (4, 1), (6, 1), (7, 1)]:
                session.add(LowStockItem(item_id=item_id, trader_id=trader_id))
            session.commit()
        with patch('app.services.market.get_engine', return_value=engine):
            items = get_market_items(1)
        self.assertEqual([i.item_name for i in items], ['Bread', 'Depleted', 'Unknown'])
        self.assertEqual([i.suggested_quantity for i in items], [27, 30, None])
        with Session(engine) as session:
            self.assertEqual(session.get(Item, 1).unit_quantity, 3)
            self.assertEqual(len(session.exec(select(LowStockItem)).all()), 6)

    def test_reply_preserves_brand_size_and_never_adds_price(self):
        text, short = item_replies(MarketItem(1, 'Cabin biscuit 100 g', 2, 28))
        self.assertIn('Cabin biscuit 100 g', text)
        self.assertIn('28 pieces', short)
        self.assertNotIn('naira', text)
        self.assertIn('how many', item_replies(MarketItem(2, 'Rice', None, None))[0])

    def test_market_schema_requires_empty_lists(self):
        self.assertIsNone(_validate_result(result()).reply_text)
        bad = result(stock_items=[dict(item_name='Bread', unit_quantity=3,
                                       bulk_type=None, bulk_quantity=None)])
        with self.assertRaises(AudioExtractionError):
            _validate_result(bad)
        with self.assertRaises(AudioExtractionError):
            _validate_result(result(status='off_topic'))
        self.assertEqual(_validate_result(result(status='needs_clarification')).status, 'ready')
        self.assertIsNone(_validate_result(result(status='needs_clarification', reply_text='Show list?')).reply_text)

    def test_real_mp3_duration_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.mp3'
            # MPEG1 Layer III, 128kbps, 44.1kHz: 417-byte frames, 1152 samples/frame.
            path.write_bytes((bytes.fromhex('fffb9000') + bytes(413)) * 100)
            self.assertAlmostEqual(audio_duration(path), 100 * 417 * 8 / 128000, places=5)


class MarketAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(public_base_url='https://example.com')
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'test.mp3'
        self.path.write_bytes(b'audio')
        self.enterContext(patch('app.ai.market._validate_public_media_origin'))
        self.tts = self.enterContext(patch('app.ai.market.synthesize_speech',
                                          new=AsyncMock(return_value=self.path)))
        self.duration = self.enterContext(patch('app.ai.market.audio_duration', return_value=5))
        self.register = self.enterContext(patch('app.ai.market.register_media',
                                               new=AsyncMock(return_value='token')))
        self.send = self.enterContext(patch('app.ai.market._send_twilio_message', return_value='sid'))

    async def test_valid_audio_is_queued_once(self):
        self.assertEqual(await send_short_reply('phone', 'full', 'short', self.settings), 'audio')
        self.send.assert_called_once_with(self.settings, 'phone', media_url='https://example.com/api/v1/media/token')
        self.assertTrue(self.path.exists())

    async def test_long_audio_retries_shorter_then_uses_text(self):
        self.duration.return_value = 11
        channel = await send_short_reply('phone', 'full', 'short', self.settings)
        self.assertEqual(channel, 'text')
        self.assertEqual(self.tts.await_count, 2)
        self.register.assert_not_awaited()
        self.assertFalse(self.path.exists())
        self.send.assert_called_once_with(self.settings, 'phone', body='full')

    async def test_shorter_retry_can_pass(self):
        self.duration.side_effect = [11, 9]
        self.assertEqual(await send_short_reply('phone', 'full', 'short', self.settings), 'audio')
        self.assertEqual(self.tts.await_args.args[0], 'short')

    async def test_invalid_audio_uses_text(self):
        self.duration.side_effect = ValueError('invalid MP3')
        self.assertEqual(await send_short_reply('phone', 'full', 'short', self.settings), 'text')
        self.register.assert_not_awaited()

    async def test_provider_failures_and_uncertain_submission(self):
        self.tts.side_effect = RuntimeError('tts down')
        self.assertEqual(await send_short_reply('phone', 'full', 'short', self.settings), 'text')
        self.send.reset_mock()
        self.tts.side_effect = None
        self.send.side_effect = TimeoutError('unknown acceptance')
        self.assertEqual(await send_short_reply('phone', 'full', 'short', self.settings), 'failed')
        self.send.assert_called_once()  # no duplicate text after uncertain audio submission

    async def test_one_send_per_item_in_order_and_delay(self):
        items = [MarketItem(1, 'Bread', 2, 28), MarketItem(2, 'Cabin', 1, 9), MarketItem(3, 'Egg', 0, 30)]
        with patch('app.ai.market.get_market_items', return_value=items), \
             patch('app.ai.market.send_short_reply', new=AsyncMock(side_effect=['audio', 'failed', 'audio'])) as send, \
             patch('app.ai.market.asyncio.sleep', new=AsyncMock()) as sleep:
            summary = await send_market_list('phone', 1, self.settings)
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual([d['item_id'] for d in summary['deliveries']], [1, 2, 3])
        self.assertEqual(send.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_empty_unavailable_and_missing_trader(self):
        with patch('app.ai.market.get_market_items', return_value=[]) as query:
            self.assertEqual((await send_market_list('phone', 1, self.settings))['status'], 'empty')
            query.side_effect = RuntimeError('db down')
            self.assertEqual((await send_market_list('phone', 1, self.settings))['status'], 'unavailable')
            query.reset_mock()
            self.assertEqual((await send_market_list('phone', None, self.settings))['status'], 'unavailable')
            query.assert_not_called()

    async def test_cancellation_propagates(self):
        self.tts.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await send_short_reply('phone', 'full', 'short', self.settings)
        self.send.assert_not_called()

    async def test_ready_routing_never_writes_stock_or_sales(self):
        with patch('app.ai.extraction.extract_data_from_audio', new=AsyncMock(return_value=result().model_dump())), \
             patch('app.ai.extraction.send_market_list', new=AsyncMock(return_value={'status': 'queued'})) as market, \
             patch('app.ai.extraction._save_extracted_sales') as sales, \
             patch('app.ai.extraction.save_stock_items') as stock, \
             patch('app.ai.extraction.send_onboarding_reply', new=AsyncMock()) as reply:
            response = await process_trader_audio('phone', 'audio.ogg', 'audio/ogg', self.settings, trader_id=1)
        market.assert_awaited_once_with('phone', 1, self.settings)
        sales.assert_not_called()
        stock.assert_not_called()
        reply.assert_not_awaited()
        self.assertEqual(response['market_result']['status'], 'queued')

    async def test_recognized_market_request_skips_confirmation(self):
        payload = _validate_result(result(status='needs_clarification')).model_dump()
        with patch('app.ai.extraction.extract_data_from_audio', new=AsyncMock(return_value=payload)), \
             patch('app.ai.extraction.send_market_list', new=AsyncMock()) as market, \
             patch('app.ai.extraction.send_onboarding_reply', new=AsyncMock()) as reply:
            await process_trader_audio('phone', 'audio.ogg', 'audio/ogg', self.settings, trader_id=1)
        market.assert_awaited_once()
        reply.assert_not_awaited()

    async def test_market_request_interrupts_package_followup(self):
        import time
        _pending_stock['phone'] = PendingStock([StockItem(item_name='Bread', unit_quantity=None,
                                                         bulk_type='pack', bulk_quantity=2)], time.monotonic())
        self.addCleanup(_pending_stock.clear)
        answer = PackageSizeAnswer(status='new_message', transcript='Which goods should I buy?', sizes=[])
        with patch('app.ai.extraction.extract_package_sizes_from_audio', new=AsyncMock(return_value=answer)), \
             patch('app.ai.extraction.extract_data_from_audio', new=AsyncMock(return_value=result().model_dump())), \
             patch('app.ai.extraction.send_market_list', new=AsyncMock()) as market:
            await process_trader_audio('phone', 'audio.ogg', 'audio/ogg', self.settings, trader_id=1)
        market.assert_awaited_once()
        self.assertNotIn('phone', _pending_stock)
