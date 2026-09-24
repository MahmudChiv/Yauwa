"""Regressions for the observed buyer-only clarification responses."""

import unittest

from app.ai.extraction import ExtractionResult, _validate_result


class OptionalBuyerTestCase(unittest.TestCase):
    def result(self, reply, **changes):
        row = dict(item_name="Cabin biscuit", quantity=3, unit_price=100,
                   total_price=300, buyer_name=None)
        row.update(changes)
        return ExtractionResult(
            intent="sale", status="needs_clarification",
            transcript="I sell three Cabin biscuits, 100 naira each",
            stock_items=[], sales=[row], reply_text=reply,
        )

    def test_observed_buyer_questions_do_not_block_complete_sales(self):
        for question in (
            "Abeg, who buy dis Cabin biscuit make I put am?",
            "Abeg, who buy the Cabin biscuit?",
        ):
            with self.subTest(question=question):
                result = _validate_result(self.result(question))
                self.assertEqual(result.status, "ready")
                self.assertIsNone(result.reply_text)
                self.assertIsNone(result.sales[0].buyer_name)

    def test_missing_required_fields_still_block(self):
        for change in ({"quantity": None}, {"unit_price": None}, {"total_price": 999}):
            with self.subTest(change=change):
                result = _validate_result(self.result("Abeg, who buy the Cabin biscuit?", **change))
                self.assertEqual(result.status, "needs_clarification")

    def test_combined_or_semantic_questions_are_preserved(self):
        for question in (
            "Abeg, who buy the Cabin biscuit and how much?",
            "Na 100 each or 100 for all?",
            "Which biscuit brand you sell?",
            "You don sell am or you wan sell am?",
        ):
            with self.subTest(question=question):
                result = _validate_result(self.result(question))
                self.assertEqual(result.status, "needs_clarification")
                self.assertEqual(result.reply_text, question)

    def test_second_incomplete_sale_prevents_promotion(self):
        result = self.result("Abeg, who buy the Cabin biscuit?")
        result.sales.append(result.sales[0].model_copy(update={"item_name": "Egg", "quantity": None}))
        self.assertEqual(_validate_result(result).status, "needs_clarification")

    def test_provided_buyer_is_not_overwritten(self):
        result = self.result("Abeg, who buy the Cabin biscuit?", buyer_name="Amina")
        self.assertEqual(_validate_result(result).sales[0].buyer_name, "Amina")


if __name__ == "__main__":
    unittest.main()
