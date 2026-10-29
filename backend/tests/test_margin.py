"""Run from backend/:  python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import margin  # noqa: E402


def reading(platform, price, level="ok", available=True, quote_kind=None):
    return {"platform": platform, "price": price, "scarcity_level": level,
            "is_available": available, "quote_kind": quote_kind}


class ClientExamples(unittest.TestCase):
    """The worked examples from the 2026-10-28 call."""

    def test_margin_under_10_raises_to_buy_plus_10(self):
        # "our listing price is $50 ... selling at $45 ... increase to $55"
        d = margin.evaluate(50, [reading("spothero", 45)])
        self.assertEqual(d["action"], margin.RAISE)
        self.assertEqual(d["margin"], 5)
        self.assertEqual(d["suggested_price"], 55)
        self.assertFalse(d["needs_review"])

    def test_margin_10_or_more_is_left_alone(self):
        # "$50 ... buying at $35 ... skip"
        d = margin.evaluate(50, [reading("spothero", 35)])
        self.assertEqual(d["action"], margin.OK)
        self.assertEqual(d["margin"], 15)
        self.assertIsNone(d["suggested_price"])

    def test_exactly_10_is_ok(self):
        self.assertEqual(margin.evaluate(50, [reading("parkwhiz", 40)])["action"], margin.OK)

    def test_flagged_listing_is_skipped_even_when_under(self):
        d = margin.evaluate(50, [reading("spothero", 45)], skip=True)
        self.assertEqual(d["action"], margin.SKIP)
        self.assertIsNone(d["suggested_price"])


class BuyPrice(unittest.TestCase):

    def test_cheapest_bookable_platform_wins(self):
        d = margin.evaluate(50, [reading("spothero", 45), reading("parkwhiz", 38), reading("way", 41)])
        self.assertEqual((d["buy_price"], d["buy_platform"], d["action"]), (38, "parkwhiz", margin.OK))

    def test_sold_out_price_is_ignored(self):
        # The cheap reading is sold out, so the real cost is the dearer one.
        d = margin.evaluate(50, [reading("parkwhiz", 30, "sold_out", False), reading("spothero", 45)])
        self.assertEqual((d["buy_price"], d["action"], d["suggested_price"]), (45, margin.RAISE, 55))

    def test_limited_is_still_bookable(self):
        d = margin.evaluate(50, [reading("spothero", 45, "limited")])
        self.assertEqual(d["action"], margin.RAISE)

    def test_parkwhiz_standard_rate_is_ignored(self):
        d = margin.evaluate(50, [reading("parkwhiz", 30, quote_kind="standard"), reading("spothero", 45)])
        self.assertEqual(d["buy_price"], 45)

    def test_parkwhiz_event_rate_is_used(self):
        d = margin.evaluate(50, [reading("parkwhiz", 30, quote_kind="event_rate")])
        self.assertEqual(d["buy_price"], 30)

    def test_not_found_and_unknown_are_ignored(self):
        d = margin.evaluate(50, [reading("spothero", None, "not_found", False),
                                 reading("way", 20, "unknown", None),
                                 reading("parkwhiz", 44)])
        self.assertEqual(d["buy_price"], 44)

    def test_zero_or_bad_price_is_ignored(self):
        d = margin.evaluate(50, [reading("parkwhiz", 0), reading("spothero", "n/a"), reading("way", 42)])
        self.assertEqual(d["buy_price"], 42)

    def test_cents_are_kept_exact(self):
        d = margin.evaluate(50, [reading("spothero", 45.37)])
        self.assertEqual((d["margin"], d["suggested_price"]), (4.63, 55.37))


class NoData(unittest.TestCase):

    def test_no_listing_price(self):
        d = margin.evaluate(None, [reading("spothero", 45)])
        self.assertEqual(d["action"], margin.NO_DATA)
        self.assertEqual(d["buy_price"], 45)   # still shown, for the team

    def test_no_readings(self):
        self.assertEqual(margin.evaluate(50, [])["action"], margin.NO_DATA)

    def test_sold_out_everywhere_found_says_so(self):
        d = margin.evaluate(50, [reading("spothero", 45, "sold_out", False),
                                 reading("parkwhiz", None, "not_found", False)])
        self.assertEqual(d["action"], margin.NO_DATA)
        self.assertIn("sold out", d["reason"])

    def test_not_found_everywhere_is_not_called_sold_out(self):
        d = margin.evaluate(50, [reading("spothero", None, "not_found", False)])
        self.assertEqual(d["reason"], "no bookable buy price found")


class Review(unittest.TestCase):

    def test_big_jump_is_marked_for_review(self):
        # Buy $80 against our $50 -> raise to $90 (+$40): more likely a wrong
        # match than a real move, so a human looks first.
        d = margin.evaluate(50, [reading("spothero", 80)])
        self.assertEqual((d["action"], d["suggested_price"], d["needs_review"]), (margin.RAISE, 90, True))

    def test_jump_at_the_limit_is_not_marked(self):
        d = margin.evaluate(50, [reading("spothero", 65)])   # 75 - 50 = 25
        self.assertFalse(d["needs_review"])
        self.assertIsNone(d["review_note"])

    def test_failed_platform_marks_a_raise_for_review(self):
        # SpotHero rate-limited us; ParkWhiz alone says $47. SpotHero may be cheaper.
        d = margin.evaluate(50, [reading("spothero", None, "unknown", None), reading("parkwhiz", 47)])
        self.assertEqual((d["action"], d["suggested_price"], d["needs_review"]), (margin.RAISE, 57, True))
        self.assertEqual(d["review_note"], "SpotHero could not be checked — it may be cheaper")
        self.assertIn("could not be checked", d["reason"])

    def test_failed_platform_does_not_flag_an_ok(self):
        # A cheaper price on the failed platform could only widen the margin.
        d = margin.evaluate(50, [reading("spothero", None, "unknown", None), reading("parkwhiz", 30)])
        self.assertEqual((d["action"], d["needs_review"]), (margin.OK, False))

    def test_both_reasons_together(self):
        d = margin.evaluate(30, [reading("spothero", None, "unknown", None), reading("parkwhiz", 60)])
        self.assertEqual(d["review_note"],
                         "big jump — check the lot match; SpotHero could not be checked — it may be cheaper")


if __name__ == "__main__":
    unittest.main()
